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

from contextlib import closing
import socket
import unittest
import tempfile
import os
import subprocess

import cherrypy
# from kubernetes.config import config_exception as kce
from kubernetes import client as k8s_client
from openshift import config

import k1s.api


def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


class K1sTestCase(unittest.TestCase):
    def setUp(self):
        self.port = find_free_port()
        self.api = k1s.api.main(self.port, blocking=False)
        self.url = "http://localhost:%d" % self.port
        self.kubeconfig = tempfile.mkstemp()[1]
        with open(self.kubeconfig, "w") as of:
            of.write("""apiVersion: v1
clusters:
- cluster:
    server: http://localhost:%d
  name: k1s
contexts:
- context:
    cluster: k1s
    user: admin/k1s
  name: /k1s/admin
current-context: /k1s/admin
kind: Config
preferences: {}
users:
- name: admin/k1s
  user:
    token: y2-4r06yYz4uJIdNidaWZyuoO7HGEwf7rRRrTCpRTLA
""" % self.port)

    def tearDown(self):
        cherrypy.engine.exit()
        cherrypy.server.httpserver = None
        os.unlink(self.kubeconfig)

    def test_python_client(self):
        conf = config.new_client_from_config(
            config_file=self.kubeconfig, context='/k1s/admin')
        # tok = conf.configuration.api_key.get('authorization', '').split()[-1]
        client = k8s_client.CoreV1Api(conf)

        pods = client.list_namespaced_pod("nodepool").items
        assert 1 == len(pods)
        assert "Pod" == pods[0].kind
        assert "todo" == pods[0].metadata.name

    def test_kubectl(self):
        proc = subprocess.Popen(["kubectl", "exec", "todo", "id"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(KUBECONFIG=self.kubeconfig))
        stdout, stderr = proc.communicate()
        assert b"" == stderr
        assert b"uid=" in stdout
        assert 0 == proc.wait()

    def test_ansible(self):
        playbook = tempfile.mkstemp()[1]
        hosts = tempfile.mkstemp()[1]
        with open(playbook, "w") as of:
            of.write("""
- hosts: all
  tasks:
    - command: echo success
    - command: sleep 5
    - command: echo success
""")
        with open(hosts, "w") as of:
            of.write("""[all]\ntodo ansible_connection=kubectl\n""")

        proc = subprocess.Popen(["ansible-playbook", "-i", hosts, playbook],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(KUBECONFIG=self.kubeconfig,
                                         PATH=os.environ["PATH"]))
        stdout, stderr = proc.communicate()
        os.unlink(playbook)
        os.unlink(hosts)
        assert b"" == stderr
        assert b"failed=0" in stdout
        assert b"unreachable=0" in stdout
        assert b"changed=3" in stdout
        assert b"ok=4" in stdout
        assert 0 == proc.wait()
