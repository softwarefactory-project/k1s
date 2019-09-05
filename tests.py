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
import shutil
import subprocess
import time
import uuid

import cherrypy
# from kubernetes.config import config_exception as kce
from kubernetes import client as k8s_client
from openshift import config

import k1s.api


fedora = "registry.fedoraproject.org/fedora:30"


def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def generate_cert():
    d = str(tempfile.mkdtemp())
    if subprocess.Popen(
            ["openssl", "genrsa", "-out", d + "/key.pem", "2048"]).wait():
        raise RuntimeError("Couldn't create privkey")
    if subprocess.Popen(
            ["openssl", "req", "-new", "-x509", "-days", "365",
             "-subj", "/C=FR/O=K1S/CN=localhost",
             "-key", d + "/key.pem",
             "-out", d + "/cert.pem"]).wait():
        raise RuntimeError("Couldn't create cert")
    return d


class K1sTestCase(unittest.TestCase):
    def setUp(self):
        self.port = find_free_port()
        self.cert_dir = generate_cert()
        self.token = str(uuid.uuid4())
        self.api = k1s.api.main(
            self.port, blocking=False, token=self.token, tls=self.cert_dir)
        self.url = "http://localhost:%d" % self.port
        self.kubeconfig = tempfile.mkstemp()[1]
        self.writeKubeConfig(self.token)
        self.proc = None

    def createPod(self):
        self.pod = "nodepool-%d" % self.port
        self.proc = subprocess.Popen([
            "sudo", "podman", "run", "-it", "--name", "k1s-" + self.pod,
            "--rm", fedora, "sleep", "Inf"])
        # Give pod a second to start...
        time.sleep(1)

    def writeKubeConfig(self, token):
        with open(self.kubeconfig, "w") as of:
            of.write("""apiVersion: v1
clusters:
- cluster:
    server: https://localhost:%d
    insecure-skip-tls-verify: true
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
    token: %s
""" % (self.port, token))

    def tearDown(self):
        cherrypy.engine.exit()
        cherrypy.server.httpserver = None
        os.unlink(self.kubeconfig)
        shutil.rmtree(self.cert_dir)
        if self.proc:
            subprocess.Popen(
                ["sudo", "podman", "kill", "k1s-" + self.pod]).wait()

    def test_python_client(self):
        conf = config.new_client_from_config(
            config_file=self.kubeconfig, context='/k1s/admin')
        client = k8s_client.CoreV1Api(conf)
        self.createPod()

        pods = client.list_namespaced_pod("nodepool").items
        assert len(pods) >= 1
        assert "Pod" == pods[0].kind
        assert self.pod == pods[0].metadata.name

    def test_create(self):
        conf = config.new_client_from_config(
            config_file=self.kubeconfig, context='/k1s/admin')
        client = k8s_client.CoreV1Api(conf)

        pod_name = "created-%d" % self.port
        spec_body = {
            'name': pod_name,
            'image': fedora,
            'command': ["/bin/bash", "-c", "--"],
            'args': ["while true; do sleep 30; done;"],
            'workingDir': '/tmp',
        }
        pod_body = {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {'name': pod_name},
            'spec': {
                'containers': [spec_body],
            },
            'restartPolicy': 'Never',
        }
        client.create_namespaced_pod("default", pod_body)
        time.sleep(1)

        pods = client.list_namespaced_pod("default").items
        assert len(pods) >= 1
        assert "Pod" == pods[0].kind
        assert pod_name == pods[0].metadata.name

        delete_body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Background"
        }
        client.delete_namespaced_pod(
            pod_name, "default", delete_body)

        time.sleep(1)
        pods = client.list_namespaced_pod("default").items
        if any(filter(lambda x: x.metadata.name == pod_name, pods)):
            print(pods)
            assert False, "pod still there..."

    def test_bad_token(self):
        self.writeKubeConfig("bad-token")
        conf = config.new_client_from_config(
            config_file=self.kubeconfig, context='/k1s/admin')
        client = k8s_client.CoreV1Api(conf)

        try:
            client.list_namespaced_pod("nodepool")
            assert False
        except k8s_client.rest.ApiException:
            pass

    def test_kubectl(self):
        self.createPod()
        proc = subprocess.Popen(["kubectl", "exec", self.pod, "id"],
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
        self.createPod()
        with open(hosts, "w") as of:
            of.write("[all]\n%s ansible_connection=kubectl "
                     "ansible_python_interpreter=/bin/python3\n" % self.pod)

        proc = subprocess.Popen(["ansible-playbook", "-i", hosts, playbook],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(KUBECONFIG=self.kubeconfig,
                                         PATH=os.environ["PATH"]))
        stdout, _ = proc.communicate()
        os.unlink(playbook)
        os.unlink(hosts)
        assert b"failed=0" in stdout
        assert b"unreachable=0" in stdout
        assert b"changed=3" in stdout
        assert b"ok=4" in stdout
        assert 0 == proc.wait()
