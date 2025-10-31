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
import sys
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
from kubernetes import config

import k1s.api


# Pod settings

POD = "quay.io/fedora/python-311:latest"
PYTHONPATH = "/opt/app-root/bin/python"


def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def generate_cert():
    d = str(tempfile.mkdtemp())
    if subprocess.Popen(
            ["openssl", "genrsa", "-out", d + "/key.pem", "2048"]).wait(
                timeout=60
            ):
        raise RuntimeError("Couldn't create privkey")
    if subprocess.Popen(
            ["openssl", "req", "-new", "-x509", "-days", "365",
             "-subj", "/C=FR/O=K1S/CN=localhost",
             "-key", d + "/key.pem",
             "-out", d + "/cert.pem"]).wait(timeout=60):
        raise RuntimeError("Couldn't create cert")
    return d


# sudo shouldn't be required, keep this in case it is
def sudo_if_needed(args):
    if os.environ.get('K1S_TESTS_NEED_SUDO'):
        return['sudo'] + args
    return args


def find_kubectl():
    proc = subprocess.Popen(["which", "kubectl", ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    return stdout.decode('utf-8').strip()


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

    def createPod(self, podName=None):
        if podName is None:
            podName = "nodepool"
        self.pod = "%s-%d" % (podName, self.port)
        args = sudo_if_needed(
            ["podman", "run", "-it", "--name", "k1s-" + self.pod,
             "--rm", POD, "sleep", "Inf"])
        self.proc = subprocess.Popen(args)

        i = 1
        isUp = False
        # Give pod time to start...
        while i < 256 and not isUp:
            test, _ = subprocess.Popen(
                sudo_if_needed(["podman", "ps"]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            ).communicate()
            isUp = ("k1s-" + self.pod) in test.decode('utf-8').strip()
            time.sleep(i)
            i = i * 2

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
            try:
                subprocess.Popen(
                    sudo_if_needed(["podman", "kill", "k1s-" + self.pod])
                ).wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise Warning(
                    'Warning: pod "k1s-%s" could not be properly killed, '
                    'some manual cleanup is required.' % self.pod)

    def test_python_client(self):
        conf = config.new_client_from_config(
            config_file=self.kubeconfig, context='/k1s/admin')
        client = k8s_client.CoreV1Api(conf)
        self.createPod("python_client")

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
            'image': POD,
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
            pod_name, "default", body=delete_body)

        time.sleep(1)
        pods = client.list_namespaced_pod("default").items
        if any(filter(lambda x: x.metadata.name == pod_name, pods)):
            print(pods)
            assert False, "pod %s still there..." % pod_name

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
        self.createPod("kubectl")
        KUBECTL = find_kubectl()
        proc = subprocess.Popen([KUBECTL, "exec", self.pod, "--", "id"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(KUBECONFIG=self.kubeconfig))
        stdout, stderr = proc.communicate()
        assert b"" == stderr
        assert b"uid=" in stdout
        assert 0 == proc.wait(timeout=60)

    def test_ansible(self):
        playbook = tempfile.mkstemp()[1]
        hosts = tempfile.mkstemp()[1]
        with open(playbook, "w") as of:
            of.write("""
- hosts: all
  tasks:
    - ansible.builtin.command: echo success
    - ansible.builtin.command: sleep 5
    - ansible.builtin.command: echo success
""")
        self.createPod("ansible")
        with open(hosts, "w") as of:
            of.write("[all]\n%s ansible_connection=kubectl "
                     "ansible_python_interpreter=%s\n" % (self.pod, PYTHONPATH))

        proc = subprocess.Popen(["ansible-playbook", "-vvvv", "-i", hosts, playbook],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(KUBECONFIG=self.kubeconfig,
                                         PATH=os.environ["PATH"]))
        stdout, stderr = proc.communicate()
        os.unlink(playbook)
        os.unlink(hosts)
        assert b"failed=0" in stdout
        assert b"unreachable=0" in stdout
        assert b"changed=3" in stdout
        assert b"ok=4" in stdout
        assert b"" == stderr
        assert 0 == proc.wait(timeout=60)
