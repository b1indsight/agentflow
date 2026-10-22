"""Replace one server from a verified GitHub image event.

Default execution checks the target and pulls an immutable image without replacing
the server. Apply stops the old Pod before starting the replacement. Workers use
ordinary reconnect and recovery; no business pause or activity tracking is required.
Removed database columns are rejected before replacement; additive columns are allowed.
No backup/restore, schema fingerprints, wire, worker-set or reconnect gates.
Do not deploy or edit configuration concurrently. No force deletion or rollback.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

# Same immutable PostgreSQL 17 image used by deploy/docker/compose.yaml.
POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:f02121de6f74d30d8a94cd1d9584125e2178d7e6c377d8130112d4e52d867995"
)


class Update:
    """Own target identity, preflight checks and stop-before-start replacement."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.column_check = Path(__file__).with_name("column-check.py").read_text()
        self.kubectl = [
            "kubectl",
            "--context",
            args.context,
            "--namespace",
            args.namespace,
            "--request-timeout=30s",
        ]
        identity = f"{args.context}/{args.namespace}/{args.workload}"
        self.directory = args.state_dir / hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.record = self.directory / "operation.json"
        self.report: dict = {}

    def run(self, *args: str, data: str | None = None) -> str:
        result = subprocess.run(
            [*self.kubectl, *args],
            input=data,
            text=True,
            capture_output=True,
            check=False,
            timeout=45,
        )
        if result.returncode:
            raise RuntimeError(f"kubectl {args[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout

    def get(self, resource: str) -> dict:
        return json.loads(self.run("get", resource, "-o", "json"))

    def schema_snapshot(self, pod: dict) -> dict:
        live = self.get(f"pod/{pod['metadata']['name']}")
        if live["metadata"]["uid"] != pod["metadata"]["uid"]:
            raise RuntimeError("Server Pod was replaced during schema inspection")
        result = json.loads(
            self.run(
                "exec",
                "-i",
                pod["metadata"]["name"],
                "-c",
                self.args.container,
                "--",
                "python",
                "-",
                "--snapshot",
                data=self.column_check,
            )
        )
        if not isinstance(result, dict):
            raise TypeError("schema inspection returned invalid JSON")
        return result

    @staticmethod
    def ready(pod: dict) -> bool:
        return not pod["metadata"].get("deletionTimestamp") and any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in pod.get("status", {}).get("conditions", [])
        )

    def pods(self, workload: dict) -> list[dict]:
        uid = workload["metadata"]["uid"]
        return [
            pod
            for pod in self.get("pods")["items"]
            if any(
                ref.get("controller") and ref["uid"] == uid
                for ref in pod["metadata"].get("ownerReferences", [])
            )
        ]

    @staticmethod
    def status(pod: dict, container: str) -> dict:
        return next(
            row
            for row in pod.get("status", {}).get("containerStatuses", [])
            if row["name"] == container
        )

    def current(self, original: dict, expected: dict) -> dict:
        current = self.get(self.args.workload)
        if current["metadata"]["uid"] != original["metadata"]["uid"] or current["spec"] != expected:
            raise RuntimeError("StatefulSet changed concurrently; refusing to overwrite it")
        return current

    def patch(self, obj: dict, changes: list[dict]) -> dict:
        tests = [
            {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": obj["metadata"]["resourceVersion"],
            },
        ]
        return json.loads(
            self.run(
                "patch",
                self.args.workload,
                "--type=json",
                "-p",
                json.dumps(tests + changes),
                "-o",
                "json",
            )
        )

    def save(self, stage: str) -> None:
        self.report["stage"] = stage
        temporary = self.directory / "operation.tmp"
        with temporary.open("w") as stream:
            temporary.chmod(0o600)
            json.dump(self.report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.record)
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        print(f"Stage: {stage}", flush=True)

    def wait[T](self, check: Callable[[], T | None], description: str) -> T:
        deadline = time.monotonic() + self.args.timeout
        while True:
            result = check()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for {description}")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def preflight(self) -> tuple[dict, dict, int]:
        workload = self.get(self.args.workload)
        if workload.get("kind") != "StatefulSet" or workload["metadata"].get("deletionTimestamp"):
            raise RuntimeError("Expected a live StatefulSet")
        if workload["spec"].get("replicas", 1) != 1:
            raise RuntimeError("Expected exactly one server replica")
        if (
            workload["spec"]
            .get("persistentVolumeClaimRetentionPolicy", {})
            .get("whenScaled", "Retain")
            != "Retain"
        ):
            raise RuntimeError("Scaling down must retain PVCs")
        for hpa in self.get("horizontalpodautoscalers")["items"]:
            target = hpa["spec"]["scaleTargetRef"]
            if target["kind"] == "StatefulSet" and target["name"] == workload["metadata"]["name"]:
                raise RuntimeError("Autoscaling must not manage this StatefulSet")
        node = self.get(f"node/{self.args.node}")
        info = node["status"]["nodeInfo"]
        if not self.ready(node) or node.get("spec", {}).get("unschedulable"):
            raise RuntimeError("Target node must be Ready and schedulable")
        if (info["operatingSystem"], info["architecture"]) != ("linux", "amd64"):
            raise RuntimeError("Published image requires a Linux/amd64 node")
        containers = workload["spec"]["template"]["spec"]["containers"]
        indices = [i for i, item in enumerate(containers) if item["name"] == self.args.container]
        if len(indices) != 1:
            raise RuntimeError("Application container is ambiguous")
        index = indices[0]
        if containers[index].get("readinessProbe", {}).get("httpGet", {}).get("path") != "/readyz":
            raise RuntimeError("Application readinessProbe must check /readyz")
        if workload["spec"]["template"]["spec"].get("terminationGracePeriodSeconds", 30) < 45:
            raise RuntimeError("Server termination grace must be at least 45 seconds")
        pods = self.pods(workload)
        if len(pods) != 1 or not self.ready(pods[0]):
            raise RuntimeError("Expected exactly one healthy server Pod")
        if pods[0]["spec"].get("nodeName") != self.args.node:
            raise RuntimeError("Server Pod is not on the specified node")
        return workload, pods[0], index

    def probe_image(self, old_pod: dict, schema: dict) -> str:
        """Inspect columns on disposable PostgreSQL, without live credentials or data."""
        name = "agentflow-image-check-" + uuid.uuid4().hex[:12]
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name},
            "spec": {
                "nodeName": self.args.node,
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "serviceAccountName": old_pod["spec"].get("serviceAccountName", "default"),
                "imagePullSecrets": old_pod["spec"].get("imagePullSecrets", []),
                "activeDeadlineSeconds": max(180, self.args.timeout + 60),
                "terminationGracePeriodSeconds": 5,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "fsGroup": 70,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumes": [{"name": value, "emptyDir": {}} for value in ("data", "socket", "tmp")],
                "containers": [
                    {
                        "name": "check",
                        "image": self.args.image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "-c", self.column_check, json.dumps(schema)],
                        "securityContext": {
                            "readOnlyRootFilesystem": True,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [{"name": "socket", "mountPath": "/socket"}],
                        "resources": {
                            "requests": {"cpu": "20m", "memory": "32Mi"},
                            "limits": {"cpu": "500m", "memory": "128Mi"},
                        },
                    },
                    {
                        "name": "postgres",
                        "image": POSTGRES_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        # Bypass the entrypoint's temporary init server: the checker
                        # must connect only after the final socket-only server starts.
                        "command": [
                            "/bin/sh",
                            "-ec",
                            (
                                "initdb -D /data/pg -U postgres -A trust --encoding=UTF8 >/dev/null; "
                                "exec postgres -D /data/pg -c listen_addresses= "
                                "-c unix_socket_directories=/socket -c shared_buffers=16MB"
                            ),
                        ],
                        "securityContext": {
                            "runAsUser": 70,
                            "runAsGroup": 70,
                            "readOnlyRootFilesystem": True,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                            {"name": "socket", "mountPath": "/socket"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "1", "memory": "256Mi"},
                        },
                    },
                ],
            },
        }
        created = succeeded = False
        try:
            self.run("create", "-f", "-", data=json.dumps(manifest))
            created = True

            def finished():
                pod = self.get(f"pod/{name}")
                if pod.get("status", {}).get("phase") == "Failed":
                    raise RuntimeError(f"Candidate image check failed; inspect pod/{name}")
                statuses = pod.get("status", {}).get("containerStatuses", [])
                for row in statuses:
                    terminated = row.get("state", {}).get("terminated")
                    if row["name"] == "postgres" and terminated is not None:
                        raise RuntimeError(
                            f"Disposable column-check database stopped; inspect pod/{name}"
                        )
                    if row["name"] == "check" and terminated is not None:
                        if terminated.get("exitCode") != 0:
                            raise RuntimeError(
                                f"Candidate column inspection failed; inspect pod/{name}"
                            )
                        return pod
                return None

            pod = self.wait(finished, "candidate image pull and database column inspection")
            result = json.loads(self.run("logs", name, "-c", "check"))
            removed = result.get("removed_columns")
            if (
                not isinstance(removed, list)
                or any(
                    not isinstance(column, list)
                    or len(column) != 2
                    or any(not isinstance(part, str) or not part for part in column)
                    for column in removed
                )
                or result.get("checked_columns") != len(schema["columns"])
                or not schema["columns"]
            ):
                raise RuntimeError("Incomplete database column inspection")
            if removed:
                raise RuntimeError("Database column removal detected: " + json.dumps(removed))
            image_id = self.status(pod, "check").get("imageID")
            if not image_id:
                raise RuntimeError("Candidate image identity is missing")
            succeeded = True
            return image_id
        finally:
            if succeeded:
                self.run("delete", f"pod/{name}", "--wait=true", "--timeout=30s")
            elif created:
                print(f"Image-check Pod retained for diagnostics: {name}", file=sys.stderr)

    def execute(self) -> None:
        if self.record.exists() and json.loads(self.record.read_text()).get("status") != "complete":
            raise RuntimeError(f"Unfinished operation: inspect {self.record}; no automatic retry")
        original, old_pod, index = self.preflight()
        print(f"Target: {self.args.context}/{self.args.namespace}/{self.args.workload}", flush=True)
        schema = self.schema_snapshot(old_pod)
        image_id = self.probe_image(old_pod, schema)
        _, pod, _ = self.preflight()
        self.current(original, original["spec"])
        if pod["metadata"]["uid"] != old_pod["metadata"]["uid"]:
            raise RuntimeError("Old Pod changed during image validation")
        print(
            "Image, target and column-removal checks passed; backup/restore and broad compatibility gates disabled."
        )
        if not self.args.apply:
            print("CHECK PASSED: no server replaced.")
            return
        old_container = original["spec"]["template"]["spec"]["containers"][index]
        if (
            old_container["image"] == self.args.image
            and old_container.get("imagePullPolicy") == "IfNotPresent"
            and self.status(old_pod, self.args.container).get("imageID") == image_id
        ):
            print("ALREADY CURRENT: no server replacement needed.")
            return

        self.report = {
            "status": "incomplete",
            "context": self.args.context,
            "namespace": self.args.namespace,
            "workload": self.args.workload,
            "container": self.args.container,
            "workload_uid": original["metadata"]["uid"],
            "old_pod_uid": old_pod["metadata"]["uid"],
            "old_image": old_container["image"],
            "old_pull_policy": old_container.get("imagePullPolicy"),
            "new_image": self.args.image,
            "new_image_id": image_id,
            "backup_policy": "disabled",
            "compatibility_policy": "column-removal-only",
        }
        self.save("stopping-old-pod")
        current = self.current(original, original["spec"])
        stopped = self.patch(current, [{"op": "add", "path": "/spec/replicas", "value": 0}])
        expected = copy.deepcopy(original["spec"])
        expected["replicas"] = 0

        def gone():
            obj = self.current(original, expected)
            acknowledged = (
                obj.get("status", {}).get("observedGeneration", 0)
                >= stopped["metadata"]["generation"]
            )
            return True if acknowledged and not self.pods(obj) else None

        self.wait(gone, "normal deletion of the old server Pod")
        self.save("starting-new-pod")
        current = self.current(original, expected)
        prefix = f"/spec/template/spec/containers/{index}"
        started = self.patch(
            current,
            [
                {"op": "add", "path": f"{prefix}/image", "value": self.args.image},
                {"op": "add", "path": f"{prefix}/imagePullPolicy", "value": "IfNotPresent"},
                {"op": "add", "path": "/spec/replicas", "value": 1},
            ],
        )

        def ready():
            workload = self.current(original, started["spec"])
            pods = self.pods(workload)
            if len(pods) > 1:
                raise RuntimeError("Multiple server Pods appeared")
            if not pods or not self.ready(pods[0]):
                return None
            pod = pods[0]
            if (
                pod["metadata"]["uid"] == old_pod["metadata"]["uid"]
                or pod["spec"].get("nodeName") != self.args.node
            ):
                raise RuntimeError("Replacement Pod identity or node mismatch")
            container = next(
                row for row in pod["spec"]["containers"] if row["name"] == self.args.container
            )
            if (
                container["image"] != self.args.image
                or self.status(pod, self.args.container).get("imageID") != image_id
            ):
                raise RuntimeError("Running image does not match the checked candidate")
            return pod

        new_pod = self.wait(ready, "replacement /readyz and matching runtime image identity")
        self.report["new_pod_uid"] = new_pod["metadata"]["uid"]
        self.current(original, started["spec"])
        self.report["status"] = "complete"
        self.save("ready")
        print("UPDATE COMPLETE: server ready; workers reconnect normally; drain flags unchanged.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("context", "namespace", "workload", "container", "node", "image"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/agentflow-deploy"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if any(
        not getattr(args, name).strip() for name in ("context", "namespace", "container", "node")
    ):
        parser.error("context, namespace, container and node must not be empty")
    if not re.fullmatch(r"statefulset/[a-z0-9][a-z0-9.-]*", args.workload):
        parser.error("--workload must be statefulset/NAME")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9/_.-]+@sha256:[a-f0-9]{64}", args.image):
        parser.error("--image must be a fixed ghcr.io/REPOSITORY@sha256:DIGEST, not a tag")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    os.umask(0o077)
    update = Update(args)
    try:
        if args.apply:
            update.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (update.directory / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                update.execute()
        else:
            update.execute()
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        StopIteration,
        subprocess.TimeoutExpired,
        KeyboardInterrupt,
    ) as error:
        print(f"Update stopped: {type(error).__name__}: {error}", file=sys.stderr)
        if update.report:
            print(
                f"Inspect {update.record}. No rollback, force-delete or post-cutover failure resume.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
