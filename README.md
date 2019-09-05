# k1s: a minimal kubernetes service to start pod for nodepool

Proof of concept that simulates a Kubernetes API for Nodepool.
This only support pods listing, minimal creation, exec and destruction.
The api feature a custom (very limited) SPDY cherrypy tool to handle
the go-client exec api machinery.

## Setup

```
pip3 install --user cherrypy routes
PYTHONPATH=$(pwd) python3 k1s/api.py
```

## Usage

```
$ cat<<EOF>kubeconfig
apiVersion: v1
clusters:
- cluster:
    server: http://localhost:9023
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
    token: secret
EOF
export KUBECONFIG=$(pwd)/kubeconfig

$ oc apply -f - << EOF
apiVersion: v1
kind: Pod
metadata:
  name: fedora-000042
spec:
  containers:
  - image: registry.fedoraproject.org/fedora:30
EOF

$ oc get pods
NAME               READY     STATUS    RESTARTS   AGE
fedora-000042      1/1       Running   0          3s

$ oc exec fedora-000042 id
uid=0(root) gid=0(root) groups=0(root)
```

## Ansible usage

```
cat<<EOF>inventory
[all]
fedora-000042 ansible_connection=kubectl
EOF

cat<<EOF>pod.yaml
- hosts: all
  tasks:
    - command: ls
EOF

$ ansible-playbook -i inventory pod.yaml

PLAY [all] **************************************************

TASK [Gathering Facts] **************************************
ok: [fedora-000042]

TASK [command] **********************************************
changed: [fedora-000042]

PLAY RECAP **************************************************
fedora-000042: ok=2    changed=1    unreachable=0    failed=0
```
