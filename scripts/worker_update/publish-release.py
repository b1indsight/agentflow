"""Publish one verified release through the existing Server pod, never a remote URL."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
ADAPTER = Path(__file__).with_name("publish_release_adapter.py")


class PublishError(RuntimeError):
    """Safe operational diagnostic; command output is intentionally not exposed."""


def kubectl(
    args: list[str], context: str, namespace: str, *, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", context, "--namespace", namespace, "--request-timeout=30s", *args],
        input=input,
        text=True,
        capture_output=True,
        check=False,
        timeout=35,
    )


def read_json(args: list[str], context: str, namespace: str) -> dict:
    result = kubectl(args, context, namespace)
    if result.returncode:
        raise PublishError("Kubernetes read failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise PublishError("Kubernetes returned invalid metadata") from None
    if not isinstance(value, dict):
        raise PublishError("Kubernetes returned invalid metadata")
    return value


def ready_pod(context: str, namespace: str, workload: str, container: str) -> tuple[str, str]:
    kind, sep, name = workload.partition("/")
    if (kind, sep) != ("statefulset", "/") or not name:
        raise PublishError("workload must be statefulset/NAME")
    statefulset = read_json(["get", workload, "-o", "json"], context, namespace)
    uid = statefulset.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise PublishError("StatefulSet UID is unavailable")
    pods = read_json(
        ["get", "pods", "-l", f"statefulset.kubernetes.io/pod-name={name}-0", "-o", "json"],
        context,
        namespace,
    )
    matches = []
    for pod in pods.get("items", []):
        owners = pod.get("metadata", {}).get("ownerReferences", [])
        owned = any(
            owner.get("uid") == uid
            and owner.get("kind") == "StatefulSet"
            and owner.get("controller") is True
            for owner in owners
        )
        statuses = pod.get("status", {}).get("containerStatuses", [])
        ready = any(
            status.get("name") == container and status.get("ready") is True for status in statuses
        )
        pod_name = pod.get("metadata", {}).get("name")
        pod_uid = pod.get("metadata", {}).get("uid")
        if (
            owned
            and ready
            and isinstance(pod_name, str)
            and isinstance(pod_uid, str)
            and not pod.get("metadata", {}).get("deletionTimestamp")
        ):
            matches.append((pod_name, pod_uid))
    if len(matches) != 1:
        raise PublishError("expected exactly one Ready Pod owned by the StatefulSet")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("run_id")
    parser.add_argument("attempt")
    parser.add_argument("sha")
    args = parser.parse_args()
    if (
        not args.run_id.isdecimal()
        or int(args.run_id) < 1
        or not args.attempt.isdecimal()
        or int(args.attempt) < 1
        or not SHA.fullmatch(args.sha)
    ):
        raise PublishError("expected positive run ID/attempt and a full lowercase commit SHA")
    pod, uid = ready_pod(args.context, args.namespace, args.workload, args.container)
    current = read_json(["get", f"pod/{pod}", "-o", "json"], args.context, args.namespace)
    if current.get("metadata", {}).get("uid") != uid or current.get("metadata", {}).get(
        "deletionTimestamp"
    ):
        raise PublishError("Server Pod changed before publication")
    command = [
        "exec",
        "-i",
        pod,
        "-c",
        args.container,
        "--",
        "python",
        "-",
        args.run_id,
        args.attempt,
        args.sha,
    ]
    if args.apply:
        command.append("--apply")
    result = kubectl(command, args.context, args.namespace, input=ADAPTER.read_text())
    if result.returncode:
        raise PublishError("Worker release publication failed")
    print("published" if args.apply else "checked")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PublishError, OSError, subprocess.SubprocessError):
        print("worker release publisher stopped", file=sys.stderr)
        raise SystemExit(1) from None
