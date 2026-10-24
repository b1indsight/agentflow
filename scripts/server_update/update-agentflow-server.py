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
        self.publication: dict | None = None

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

    def probe(self) -> dict | None:
        raw = self.run(
            "get", f"pod/{self.report['probe_name']}", "-o", "json", "--ignore-not-found"
        )
        if not raw.strip():
            return None
        pod = json.loads(raw)
        if (
            pod["metadata"].get("annotations", {}).get("agentflow.io/update-owner")
            != self.report["probe_owner"]
        ):
            raise RuntimeError("Candidate Pod belongs to another operation")
        if self.report.get("probe_uid") not in (None, pod["metadata"]["uid"]):
            raise RuntimeError("Candidate Pod was replaced concurrently")
        checks = [c for c in pod["spec"]["containers"] if c["name"] == "check"]
        if (
            pod["spec"].get("nodeName") != self.args.node
            or len(checks) != 1
            or checks[0]["image"] != self.report["new_image"]
            or checks[0].get("command")
            != ["python", "-c", self.column_check, json.dumps(self.report["schema"])]
        ):
            raise RuntimeError("Candidate Pod inputs differ from recorded validation")
        return pod

    def cleanup_probe(self) -> None:
        """Delete only our recorded candidate UID; a lost response is reconciled by GET."""
        if not self.report.get("probe_name"):
            return
        pod = self.probe()
        if pod is not None:
            uri = f"/api/v1/namespaces/{self.args.namespace}/pods/{self.report['probe_name']}"
            options = {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": pod["metadata"]["uid"]},
            }
            try:
                self.run("delete", "--raw", uri, "-f", "-", data=json.dumps(options))
            except (RuntimeError, subprocess.TimeoutExpired):
                if self.probe() is not None:
                    raise
            self.wait(lambda: True if self.probe() is None else None, "candidate Pod deletion")
        self.report.pop("probe_uid", None)

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
        try:
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
        except (RuntimeError, subprocess.TimeoutExpired):
            current = self.get(self.args.workload)
            if current["metadata"]["uid"] != obj["metadata"]["uid"]:
                raise
            for change in changes:
                value = current
                for part in change["path"].strip("/").split("/"):
                    value = value[int(part)] if isinstance(value, list) else value[part]
                if value != change["value"]:
                    raise
            return current

    def save(self, stage: str) -> None:
        self.report["stage"] = stage
        if not self.args.apply:
            return
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

    def target(self) -> tuple[dict, int]:
        """Checks shared by fresh operations and recovery; no healthy old Pod required."""
        workload = self.get(self.args.workload)
        if workload.get("kind") != "StatefulSet" or workload["metadata"].get("deletionTimestamp"):
            raise RuntimeError("Expected a live StatefulSet")
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
        if node.get("spec", {}).get("unschedulable"):
            raise RuntimeError("Target node must be schedulable")
        containers = workload["spec"]["template"]["spec"]["containers"]
        indices = [i for i, item in enumerate(containers) if item["name"] == self.args.container]
        if len(indices) != 1:
            raise RuntimeError("Application container is ambiguous")
        return workload, indices[0]

    def preflight(self) -> tuple[dict, dict, int]:
        workload, index = self.target()
        if workload["spec"].get("replicas", 1) != 1:
            raise RuntimeError("Expected exactly one server replica")
        pods = self.pods(workload)
        if len(pods) != 1:
            raise RuntimeError("Expected exactly one server Pod")
        if pods[0]["spec"].get("nodeName") != self.args.node:
            raise RuntimeError("Server Pod is not on the specified node")
        return workload, pods[0], index

    def probe_image(self, old_pod: dict, schema: dict) -> str:
        """Inspect columns on disposable PostgreSQL, without live credentials or data."""
        name = self.report["probe_name"]
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "annotations": {"agentflow.io/update-owner": self.report["probe_owner"]},
            },
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
            existing = self.probe()
            if existing is not None and (
                existing.get("status", {}).get("phase") == "Failed"
                or any(
                    row.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0
                    for row in existing.get("status", {}).get("containerStatuses", [])
                )
            ):
                self.cleanup_probe()
                existing = None
            if existing is None:
                try:
                    self.run("create", "-f", "-", data=json.dumps(manifest))
                except (RuntimeError, subprocess.TimeoutExpired):
                    if self.probe() is None:
                        raise
            existing = self.probe()
            if existing is None:
                raise RuntimeError("Candidate Pod disappeared")
            self.report["probe_uid"] = existing["metadata"]["uid"]
            self.save("checking-image")
            created = True

            def finished():
                pod = self.probe()
                if pod is None:
                    raise RuntimeError("Candidate Pod disappeared during inspection")
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
            self.report["new_image_id"] = image_id
            self.save("image-checked")
            succeeded = True
            return image_id
        finally:
            if succeeded:
                self.cleanup_probe()
            elif created:
                print(f"Image-check Pod retained for diagnostics: {name}", file=sys.stderr)

    def spec_signature(self, workload: dict, index: int) -> str:
        """Guard unmanaged configuration without writing environment values to disk."""
        spec = copy.deepcopy(workload["spec"])
        spec.pop("replicas", None)
        container = spec["template"]["spec"]["containers"][index]
        container.pop("image", None)
        container.pop("imagePullPolicy", None)
        return hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def observed(self) -> tuple[dict, int, str]:
        workload, index = self.target()
        if (
            workload["metadata"]["uid"] != self.report["workload_uid"]
            or self.spec_signature(workload, index) != self.report["spec_signature"]
        ):
            raise RuntimeError("StatefulSet changed concurrently; refusing to overwrite it")
        container = workload["spec"]["template"]["spec"]["containers"][index]
        replicas = workload["spec"].get("replicas", 1)
        if (
            container["image"] == self.args.image
            and container.get("imagePullPolicy") == "IfNotPresent"
            and replicas == 1
        ):
            return workload, index, "target"
        if (container["image"], container.get("imagePullPolicy")) == (
            self.report["old_image"],
            self.report["old_pull_policy"],
        ) and replicas in (0, 1):
            return workload, index, "stopped" if replicas == 0 else "old"
        raise RuntimeError(
            "Deployment image or replica configuration conflicts with recorded intent"
        )

    def finish_ready(self) -> None:
        def ready():
            workload, _, state = self.observed()
            if state != "target":
                raise RuntimeError("Target image changed while waiting for readiness")
            if (
                workload.get("status", {}).get("observedGeneration", 0)
                < workload["metadata"]["generation"]
            ):
                return None
            pods = self.pods(workload)
            if len(pods) > 1:
                raise RuntimeError("Multiple server Pods appeared")
            if not pods or not self.ready(pods[0]):
                return None
            pod = pods[0]
            if pod["spec"].get("nodeName") != self.args.node or (
                self.report["stop_intent"] and pod["metadata"]["uid"] == self.report["old_pod_uid"]
            ):
                raise RuntimeError("Replacement Pod identity or node mismatch")
            container = next(
                row for row in pod["spec"]["containers"] if row["name"] == self.args.container
            )
            if (
                container["image"] != self.args.image
                or self.status(pod, self.args.container).get("imageID")
                != self.report["new_image_id"]
            ):
                raise RuntimeError("Running image does not match the checked candidate")
            return pod

        pod = self.wait(ready, "replacement readiness and matching runtime image identity")
        self.cleanup_probe()
        self.report.update(status="complete", new_pod_uid=pod["metadata"]["uid"])
        self.save("ready")
        print("UPDATE COMPLETE: server ready; workers reconnect normally; drain flags unchanged.")

    def read_record(self) -> dict:
        """Reject damaged authority before it can select recovery or be replaced."""
        if self.record.is_symlink():
            raise RuntimeError("Operation record must not be a symbolic link")
        record = json.loads(self.record.read_text())
        text_fields = (
            "context",
            "namespace",
            "workload",
            "container",
            "node",
            "workload_uid",
            "old_pod_uid",
            "old_image",
            "new_image",
            "probe_name",
            "probe_owner",
        )
        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
            or record.get("status") not in {"incomplete", "complete"}
            or type(record.get("stop_intent")) is not bool
            or any(not isinstance(record.get(k), str) or not record[k] for k in text_fields)
            or record.get("old_pull_policy") not in {None, "Always", "Never", "IfNotPresent"}
            or "old_pull_policy" not in record
            or "schema" not in record
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("spec_signature", ""))
            or not re.fullmatch(r"[a-f0-9]{32}", record["probe_owner"])
            or record["probe_name"] != "agentflow-image-check-" + record["probe_owner"][:12]
            or not re.fullmatch(r"ghcr\.io/[a-z0-9/_.-]+@sha256:[a-f0-9]{64}", record["new_image"])
        ):
            raise RuntimeError("Invalid operation record; refusing to discard authoritative state")
        if (record["stop_intent"] or record["status"] == "complete") and not (
            isinstance(record.get("new_image_id"), str) and record["new_image_id"]
        ):
            raise RuntimeError("Durable image validation is missing")
        publication = record.get("publication")
        if publication is not None and (
            not isinstance(publication, dict)
            or any(
                type(publication.get(k)) is not int or publication[k] < 1
                for k in ("run_id", "attempt", "run_number")
            )
            or not re.fullmatch(r"[a-f0-9]{40}", publication.get("sha", ""))
            or publication.get("image") != record["new_image"]
        ):
            raise RuntimeError("Invalid publication authorization in operation record")
        return record

    def execute(self) -> None:
        """Converge from Kubernetes state, treating journal stages only as durable intent."""
        if self.record.exists():
            self.report = self.read_record()
            if any(
                self.report[key] != getattr(self.args, key)
                for key in ("context", "namespace", "workload", "container", "node")
            ):
                raise RuntimeError("Operation belongs to a different deployment target")
            if self.report["new_image"] != self.args.image:
                if self.report["status"] != "complete" and self.report["stop_intent"]:
                    raise RuntimeError("Another image has an unfinished deployment")
                self.cleanup_probe()
                self.report = {}
            elif not self.args.apply and self.report["status"] != "complete":
                raise RuntimeError("Use the apply command to reconcile an existing operation")
            else:
                self.save("resuming")

        if self.report:
            workload, index, state = self.observed()
            if self.report.get("new_image_id") and state == "target":
                self.finish_ready()
                return

        if not self.report or not self.report["stop_intent"]:
            original, old_pod, index = self.preflight()
            schema = self.schema_snapshot(old_pod)
            if not self.report:
                container = original["spec"]["template"]["spec"]["containers"][index]
                owner = uuid.uuid4().hex
                self.report = {
                    "version": 1,
                    "status": "incomplete",
                    "stop_intent": False,
                    **{
                        key: getattr(self.args, key)
                        for key in ("context", "namespace", "workload", "container", "node")
                    },
                    "workload_uid": original["metadata"]["uid"],
                    "old_pod_uid": old_pod["metadata"]["uid"],
                    "old_image": container["image"],
                    "old_pull_policy": container.get("imagePullPolicy"),
                    "new_image": self.args.image,
                    "spec_signature": self.spec_signature(original, index),
                    "schema": schema,
                    "publication": self.publication,
                    "probe_name": "agentflow-image-check-" + owner[:12],
                    "probe_owner": owner,
                    "backup_policy": "disabled",
                    "compatibility_policy": "column-removal-only",
                }
                self.save("checking-image")
            elif (
                schema != self.report["schema"]
                or old_pod["metadata"]["uid"] != self.report["old_pod_uid"]
            ):
                # Before stop intent, a normal controller restart can refresh the baseline.
                self.cleanup_probe()
                self.report["schema"] = schema
                self.report["old_pod_uid"] = old_pod["metadata"]["uid"]
                self.report.pop("new_image_id", None)
                self.save("checking-image")
            if not self.report.get("new_image_id"):
                self.probe_image(old_pod, schema)
            else:
                self.cleanup_probe()
            self.current(original, original["spec"])
            workload, index, state = self.observed()
            if not self.args.apply:
                print("CHECK PASSED: no server replaced.")
                return
            if state == "target":
                self.finish_ready()
                return
            self.report["stop_intent"] = True
            self.save("stopping-old-pod")

        if not self.report.get("new_image_id"):
            raise RuntimeError("Durable image validation is missing")
        workload, index, state = self.observed()
        if state == "old":
            pods = self.pods(workload)
            if len(pods) != 1 or pods[0]["metadata"]["uid"] != self.report["old_pod_uid"]:
                raise RuntimeError("Old Pod identity changed before scaling down")
            self.patch(workload, [{"op": "add", "path": "/spec/replicas", "value": 0}])
            state = "stopped"
        if state == "stopped":

            def gone():
                obj, _, phase = self.observed()
                if phase != "stopped":
                    raise RuntimeError("Workload changed during normal old Pod deletion")
                acknowledged = (
                    obj.get("status", {}).get("observedGeneration", 0)
                    >= obj["metadata"]["generation"]
                )
                return True if acknowledged and not self.pods(obj) else None

            self.wait(gone, "normal deletion of the old server Pod")
            self.save("starting-new-pod")
            workload, index, _ = self.observed()
            prefix = f"/spec/template/spec/containers/{index}"
            self.patch(
                workload,
                [
                    {"op": "add", "path": f"{prefix}/image", "value": self.args.image},
                    {"op": "add", "path": f"{prefix}/imagePullPolicy", "value": "IfNotPresent"},
                    {"op": "add", "path": "/spec/replicas", "value": 1},
                ],
            )
        self.finish_ready()

    def apply(self) -> None:
        """Both the standalone CLI and queue use the same host deployment exclusion."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.execute()


def configure_target(parser: argparse.ArgumentParser) -> None:
    for name in ("context", "namespace", "workload", "container", "node"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/agentflow-deploy"
    )


def validate_target(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if any(
        not getattr(args, name).strip() for name in ("context", "namespace", "container", "node")
    ):
        parser.error("context, namespace, container and node must not be empty")
    if not re.fullmatch(r"statefulset/[a-z0-9][a-z0-9.-]*", args.workload):
        parser.error("--workload must be statefulset/NAME")
    if args.timeout < 1:
        parser.error("--timeout must be positive")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    configure_target(parser)
    parser.add_argument("--image", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    validate_target(parser, args)
    if not re.fullmatch(r"ghcr\.io/[a-z0-9/_.-]+@sha256:[a-f0-9]{64}", args.image):
        parser.error("--image must be a fixed ghcr.io/REPOSITORY@sha256:DIGEST, not a tag")
    os.umask(0o077)
    update = Update(args)
    try:
        update.apply()
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
                f"Inspect {update.record}; retry the same image to resume. No rollback or force-delete.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
