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
import os
import select
import subprocess
import time
from typing import Dict, List

import cherrypy

from k1s.spdy import SPDYTool, SPDYHandler

cherrypy.tools.spdy = SPDYTool()


def getPod(podId: str) -> Dict:
    # TODO: read pod state informations
    ns = "tbd"
    ts = "2019-06-13T07:04:48Z"
    image = "nodepool-label"
    status = "Running"

    return {
        "kind": "Pod",
        "apiVersion": "v1",
        "metadata": {
            "name": podId,
            "namespace": ns,
            "creationTimestamp": ts,
        },
        "spec": {
            "volumes": [],
            "containers": [{
                "name": podId,
                "image": image,
                "args": ["paused"],
            }],
        },
        "status": {
            "phase": status,
            "startTime": ts,
            "containerStatuses": [{
                "name": podId,
                "state": {
                    status.lower(): {
                        "startedAt": ts,
                    }
                },
                "ready": True,
                "restartCount": 0,
                "image": image,
                "imageID": image,
            }],
        }
    }


def listPods() -> List[str]:
    # TODO: return list of running pods id
    return ["todo"]


class ExecHandler(SPDYHandler):
    def run(self):
        # print("Exec args are:", self.args)
        # TODO: verify pod is ready
        print(self.addr, "%s: %s" % (self.args['pod'], self.args['command']))
        # Process stream creation request first
        self.streams = {}
        while len(self.streams) != (4 if self.args.get('stdin') else 3):
            name, streamId = self.readStreamPacket()
            self.streams[name] = streamId
        # print("Got all the streams!", self.streams)

        # TODO: prefix command with podman|runc exec
        self.proc = subprocess.Popen(
            self.args['command'], bufsize=0, start_new_session=True,
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
                print("ERROR: process stalled")
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
    @cherrypy.expose
    @cherrypy.tools.spdy(handler_cls=ExecHandler)
    def execStream(self, ns, pod, *args, **kwargs):
        kwargs['pod'] = pod
        cherrypy.request.spdy_handler.handle(kwargs)
        resp = cherrypy.response
        resp.headers['X-Stream-Protocol-Version'] = "v4.channel.k8s.io"

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def get(self, ns: str, pod: str) -> Dict:
        return getPod(pod)

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def list(self, ns: str, **kwargs) -> Dict:
        return {
            "kind": "PodList",
            "apiVersion": "v1",
            "items": [getPod(pod) for pod in listPods()]
        }

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def api(self, **kwargs) -> Dict:
        return {
            "kind": "APIResourceList",
            "groupVersion": "v1",
            "resources": [{
                "name": "namespaces",
                "singularName": "",
                "namespaced": False,
                "kind": "Namespace",
                "verbs": [
                    "create",
                    "delete",
                    "get",
                    "list",
                    "patch",
                    "update",
                    "watch"
                ],
                "shortNames": [
                    "ns"
                ]
            }, {
                "name": "pods",
                "singularName": "",
                "namespaced": True,
                "kind": "Pod",
                "verbs": [
                    "create",
                    "delete",
                    "deletecollection",
                    "get",
                    "list",
                    "patch",
                    "update",
                    "watch"
                ],
                "shortNames": [
                    "po"
                ],
                "categories": [
                    "all"
                ]
            }, {
                "name": "pods/exec",
                "singularName": "",
                "namespaced": True,
                "kind": "Pod",
                "verbs": []
            }, {
                "name": "pods/log",
                "singularName": "",
                "namespaced": True,
                "kind": "Pod",
                "verbs": [
                    "get"
                ]
            }, {
                "name": "pods/status",
                "singularName": "",
                "namespaced": True,
                "kind": "Pod",
                "verbs": [
                    "get",
                    "patch",
                    "update"
                ]
            },
            ]
        }

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def index(self, **kwargs) -> Dict:
        return {
            "kind": "APIVersions",
            "versions": ["v1"]
        }

    @cherrypy.expose
    @cherrypy.tools.json_out(content_type='application/json; charset=utf-8')
    def indexes(self, **kwargs) -> Dict:
        return {
            "kind": "APIGroupList",
            "apiVersion": "v1",
            "groups": []}


def main(port=9023, blocking=True):
    route_map = cherrypy.dispatch.RoutesDispatcher()
    api = K1s()
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{pod}/exec',
                      controller=api, action='execStream',
                      conditions=dict(method=["POST"]))
    route_map.connect('api', '/api/v1/namespaces/{ns}/pods/{pod}',
                      controller=api, action='get')
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
    main()
