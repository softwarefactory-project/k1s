# k1s: a minimal kubernetes service to start pod for nodepool

Proof of concept that simulates a Kubernetes API for Nodepool.
This only support pods listing, minimal creation, exec and destruction.
The api feature a custom (very limited) SPDY cherrypy tool to handle
the go-client exec api machinery.

## TODO

* Add real container creation, state and destroy
* Fixes todos inlined in source

## Setup

```
pip3 install --user cherrypy routes
PYTHONPATH=$(pwd) python3 k1s/api.py
```

## Usage

```
cat<<EOF>kubeconfig
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
    token: y2-4r06yYz4uJIdNidaWZyuoO7HGEwf7rRRrTCpRTLA
EOF
export KUBECONFIG=$(pwd)/kubeconfig

$ oc get pods
NAME      READY     STATUS    RESTARTS   AGE
todo      1/1       Running   0          22h
$ oc exec todo id
uid=1000(centos) gid=1000(centos)
```

## Ansible usage

```
cat<<EOF>inventory
[all]
todo ansible_connection=kubectl
EOF

cat<<EOF>pod.yaml
- hosts: all
  tasks:
    - command: ls
EOF

$ ansible-playbook -i inventory pod.yaml

PLAY [all] ************************************************************************************************************

TASK [Gathering Facts] ************************************************************************************************
ok: [todo]

TASK [command] ********************************************************************************************************
changed: [todo]

PLAY RECAP ************************************************************************************************************
todo                       : ok=2    changed=1    unreachable=0    failed=0
```
