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

from typing import Any, Dict

Pod = Dict[str, Any]


def new_pod(name, namespace, image, timestamp, status) -> Pod:
    return dict(
        kind="Pod",
        apiVersion="v1",
        metadata=dict(
            name=name,
            namespace=namespace,
            creationTimestamp=timestamp,
            selfLink="/api/v1/namespaces/%s/pods/%s" % (namespace, name),
        ),
        spec=dict(
            volumes=[],
            containers=[dict(
                name=name,
                image=image,
                args=["paused"],
            )],
        ),
        status=dict(
            phase=status,
            startTime=timestamp,
            containerStatuses=[dict(
                name=name,
                state={
                    status.lower(): dict(startedAt=timestamp),
                },
                ready=True,
                restartCount=0,
                image=image,
                imageID=image,
            )]))


PodAPI = dict(
    kind="APIResourceList",
    groupVersion="v1",
    resources=[dict(
        name="namespaces",
        singularName="",
        namespaced=False,
        kind="Namespace",
        verbs=["create", "delete", "get", "list", "patch", "update", "watch"],
        shortNames=["ns"]
    ), dict(
        name="pods",
        singularName="",
        namespaced=True,
        kind="Pod",
        verbs=["create", "delete", "get", "list", "patch", "update", "watch"],
        shortNames=["po"],
        categories=["all"]
    ), dict(
        name="pods/exec",
        singularName="",
        namespaced=True,
        kind="Pod",
        verbs=[]
    ), dict(
        name="pods/log",
        singularName="",
        namespaced=True,
        kind="Pod",
        verbs=["get"],
    ), dict(
        name="pods/status",
        singularName="",
        namespaced=True,
        kind="Pod",
        verbs=["get", "patch", "update"]
        )])
