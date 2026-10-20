"""Build a fixed Git revision and replace a single-node Kubernetes server.

Python 3.12+, Linux/amd64, Docker, kubectl, ctr and crictl are required on the node.
Default execution is read-only; --apply also requires --maintenance-confirmed.
See docs/deployment/server-update.md for preparation and failure recovery.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


class Patch(TypedDict):
    op: str
    path: str
    value: str | int


@dataclass(frozen=True)
class Config:
    repo: Path
    context: str
    namespace: str
    workload: str
    container: str
    node: str
    image: str
    revision: str = "origin/main"
    target: str = "serve"
    socket: str = "/run/containerd/containerd.sock"
    state_dir: Path = Path.home() / ".local/state/agentflow-update"
    timeout: int = 300
    build_timeout: int = 1800
    sudo: bool = False
    fetch: bool = False
    apply: bool = False
    maintenance_confirmed: bool = False


class Update:
    def __init__(self, config: Config):
        self.config = config
        identity = f"{config.context}/{config.namespace}/{config.workload}"
        self.directory = config.state_dir / hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.report_path = self.directory / "operation.json"
        self.owners: set[str] = set()
        self.report: dict = {}

    def run(self, *command: str, visible: bool = False, timeout: int | None = None) -> str:
        """Bound every command; avoid printing workload patches containing environment data."""
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=None if visible else subprocess.PIPE,
            stderr=None if visible else subprocess.PIPE,
            timeout=timeout or self.config.timeout,
        )
        if result.returncode:
            raise RuntimeError(f"{command[0]} failed ({result.returncode}): {result.stderr or ''}")
        return result.stdout or ""

    def kube(self, *args: str) -> dict:
        return json.loads(
            self.run(
                "kubectl",
                "--context",
                self.config.context,
                "--namespace",
                self.config.namespace,
                "--request-timeout=30s",
                *args,
                "-o",
                "json",
            )
        )

    def runtime(self, program: str, *args: str, visible: bool = False) -> str:
        prefix = ["sudo", "-n"] if self.config.sudo else []
        if program == "ctr":
            options = ["--address", self.config.socket, "--namespace", "k8s.io"]
        else:
            endpoint = f"unix://{self.config.socket}"
            options = ["--runtime-endpoint", endpoint, "--image-endpoint", endpoint]
        return self.run(*prefix, program, *options, *args, visible=visible)

    def workload(self) -> dict:
        obj = self.kube("get", self.config.workload)
        if obj.get("kind") not in {"Deployment", "StatefulSet"}:
            raise RuntimeError("Only Deployment and StatefulSet are supported")
        if obj["metadata"].get("deletionTimestamp"):
            raise RuntimeError("Workload is being deleted")
        return obj

    def pods(self, workload: dict) -> list[dict]:
        """Follow controller ownership, not labels that may select unrelated workloads."""
        uid = workload["metadata"]["uid"]
        if workload["kind"] == "StatefulSet":
            self.owners.add(uid)
        else:
            for replica_set in self.kube("get", "replicasets")["items"]:
                if self.owned_by(replica_set, {uid}):
                    self.owners.add(replica_set["metadata"]["uid"])
        return [pod for pod in self.kube("get", "pods")["items"] if self.owned_by(pod, self.owners)]

    @staticmethod
    def owned_by(obj: dict, owners: set[str]) -> bool:
        return any(
            ref.get("controller") and ref["uid"] in owners
            for ref in obj["metadata"].get("ownerReferences", [])
        )

    @staticmethod
    def ready(pod: dict) -> bool:
        return not pod["metadata"].get("deletionTimestamp") and any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )

    def container_status(self, pod: dict) -> dict:
        return next(
            item
            for item in pod["status"]["containerStatuses"]
            if item["name"] == self.config.container
        )

    def preflight(self) -> tuple[dict, dict, int]:
        """Reject ambiguous targets and verify the local runtime against the live container."""
        cfg = self.config
        nodes = self.kube("get", "nodes")["items"]
        if len(nodes) != 1 or nodes[0]["metadata"]["name"] != cfg.node:
            raise RuntimeError("Requires exactly one cluster node matching --node")
        if nodes[0].get("spec", {}).get("unschedulable") or not self.ready(nodes[0]):
            raise RuntimeError("Node must be Ready and schedulable")
        info = nodes[0]["status"]["nodeInfo"]
        if (info["operatingSystem"], info["architecture"]) != ("linux", "amd64"):
            raise RuntimeError("Only a Linux/amd64 node is supported")
        if not info["containerRuntimeVersion"].startswith("containerd:"):
            raise RuntimeError("Node must use containerd")
        workload = self.workload()
        if workload["spec"].get("replicas", 1) != 1 or workload["spec"].get("paused"):
            raise RuntimeError("Requires an unpaused workload with exactly one replica")
        retention = workload["spec"].get("persistentVolumeClaimRetentionPolicy", {})
        if retention.get("whenScaled", "Retain") != "Retain":
            raise RuntimeError("Scaling down must retain StatefulSet PVCs, not delete them")
        for hpa in self.kube("get", "horizontalpodautoscalers")["items"]:
            ref = hpa["spec"]["scaleTargetRef"]
            if (ref["kind"], ref["name"]) == (workload["kind"], workload["metadata"]["name"]):
                raise RuntimeError("Remove autoscaling management before updating")
        containers = workload["spec"]["template"]["spec"]["containers"]
        indices = [i for i, item in enumerate(containers) if item["name"] == cfg.container]
        if len(indices) != 1:
            raise RuntimeError("--container must identify one application container")
        index = indices[0]
        if "httpGet" not in containers[index].get("readinessProbe", {}):
            raise RuntimeError("Application container must have an HTTP readinessProbe")
        pods = self.pods(workload)
        if len(pods) != 1 or not self.ready(pods[0]):
            raise RuntimeError("Expected exactly one healthy existing Pod")
        pod = pods[0]
        if pod["spec"].get("nodeName") != cfg.node:
            raise RuntimeError("Existing Pod is not on --node")
        container_id = self.container_status(pod)["containerID"]
        if not container_id.startswith("containerd://"):
            raise RuntimeError("Existing container is not managed by containerd")
        # Finding the live container proves this socket belongs to the target node's CRI.
        self.runtime("ctr", "containers", "info", container_id.removeprefix("containerd://"))
        self.runtime("crictl", "inspecti", containers[index]["image"])
        if self.run("docker", "info", "--format", "{{.OSType}}").strip() != "linux":
            raise RuntimeError("Docker must build Linux containers")
        return workload, pod, index

    def save_report(self, stage: str) -> None:
        self.report["stage"] = stage
        temporary = self.directory / "operation.tmp"
        with temporary.open("w") as output:
            temporary.chmod(0o600)
            output.write(json.dumps(self.report, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.report_path)
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        print(f"Stage: {stage}; report: {self.report_path}", flush=True)

    def patch(self, obj: dict, changes: list[Patch]) -> dict:
        tests: list[Patch] = [
            {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": obj["metadata"]["resourceVersion"],
            },
        ]
        return self.kube(
            "patch", self.config.workload, "--type=json", "-p", json.dumps(tests + changes)
        )

    def unchanged(self, original: dict, expected_spec: dict) -> dict:
        current = self.workload()
        if (
            current["metadata"]["uid"] != original["metadata"]["uid"]
            or current["spec"] != expected_spec
        ):
            raise RuntimeError("Workload changed concurrently; refusing to overwrite it")
        return current

    def wait[T](self, check: Callable[[], T | None], description: str) -> T:
        deadline = time.monotonic() + self.config.timeout
        while True:
            result = check()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for {description}")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def apply(self, original: dict, old_pod: dict, index: int, commit: str) -> None:
        """Build before maintenance; never start the replacement before the old Pod is gone."""
        cfg = self.config
        image = f"{cfg.image}:{commit[:12]}-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="server-update-") as temporary:
            directory = Path(temporary)
            archive = directory / "source.tar"
            source = directory / "source"
            source.mkdir()
            self.run(
                "git", "-C", str(cfg.repo), "archive", "--format=tar", f"--output={archive}", commit
            )
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter="data")
            print(f"Building {image} from {commit}", flush=True)
            self.run(
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--target",
                cfg.target,
                "--label",
                f"org.opencontainers.image.revision={commit}",
                "--tag",
                image,
                str(source),
                visible=True,
                timeout=cfg.build_timeout,
            )
            image_id = self.run("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()
            tar = directory / "image.tar"
            self.run("docker", "save", "--output", str(tar), image, timeout=cfg.build_timeout)
            self.runtime("ctr", "images", "import", str(tar), visible=True)
        imported = json.loads(self.runtime("crictl", "inspecti", image))["status"]
        if imported["id"] != image_id:
            raise RuntimeError("Imported CRI image ID does not match the Docker build")
        # Recheck after the potentially long build, before any Kubernetes mutation.
        current, pod, _ = self.preflight()
        self.unchanged(original, original["spec"])
        if pod["metadata"]["uid"] != old_pod["metadata"]["uid"]:
            raise RuntimeError("Existing Pod was replaced during the build")
        old_container = original["spec"]["template"]["spec"]["containers"][index]
        self.report = {
            "context": cfg.context,
            "namespace": cfg.namespace,
            "workload": cfg.workload,
            "container": cfg.container,
            "workload_uid": original["metadata"]["uid"],
            "old_pod_uid": pod["metadata"]["uid"],
            "old_image": old_container["image"],
            "old_pull_policy": old_container.get("imagePullPolicy"),
            "new_image": image,
            "new_image_id": image_id,
            "commit": commit,
            "status": "incomplete",
        }
        self.save_report("stopping-old-pod")
        stopped = self.patch(current, [{"op": "add", "path": "/spec/replicas", "value": 0}])
        stopped_spec = copy.deepcopy(original["spec"])
        stopped_spec["replicas"] = 0

        def old_stopped() -> bool | None:
            obj = self.unchanged(original, stopped_spec)
            if (
                not self.pods(obj)
                and obj.get("status", {}).get("observedGeneration", 0)
                >= stopped["metadata"]["generation"]
            ):
                return True
            return None

        self.wait(old_stopped, "old Pod deletion and controller acknowledgement")
        self.save_report("starting-new-pod")
        current = self.unchanged(original, stopped_spec)
        prefix = f"/spec/template/spec/containers/{index}"
        changes: list[Patch] = [
            {"op": "add", "path": f"{prefix}/image", "value": image},
            {"op": "add", "path": f"{prefix}/imagePullPolicy", "value": "Never"},
            {"op": "add", "path": "/spec/replicas", "value": 1},
        ]
        started = self.patch(current, changes)
        expected = started["spec"]

        def new_ready() -> dict | None:
            obj = self.unchanged(original, expected)
            pods = self.pods(obj)
            if len(pods) > 1:
                raise RuntimeError("Multiple Pods appeared; manual inspection required")
            if not pods or not self.ready(pods[0]):
                return None
            pod = pods[0]
            if (
                pod["metadata"]["uid"] == old_pod["metadata"]["uid"]
                or pod["spec"].get("nodeName") != cfg.node
            ):
                raise RuntimeError("Replacement Pod identity/node mismatch")
            pod_container = next(
                item for item in pod["spec"]["containers"] if item["name"] == cfg.container
            )
            if pod_container["image"] != image:
                raise RuntimeError("Replacement Pod does not use the candidate image")
            actual = self.container_status(pod)["imageID"]
            actual = actual.removeprefix("containerd://").removeprefix("docker-pullable://")
            # Resolve manifest references via CRI rather than compare them to a config digest.
            running = json.loads(self.runtime("crictl", "inspecti", actual))["status"]
            if running["id"] != image_id:
                raise RuntimeError("Running image digest does not match the imported image")
            return pod

        new_pod = self.wait(new_ready, "replacement HTTP readiness and image identity")
        self.report["new_pod_uid"] = new_pod["metadata"]["uid"]
        self.report["status"] = "complete"
        self.save_report("ready")
        print("Server ready. Check Worker reconnect/recovery, then manually undo your drain.")

    def execute(self) -> None:
        cfg = self.config
        if self.report_path.exists():
            previous = json.loads(self.report_path.read_text())
            if previous.get("status") != "complete":
                raise RuntimeError(
                    f"Unfinished update: inspect {self.report_path}; do not blindly retry"
                )
        if cfg.fetch:
            self.run("git", "-C", str(cfg.repo), "fetch", "origin", "main:refs/remotes/origin/main")
        commit = self.run(
            "git",
            "-C",
            str(cfg.repo),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{cfg.revision}^{{commit}}",
        ).strip()
        workload, pod, index = self.preflight()
        print(f"Target: {cfg.context} / {cfg.namespace} / {cfg.workload} / {cfg.container}")
        print(f"Revision: {commit}; node: {cfg.node}")
        if cfg.apply:
            self.apply(workload, pod, index, commit)
        else:
            print("Preflight passed. No build/import/drain/deployment changes were made.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    for name in ("context", "namespace", "workload", "container", "node", "image"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--revision", default="origin/main")
    parser.add_argument("--target", default="serve")
    parser.add_argument("--socket", default="/run/containerd/containerd.sock")
    parser.add_argument("--state-dir", type=Path, default=Config.state_dir)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--sudo", action="store_true", help="Use sudo -n for ctr/crictl only")
    parser.add_argument("--fetch", action="store_true", help="Fetch origin/main (requires --apply)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm drained/idle Workers, compatible versions and backup",
    )
    cfg = Config(**vars(parser.parse_args()))
    if cfg.apply and not cfg.maintenance_confirmed:
        parser.error("--apply requires --maintenance-confirmed; read the maintenance checklist")
    if cfg.fetch and not cfg.apply:
        parser.error("--fetch requires --apply; default preflight must remain read-only")
    if cfg.timeout <= 0 or cfg.build_timeout <= 0:
        parser.error("Timeouts must be positive")
    resource, separator, name = cfg.workload.partition("/")
    if resource not in {"deployment", "statefulset"} or not separator or not name or "/" in name:
        parser.error("--workload must be deployment/NAME or statefulset/NAME (no aliases)")
    if ":" in cfg.image.rsplit("/", 1)[-1] or "@" in cfg.image:
        parser.error("--image is a repository without a tag/digest (registry ports are allowed)")
    update = Update(cfg)
    try:
        if cfg.apply:
            update.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (update.directory / "lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Another update holds the local workload lock") from None
                update.execute()
        else:
            update.execute()
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        StopIteration,
        subprocess.TimeoutExpired,
        tarfile.TarError,
        KeyboardInterrupt,
    ) as error:
        print(f"Update stopped: {type(error).__name__}: {error}", file=sys.stderr)
        if update.report:
            print(
                f"Inspect {update.report_path}. No automatic rollback or undrain was attempted.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
