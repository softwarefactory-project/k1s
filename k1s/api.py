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
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

import cherrypy

from k1s.schema import Pod, new_pod, PodAPI
from k1s.spdy import SPDYTool, SPDYHandler

log = logging.getLogger("K1S.api")
cherrypy.tools.spdy = SPDYTool()
Podman = ["podman"]
NsEnter = ["nsenter"]
TZFormat = "%Y-%m-%dT%H:%M:%S"


if os.getuid():
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
    return json.loads(p.communicate()[0])[0]


def netns_pod(name: str) -> str:
    return inspect_pod(name)["NetworkSettings"]["SandboxKey"]


def port_forward(name: str, port: int, spdy: SPDYHandler) -> int:
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
    return process(proc, spdy, "data", "data")


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
    return process(proc, spdy, "stdout", "stdout")


def process(
        proc: subprocess.Popen, spdy: SPDYHandler,
        stdout: str, stderr: str) -> int:
    rc = -1
    try:
        for f in (proc.stdout, proc.stderr, spdy.sock):
            # Make proc output non blocking
            fd = f.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        idle_time = time.monotonic()
        while True:
            process_active = False
            r, w, x = select.select(
                [proc.stdout, proc.stderr, spdy.sock], [], [], 1)
            # print("Select yield", r)
            if time.monotonic() - idle_time > 3600:
                log.error("ERROR: process stalled")
                break
            for reader in r:
                if reader == spdy.sock:
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
                    if reader == proc.stdout:
                        output = stdout
                    else:
                        output = stderr
                    data = reader.read()
                    if data:
                        process_active = True
                        spdy.sendFrame(spdy.streams[output], data)
            if not process_active and proc.poll() is not None:
                rc = proc.poll()
                break
    finally:
        proc.terminate()
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
        if inf["metadata"]["namespace"] != self.args['ns']:
            raise RuntimeError("Invalid namespace %s" % self.args['ns'])
        self.streams = {}  # type: Dict[str, int]
        while len(self.streams) != 2:
            try:
                name, streamId, port = self.readStreamPacket()
            except socket.timeout:
                # kubectl client delays stream creation until the first
                # connection.
                continue
            self.streams[name] = streamId
            if name == "error":
                # print("Purging data frame")
                self.readDataFrame()
                # print(data)
        try:
            port_forward(self.args['pod'], port, self)
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
            name, streamId, _ = self.readStreamPacket()
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
    if os.environ.get("K1S_DEBUG"):
        logging.basicConfig(
            format='%(levelname)-5.5s - %(message)s',
            level=logging.DEBUG)
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
    cherrypy.engine.start()
    if blocking:
        cherrypy.engine.block()
    return api


def run():
    main(port=os.environ.get("K1S_PORT", 9023),
         token=os.environ.get("K1S_TOKEN"),
         tls=os.environ.get("K1S_TLS_PATH"))


if __name__ == '__main__':
    main()
