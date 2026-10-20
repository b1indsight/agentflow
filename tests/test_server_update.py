"""Exercise the standalone CLI with simulated process boundaries, not a real cluster."""

import copy
import io
import itertools
import json
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from scripts import update_server


class Commands:
    def __init__(self, kind: str, failure: str = ""):
        self.failure = failure
        self.calls: list[tuple[str, ...]] = []
        self.workload: dict = {
            "kind": kind,
            "metadata": {
                "uid": "workload",
                "name": "server",
                "resourceVersion": "1",
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "old:1",
                                "imagePullPolicy": "IfNotPresent",
                                "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8000}},
                            },
                            {"name": "gateway", "image": "gateway:1"},
                        ]
                    }
                },
            },
            "status": {"observedGeneration": 1},
        }
        if failure == "pvc-delete":
            self.workload["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": "Delete"}
        self.image = ""
        self.stopped_reads = 0
        self.old_gone = False

    def pod(self) -> dict:
        new = bool(self.image and self.workload["metadata"]["generation"] > 2)
        return {
            "metadata": {
                "uid": "new" if new else "old",
                "ownerReferences": [
                    {
                        "uid": "rs" if self.workload["kind"] == "Deployment" else "workload",
                        "controller": True,
                    },
                ],
            },
            # Admission can reorder containers; the application is identified by name.
            "spec": {
                "nodeName": "node",
                "containers": list(
                    reversed(copy.deepcopy(self.workload["spec"]["template"]["spec"]["containers"]))
                ),
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False" if new and self.failure == "readiness" else "True",
                    }
                ],
                "containerStatuses": [
                    {
                        "name": "app",
                        "containerID": "containerd://running",
                        "imageID": "sha256:wrong"
                        if new and self.failure == "running-digest"
                        else "docker-pullable://test-server@sha256:manifest",
                    }
                ],
            },
        }

    def kube(self, args: tuple[str, ...]) -> dict:
        operation, resource = args[:2]
        if operation == "patch":
            changes = json.loads(args[args.index("-p") + 1])
            for patch in changes:
                path = patch["path"].split("/")[1:]
                target = self.workload
                for key in path[:-1]:
                    target = target[int(key)] if isinstance(target, list) else target[key]
                if patch["op"] == "test":
                    assert target[path[-1]] == patch["value"]
                else:
                    if patch["path"].endswith("/image"):
                        assert self.old_gone, "must wait for old Pod deletion before replacement"
                    target[path[-1]] = patch["value"]
            self.workload["metadata"]["generation"] += 1
            self.workload["metadata"]["resourceVersion"] = str(
                self.workload["metadata"]["generation"]
            )
            self.workload["status"]["observedGeneration"] = self.workload["metadata"]["generation"]
            return self.workload
        if resource == "nodes":
            return {
                "items": [
                    {
                        "metadata": {"name": "node"},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "nodeInfo": {
                                "operatingSystem": "linux",
                                "architecture": "amd64",
                                "containerRuntimeVersion": "containerd:1.7",
                            },
                        },
                    }
                ]
            }
        if "/" in resource:
            return self.workload
        if resource == "horizontalpodautoscalers":
            return {
                "items": [
                    {"spec": {"scaleTargetRef": {"kind": self.workload["kind"], "name": "server"}}}
                ]
                if self.failure == "hpa"
                else []
            }
        if resource == "replicasets":
            return {
                "items": [
                    {
                        "metadata": {
                            "uid": "rs",
                            "ownerReferences": [{"uid": "workload", "controller": True}],
                        }
                    }
                ]
            }
        assert resource == "pods"
        if self.workload["spec"]["replicas"] == 0:
            self.stopped_reads += 1
            if self.failure == "old-stuck" or self.stopped_reads == 1:
                return {"items": [self.pod()]}
            self.old_gone = True
            return {"items": []}
        return {"items": [self.pod()]}

    def __call__(self, command: tuple[str, ...], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(command)
        tool, *args = command
        if tool == "sudo":
            assert args[0] == "-n" and args[1] in {"ctr", "crictl"}
            tool, *args = args[1:]
        output = ""
        code = 0
        if tool == "kubectl":
            assert args[:4] == ["--context", "test-context", "--namespace", "test-namespace"]
            output = json.dumps(self.kube(tuple(args[5:-2])))
        elif tool == "git":
            if "rev-parse" in args:
                output = "a" * 40
            elif "archive" in args:
                archive = next(
                    arg.removeprefix("--output=") for arg in args if arg.startswith("--output=")
                )
                with tarfile.open(archive, "w") as bundle:
                    entry = tarfile.TarInfo("Dockerfile")
                    entry.size = 12
                    bundle.addfile(entry, io.BytesIO(b"FROM scratch"))
        elif tool == "docker":
            if args[0] == "info":
                output = "linux"
            elif args[0] == "build":
                self.image = args[args.index("--tag") + 1]
                code = int(self.failure == "build")
                if self.failure == "concurrent":
                    self.workload["spec"]["template"]["spec"]["containers"][1]["image"] = (
                        "new-gateway"
                    )
            elif args[:2] == ["image", "inspect"]:
                output = "sha256:build"
        elif tool == "ctr":
            assert args[:4] == [
                "--address",
                "/run/containerd/containerd.sock",
                "--namespace",
                "k8s.io",
            ]
            code = int(self.failure == "import" and "import" in args)
            code |= int(self.failure == "runtime" and "info" in args)
        elif tool == "crictl":
            output = json.dumps(
                {
                    "status": {
                        "id": "sha256:wrong"
                        if self.failure == "import-digest" or args[-1] == "sha256:wrong"
                        else "sha256:build",
                        "repoDigests": [],
                    }
                }
            )
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, code, stdout=output, stderr="injected failure")


@pytest.fixture
def invoke(monkeypatch, tmp_path):
    def call(commands: Commands, *flags: str) -> int:
        ticks = itertools.count()
        monkeypatch.setattr(
            update_server,
            "time",
            SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None),
        )
        monkeypatch.setattr(update_server.subprocess, "run", commands)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "update_server.py",
                "--context",
                "test-context",
                "--namespace",
                "test-namespace",
                "--workload",
                f"{commands.workload['kind'].lower()}/server",
                "--container",
                "app",
                "--node",
                "node",
                "--image",
                "test-server",
                "--timeout",
                "4",
                "--state-dir",
                str(tmp_path),
                *flags,
            ],
        )
        return update_server.main()

    return call


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_update_builds_before_stop_and_waits_for_old_pod(invoke, tmp_path, kind):
    commands = Commands(kind)
    assert invoke(commands, "--apply", "--maintenance-confirmed", "--fetch", "--sudo") == 0
    report = json.loads(next(tmp_path.glob("*/operation.json")).read_text())
    assert report["status"] == "complete" and report["new_pod_uid"] == "new"
    calls = commands.calls
    build = next(i for i, cmd in enumerate(calls) if cmd[:2] == ("docker", "build"))
    imported = next(i for i, cmd in enumerate(calls) if "import" in cmd)
    patches = [i for i, cmd in enumerate(calls) if "patch" in cmd]
    assert build < imported < patches[0] < patches[1]
    containers = commands.workload["spec"]["template"]["spec"]["containers"]
    assert containers[0]["imagePullPolicy"] == "Never"
    assert containers[1]["image"] == "gateway:1"
    assert commands.stopped_reads >= 2
    assert next(tmp_path.glob("*/operation.json")).stat().st_mode & 0o777 == 0o600


def test_default_preflight_has_no_side_effects(invoke, tmp_path):
    commands = Commands("Deployment")
    assert invoke(commands) == 0
    assert not list(tmp_path.iterdir())
    assert not any(
        token in cmd
        for cmd in commands.calls
        for token in ("patch", "build", "import", "fetch", "archive")
    )


@pytest.mark.parametrize(
    "failure",
    [
        "build",
        "import",
        "import-digest",
        "runtime",
        "hpa",
        "pvc-delete",
        "concurrent",
        "old-stuck",
        "readiness",
        "running-digest",
    ],
)
def test_failures_stop_without_rollback_or_blind_retry(invoke, tmp_path, failure):
    commands = Commands("StatefulSet", failure)
    assert invoke(commands, "--apply", "--maintenance-confirmed") == 1
    patches = [cmd for cmd in commands.calls if "patch" in cmd]
    assert len(patches) == (
        1 if failure == "old-stuck" else 2 if failure in {"readiness", "running-digest"} else 0
    )
    if patches:
        report = json.loads(next(tmp_path.glob("*/operation.json")).read_text())
        assert report["status"] == "incomplete"
        commands.calls.clear()
        assert invoke(commands, "--apply", "--maintenance-confirmed") == 1
        assert not commands.calls


def test_apply_requires_explicit_maintenance_confirmation(invoke):
    commands = Commands("Deployment")
    with pytest.raises(SystemExit) as error:
        invoke(commands, "--apply")
    assert error.value.code == 2
    assert not commands.calls


def test_node_mismatch_fails_before_build_or_mutation(invoke):
    commands = Commands("Deployment")
    assert invoke(commands, "--apply", "--maintenance-confirmed", "--node", "other-node") == 1
    assert not any(token in cmd for cmd in commands.calls for token in ("patch", "build", "import"))
