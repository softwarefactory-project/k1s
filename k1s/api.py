# Copyright 2019 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import fcntl
import json
import logging
import os
import select
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Union

import cherrypy  # type: ignore

from k1s.schema import Pod, new_pod, PodAPI  # type: ignore
from k1s.spdy import SPDYTool, SPDYHandler  # type: ignore

log = logging.getLogger("K1S.api")
cherrypy.tools.spdy = SPDYTool()
Podman = ["podman"]
NsEnter = ["nsenter"]
TZFormat = "%Y-%m-%dT%H:%M:%S"


def setup_logging(with_debug):
    fmt = '%(name)s - %(levelname)-5.5s - %(message)s'
    if with_debug:
        logging.basicConfig(format=fmt, level=logging.DEBUG)
    else:
        ch = logging.StreamHandler()
        formatter = logging.Formatter(fmt)
        ch.setFormatter(formatter)
        log.addHandler(ch)
        log.setLevel(logging.INFO)


def has_rootless() -> bool:
    try:
        return open("/proc/sys/user/max_user_namespaces").read().strip() != "0"
    except:
        return False


def is_user() -> bool:
    return os.getuid() != 0


if is_user():
    if not has_rootless():
        Podman.insert(0, "sudo")
    NsEnter.insert(0, "sudo")


def pread(args: List[str]) -> str:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    stdout, _ = proc.communicate()
    return stdout.decode('utf-8')


def now() -> str:
    return time.strftime(TZFormat)


def delete_pod(name: str) -> None:
    log.info("Deleting %s")
    subprocess.Popen(Podman + ["kill", "k1s-" + name])
    try:
        subprocess.Popen(Podman + ["rm", "-f", "k1s-" + name])
    except Exception:
        pass


def inspect_pod(name: str) -> Dict[str, Any]:
    p = subprocess.Popen(
        Podman + ["inspect", "k1s-" + name], stdout=subprocess.PIPE)
    return json.loads(p.communicate()[0])[0]  # type: ignore


def netns_pod(name: str) -> str:
    pid = inspect_pod(name)["State"]["Pid"]  # type: ignore
    return ("/proc/%s/ns/net" % pid)


# this doesn't work on some python because of: TypeError: 'type' object is not subscriptable
# if sys.version_info[1] < 7:
Proc = int
# else:
#    Proc = subprocess.Popen[Any]
Stream = Optional[Proc]
Streams = Dict[int, Stream]


def create_stream(name: str, port: int) -> Proc:
    args = [
        "--net=" + netns_pod(name),
        "socat", "stdio", "tcp-connect:localhost:" + str(port)
        ]
    log.debug("Running %s", args)
    proc = subprocess.Popen(
        NsEnter + args,
        bufsize=0, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE)
    list(map(unblock_file, (proc.stdout, proc.stderr)))
    return proc


def handle_port_forward_input(
        name: str, spdy: SPDYHandler, streams: Streams) -> None:
    isData, frame = spdy.readAnyFrame()
    if isData:
        streamId, flag, data = frame
        if flag == 1:
            log.debug("PF: got a fin data frame: %s", streamId)
            try:
                proc = streams.pop(streamId)
                spdy.closeStream(streamId)
                if proc:
                    terminate(proc)
            except KeyError:
                log.warning(
                    "PF: got a fin frame for an unknown stream %s", streamId)
        else:
            log.debug("PF: got a data frame for %s", streamId)
            try:
                proc = streams[streamId]
                try:
                    if proc and proc.stdin:
                        proc.stdin.write(data)
                    else:
                        raise BrokenPipeError
                except BrokenPipeError:
                    log.warning("PF: Invalid stream %s", streamId)
                    streams.pop(streamId)
            except KeyError:
                log.warning(
                    "PF: got a data frame for an unknown stream %s", streamId)
    else:
        streamId, streamName, streamPort = frame
        log.debug("PF: got a new stream %s named %s", streamId, streamName)
        if streamName == "error":
            streams[streamId] = None
        elif streamName == "data":
            streams[streamId] = create_stream(name, streamPort)
        else:
            log.warning("Unknown stream %s", streamName)


def unblock_file(f: Any) -> None:
    fd = f.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def terminate(proc: Proc) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def stalled(idle_time: float) -> bool:
    return time.monotonic() - idle_time > 21600 # 6 hours


def run_port_forwards(name: str, spdy: SPDYHandler) -> None:
    streams: Dict[int, Stream] = dict()
    unblock_file(spdy.sock)
    try:
        idle_time = time.monotonic()
        while True:
            readers = [spdy.sock]
            for proc in streams.values():
                if proc:
                    readers.extend([proc.stdout, proc.stderr])
            r, w, x = select.select(readers, [], [], 1)
            if stalled(idle_time):
                log.error("ERROR: process stalled")
                break
            for reader in r:
                if reader == spdy.sock:
                    handle_port_forward_input(name, spdy, streams)
                else:
                    data = reader.read()
                    idle_time = time.monotonic()
                    for streamId, proc in streams.items():
                        if proc and reader in (proc.stdout, proc.stderr):
                            if data:
                                spdy.sendFrame(streamId, data)
                            else:
                                # Python is telling us that socat is dead
                                spdy.closeStream(streamId)
                                terminate(proc)
                            break
    finally:
        for proc in streams.values():
            if proc:
                terminate(proc)


def exec_pod(
        name: str, stdin: bool,
        args: Union[str, List[str]],
        spdy: SPDYHandler) -> int:
    exec_command = ["exec"]
    if stdin:
        exec_command.append("-i")
    exec_command.append("k1s-" + name)
    if isinstance(args, list):
        exec_command.extend(args)
    else:
        exec_command.append(args)

    log.debug("Running %s", args)
    proc = subprocess.Popen(
        Podman + exec_command,
        bufsize=0, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin else None)
    return run_exec_stream(proc, spdy)


def run_exec_stream(proc: Proc, spdy: SPDYHandler) -> int:
    rc = -1
    try:
        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("Invalid proc")
        list(map(unblock_file, (proc.stdout, proc.stderr, spdy.sock)))
        idle_time = time.monotonic()
        while True:
            process_active = False
            r, w, x = select.select(
                [proc.stdout, proc.stderr, spdy.sock], [], [], 1)
            # print("Select yield", r)
            if stalled(idle_time):
                log.error("ERROR: process stalled")
                break
            for reader in r:
                if reader == spdy.sock:
                    if proc.stdin is None:
                        raise RuntimeError("Process does not have stdin")
                    # Assume streamId is always stdin
                    _, flag, data = spdy.readDataFrame()
                    if flag == 1:
                        # This is the end
                        proc.stdin.close()
                    else:
                        try:
                            proc.stdin.write(data)
                        except BrokenPipeError:
                            return -1
                        proc.stdin.flush()
                else:

                    idle_time = time.monotonic()
                    output = "stdout" if reader == proc.stdout else "stderr"
                    data = reader.read()
                    if data:
                        process_active = True
                        spdy.sendFrame(spdy.streams[output], data)
            if not process_active and proc.poll() is not None:
                rc_val = proc.poll()
                if rc_val is not None:
                    rc = rc_val
                break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    return rc


def create_pod(name: str, namespace: str, image: str) -> Pod:
    log.info("Creating pod %s with %s", name, image)
    create_args = [
        "run", "--rm", "--detach", "--name", "k1s-" + name,
        image, "sleep", "Inf"]
    if subprocess.Popen(Podman + create_args).wait():
        log.warning("Couldn't create pod")
    return new_pod(name, namespace, image, now(), "Pending")


def get_pod(name: str) -> Optional[Pod]:
    try:
        inf = json.loads(pread(
            Podman + ["container", "inspect", "k1s-" + name]))
    except json.decoder.JSONDecodeError:
        return None
    if len(inf) > 1:
        raise RuntimeError("Multiple container with same name: %s!" % name)
    if len(inf) == 0:
        return None
    inf = inf[0]
    ns = inf["Config"]["Annotations"].get(
        "io.softwarefactory-project.k1s.Namespace", "default")
    ts = inf["Created"]
    image = inf["ImageName"]
    status = inf["State"]["Status"].capitalize()
    return new_pod(name, ns, image, ts, status)


def list_pods() -> List[Pod]:
    pod_list = []
    list_args = Podman + ["ps", "-a", "--format", "{{.Names}}"]
    for pod_name in list(filter(
            lambda x: x.startswith("k1s-"),
            pread(list_args).split('\n'))):
        pod = get_pod(pod_name.lstrip("k1s-"))
        if pod:
            if pod.get('status', {}).get('phase', '').lower() == 'exited':
                try:
                    delete_pod(pod['metadata']['name'])
                except Exception:
                    log.exception("%s: couldn't delete exited pod", pod_name)
            else:
                pod_list.append(pod)
    return pod_list


class PortHandler(SPDYHandler):
    def run(self) -> None:
        inf = get_pod(self.args['pod'])
        if not inf:
            raise cherrypy.HTTPError(404, "Pod not found")

        # TODO: add support for multiple namespaces
        # in the meantime, disable that bogus check
        # if inf["metadata"]["namespace"] != self.args['ns']:
        #     raise RuntimeError("Invalid namespace %s" % self.args['ns'])

        try:
            run_port_forwards(self.args['pod'], self)
        except EOFError:
            pass
        except Exception:
            log.exception("port_forward failed")
        self.sock.close()


class ExecHandler(SPDYHandler):
    def run(self) -> None:
        inf = get_pod(self.args['pod'])
        if not inf:
            raise cherrypy.HTTPError(404, "Pod not found")
        if inf["metadata"]["namespace"] != self.args['ns']:
            raise RuntimeError("Invalid namespace %s" % self.args['ns'])
        # print("Exec args are:", self.args)
        log.debug("%s: runnning %s %s",
                  self.addr, self.args['pod'], self.args['command'])
        # Process stream creation request first
        self.streams = {}  # type: Dict[str, int]
        while len(self.streams) != (4 if self.args.get('stdin') else 3):
            streamId, name, _ = self.readStreamPacket()
            self.streams[name] = streamId
        # print("Got all the streams!", self.streams)

        try:
            rc = exec_pod(
                self.args['pod'], self.args.get('stdin', False),
                self.args['command'], self)
        except Exception:
            log.exception("execution failed")
            rc = 1

        self.sendFrame(self.streams['error'], json.dumps({
            'kind': 'Status',
            'Status': 'Failure' if rc else 'Success',
            'code': rc}).encode('ascii'))
        self.sock.close()
        # print(self.addr, "over and out")


class K1s:
    def __init__(self, token):
        self.bearer = 'Bearer %s' % token if token else None

    def check_token(self, headers: Dict[str, str]) -> None:
        if self.bearer and headers.get('Authorization', '') != self.bearer:
            raise cherrypy.HTTPError(401, 'Unauthorized')

    @cherrypy.expose
    @cherrypy.tools.spdy(handler_cls=PortHandler)
    def port_forward(self, ns: str, name: str, *args, **kwargs) -> None:
        self.check_token(cherrypy.request.headers)
        kwargs['pod'] = name
        kwargs['ns'] = ns
        cherrypy.request.spdy_handler.handle(kwargs)
        resp = cherrypy.response
        resp.headers['X-Stream-Protocol-Version'] = "v4.channel.k8s.io"

    @cherrypy.expose
    @cherrypy.tools.spdy(handler_cls=ExecHandler)
    def exec_stream(self, ns: str, name: str, *args, **kwargs) -> None:
        self.check_token(cherrypy.request.headers)
        kwargs['pod'] = name
        kwargs['ns'] = ns
        cherrypy.request.spdy_handler.handle(kwargs)
        resp = cherrypy.response
        resp.headers['X-Stream-Protocol-Version'] = "v4.channel.k8s.io"

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def get(self, ns: str, name: str) -> Dict:
        self.check_token(cherrypy.request.headers)
        pod = get_pod(name)
        if not pod:
            raise cherrypy.HTTPError(404, "Pod not found")
        return pod

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def delete(self, ns: str, name: str, **kwargs) -> None:
        self.check_token(cherrypy.request.headers)
        delete_pod(name)

    @cherrypy.expose
    @cherrypy.tools.json_in()
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def create(self, ns: str, **kwargs) -> Pod:
        self.check_token(cherrypy.request.headers)
        req = cherrypy.request.json
        return create_pod(
            name=req["metadata"]["name"],
            namespace=ns,
            image=req["spec"]["containers"][0]["image"])

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def list(self, ns: str, **kwargs) -> Dict:
        self.check_token(cherrypy.request.headers)
        return dict(kind="PodList", apiVersion="v1", items=list_pods())

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def api(self, **kwargs) -> Dict:
        self.check_token(cherrypy.request.headers)
        return PodAPI

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def index(self, **kwargs) -> Dict:
        self.check_token(cherrypy.request.headers)
        return dict(kind="APIVersions", versions=["v1"])

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def indexes(self, **kwargs) -> Dict:
        self.check_token(cherrypy.request.headers)
        return dict(kind="APIGroupList", apiVersion="v1", groups=[])


def main(port=9023, blocking=True, token=None, tls=None):
    setup_logging(os.environ.get("K1S_DEBUG"))
    route_map = cherrypy.dispatch.RoutesDispatcher()
    api = K1s(token)
    if tls:
        cherrypy.server.ssl_module = 'builtin'
        cherrypy.server.ssl_certificate = os.path.join(tls, "cert.pem")
        cherrypy.server.ssl_private_key = os.path.join(tls, "key.pem")
        chain_path = os.path.join(tls, "chain.pem")
        if os.path.exists(chain_path):
            cherrypy.server.ssl_certificate_chain = chain_path

    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{name}/portforward',
                      controller=api, action='port_forward',
                      conditions=dict(method=["POST"]))
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{name}/exec',
                      controller=api, action='exec_stream',
                      conditions=dict(method=["POST"]))
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{name}',
                      controller=api, action='delete',
                      conditions=dict(method=["DELETE"]))
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{name}',
                      controller=api, action='get')
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods',
                      controller=api, action='create',
                      conditions=dict(method=["POST"]))
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods',
                      controller=api, action='list')
    route_map.connect('api', '/api/v1',
                      controller=api, action='api')
    route_map.connect('api', '/apis',
                      controller=api, action='indexes')
    route_map.connect('api', '/api',
                      controller=api, action='index')

    conf = {'/': {'request.dispatch': route_map}}
    gl_conf = {
        'engine.autoreload.on': True,
        'server.socket_host': os.environ.get("K1S_HOST", '0.0.0.0'),
        'server.socket_port': int(port),
    }
    if os.environ.get("K1S_ENV", "production") == "production":
        gl_conf["environment"] = "production"
    cherrypy.config.update({"global": gl_conf})
    cherrypy.tree.mount(api, '/', config=conf)
    log.info("Starting k1s API")
    cherrypy.engine.start()
    if blocking:
        cherrypy.engine.block()
    return api


def run():
    main(port=os.environ.get("K1S_PORT", 9023),
         token=os.environ.get("K1S_TOKEN"),
         tls=os.environ.get("K1S_TLS_PATH"))


if __name__ == '__main__':
    run()
