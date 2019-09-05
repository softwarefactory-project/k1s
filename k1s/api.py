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
import time
from typing import Dict, List, Optional

import cherrypy

from k1s.schema import Pod, new_pod, PodAPI
from k1s.spdy import SPDYTool, SPDYHandler

log = logging.getLogger("K1S.api")
cherrypy.tools.spdy = SPDYTool()
Podman = ["podman"]
TZFormat = "%Y-%m-%dT%H:%M:%S"


if os.getuid():
    Podman.insert(0, "sudo")


def pread(args: List[str]) -> str:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    stdout, _ = proc.communicate()
    return stdout.decode('utf-8')


def now() -> str:
    return time.strftime(TZFormat)


def delete_pod(name: str) -> None:
    log.info("Deleting %s")
    subprocess.Popen(Podman + ["kill", "k1s-" + name])


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
            pod_list.append(pod)
    return pod_list


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
            name, streamId = self.readStreamPacket()
            self.streams[name] = streamId
        # print("Got all the streams!", self.streams)

        execCommand = ["exec"]
        if self.args.get('stdin'):
            execCommand.append("-i")
        execCommand.append("k1s-" + self.args['pod'])
        if isinstance(self.args['command'], list):
            execCommand.extend(self.args['command'])
        else:
            execCommand.append(self.args['command'])

        self.proc = subprocess.Popen(
            Podman + execCommand,
            bufsize=0, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if self.args.get('stdin') else None)
        for f in (self.proc.stdout, self.proc.stderr, self.sock):
            # Make proc output non blocking
            fd = f.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        rc = -1
        idle_time = time.monotonic()
        while True:
            process_active = False
            r, w, x = select.select(
                [self.proc.stdout, self.proc.stderr, self.sock], [], [], 1)
            # print("Select yield", r)
            if time.monotonic() - idle_time > 3600:
                log.error("ERROR: process stalled")
                break
            for reader in r:
                if reader == self.sock:
                    # Assume streamId is always stdin
                    _, flag, data = self.readDataFrame()
                    if flag == 1:
                        # This is the end
                        self.proc.stdin.close()
                    else:
                        print("Writting proc stdin", data)
                        self.proc.stdin.write(data)
                        self.proc.stdin.flush()
                else:

                    idle_time = time.monotonic()
                    if reader == self.proc.stdout:
                        output = "stdout"
                    else:
                        output = "stderr"
                    data = reader.read()
                    if data:
                        process_active = True
                        self.sendFrame(self.streams[output], data)
            if not process_active and self.proc.poll() is not None:
                rc = self.proc.poll()
                break

        self.sendFrame(self.streams['error'], json.dumps({
            'kind': 'Status',
            'Status': 'Failure' if rc else 'Success',
            'code': rc}).encode('ascii'))
        self.sock.close()
        self.proc.terminate()
        print(self.addr, "over and out")


class K1s:
    def __init__(self, token):
        self.bearer = 'Bearer %s' % token if token else None

    def check_token(self, headers: Dict[str, str]) -> None:
        if self.bearer and headers.get('Authorization', '') != self.bearer:
            raise cherrypy.HTTPError(401, 'Unauthorized')

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


def main(port=9023, blocking=True, token=None, tls={}):
    route_map = cherrypy.dispatch.RoutesDispatcher()
    api = K1s(token)
    if tls.get("cpath"):
        cherrypy.server.ssl_module = 'builtin'
        cherrypy.server.ssl_certificate = tls["cpath"]
        cherrypy.server.ssl_private_key = tls["kpath"]
        if tls.get("chain_path"):
            cherrypy.server.ssl_certificate_chain = tls["chain_path"]

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
    cherrypy.config.update({
        'global': {
            # 'environment': 'production',
            'engine.autoreload.on': True,
            'server.socket_host': '127.0.0.1',
            'server.socket_port': int(port),
        },
    })
    cherrypy.tree.mount(api, '/', config=conf)
    cherrypy.engine.start()
    if blocking:
        cherrypy.engine.block()
    return api


if __name__ == '__main__':
    main(token=os.environ.get("K1S_TOKEN"),
         tls=dict(
             cpath=os.environ.get("K1S_CERT_PATH"),
             kpath=os.environ.get("K1S_KEY_PATH"),
             chain_path=os.environ.get("K1S_CHAIN_PATH")
         ))
