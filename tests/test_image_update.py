"""Run the webhook updater CLI with real PostgreSQL and a simulated Kubernetes boundary."""

import copy
import importlib.util
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.contracts.protocol import PROTOCOL_VERSION
from app.persistence.schema import SCHEMAS

TOOLS = Path(__file__).parents[1] / "scripts/server_update"
IMAGE = "ghcr.io/example/serve@sha256:" + "a" * 64
REAL_RUN = subprocess.run


class ImageCommands:
    """Keep real schema inspection and PostgreSQL, replacing Kubernetes transport."""

    def __init__(self, store, failure=""):
        self.store = store
        self.failure = failure
        self.calls = []
        self.workload = {
            "kind": "StatefulSet",
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
                        "terminationGracePeriodSeconds": 60,
                        "containers": [
                            {
                                "name": "app",
                                "image": "old:1",
                                "imagePullPolicy": "IfNotPresent",
                                "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8000}},
                            },
                            {"name": "gateway", "image": "gateway:1"},
                        ],
                    }
                },
            },
            "status": {"observedGeneration": 1},
        }
        if failure == "pvc-delete":
            self.workload["spec"]["persistentVolumeClaimRetentionPolicy"] = {"whenScaled": "Delete"}
        self.stopped_reads = 0
        self.old_gone = False
        self.probe = None
        self.probe_result = None
        self.events = []
        # Authorized-but-not-registered and registered-but-not-authorized workers
        # must not turn a controller/frontend update into a worker rollout gate.
        self.env = dict(
            os.environ,
            FLOWTOOL_DATABASE_URL=store.database_url,
            FLOWTOOL_WORKER_TOKENS='{"unregistered":"TEST-UPDATER-SECRET"}',
        )

    def pod(self):
        new = self.workload["metadata"]["generation"] > 2
        if new and self.failure != "readiness":
            self.events.append("new-ready")
        return {
            "metadata": {
                "name": "server-0",
                "uid": "new" if new else "old",
                "ownerReferences": [{"uid": "workload", "controller": True}],
            },
            "spec": {
                "nodeName": "node",
                # Admission may reorder containers; match the application by name.
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
                        "imageID": "wrong"
                        if new and self.failure == "running-digest"
                        else "checked"
                        if new
                        else "old",
                    }
                ],
            },
        }

    def kube(self, args):
        operation, resource = args[:2]
        if operation == "patch":
            for patch in json.loads(args[args.index("-p") + 1]):
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
        if resource == "node/node":
            return {
                "metadata": {"name": "node"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "nodeInfo": {"operatingSystem": "linux", "architecture": "amd64"},
                },
            }
        if resource == "statefulset/server":
            return self.workload
        if resource == "horizontalpodautoscalers":
            return {
                "items": [{"spec": {"scaleTargetRef": {"kind": "StatefulSet", "name": "server"}}}]
                if self.failure == "hpa"
                else []
            }
        assert resource == "pods"
        if self.workload["spec"]["replicas"] == 0:
            self.stopped_reads += 1
            if self.failure == "old-stuck" or self.stopped_reads == 1:
                return {"items": [self.pod()]}
            self.old_gone = True
            return {"items": []}
        return {"items": [self.pod()]}

    def run_probe(self):
        schemas = list(SCHEMAS)
        changes = {
            "add-column": "ALTER TABLE workers ADD COLUMN IF NOT EXISTS controller_hint TEXT",
            "change-default": "ALTER TABLE workers ALTER COLUMN draining SET DEFAULT true",
            "drop-readd": "ALTER TABLE workers DROP COLUMN draining;"
            "ALTER TABLE workers ADD COLUMN draining BOOLEAN NOT NULL DEFAULT false",
            "dynamic-drop": "DO $$ BEGIN EXECUTE 'ALTER TABLE workers DROP COLUMN draining'; END $$",
            "drop-table": "DROP TABLE workers CASCADE",
            "legacy-drop": "ALTER TABLE workers DROP COLUMN IF EXISTS legacy_hint",
        }
        if self.failure == "remove-definition":
            schemas[0] = schemas[0].replace("  draining BOOLEAN NOT NULL DEFAULT FALSE,\n", "")
        elif self.failure in changes:
            schemas.append(changes[self.failure])
        check = self.probe["spec"]["containers"][0]["command"]
        assert check[2] == (TOOLS / "column-check.py").read_text()
        # Replace only the socket transport. Candidate DDL, isolated databases and
        # the production checker all execute against the real test PostgreSQL.
        # CI requires TCP password auth. Give the probe role its own password,
        # preserving its non-superuser privileges instead of running DDL as admin.
        bootstrap = (
            "import os, psycopg\n"
            "from secrets import token_urlsafe\n"
            "from psycopg import sql\n"
            "from psycopg.conninfo import make_conninfo\n"
            "import app.persistence.schema as schema\n"
            f"schema.SCHEMAS = {schemas!r}\n"
            "namespace = {'__name__': 'column_probe'}\n"
            f"exec({check[2]!r}, namespace)\n"
            "dsn = os.environ['FLOWTOOL_DATABASE_URL']\n"
            "password = token_urlsafe(32)\n"
            "def connect(database, user):\n"
            "    if user == 'postgres':\n"
            "        return psycopg.connect(make_conninfo(dsn, dbname=database), autocommit=True)\n"
            "    with psycopg.connect(dsn, autocommit=True) as admin:\n"
            "        admin.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(\n"
            "            sql.Identifier(user), sql.Literal(password)))\n"
            "    return psycopg.connect(\n"
            "        make_conninfo(dsn, dbname=database, user=user, password=password),\n"
            "        autocommit=True)\n"
            "namespace['connect'] = connect\n"
            "namespace['main']()\n"
        )
        return REAL_RUN(
            [sys.executable, "-c", bootstrap, check[3]],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
            timeout=30,
        )

    def __call__(self, command, *, input=None, **kwargs):
        self.calls.append(tuple(command))
        assert list(command[:6]) == [
            "kubectl",
            "--context",
            "test-context",
            "--namespace",
            "test-namespace",
            "--request-timeout=30s",
        ]
        args = list(command[6:])
        operation, resource = args[:2]
        if operation == "exec":
            inner = args[args.index("--") + 1 :]
            assert inner == ["python", "-", "--snapshot"]
            self.events.append("schema-snapshot")
            assert input == (TOOLS / "column-check.py").read_text()
            return REAL_RUN(
                [sys.executable, "-", *inner[2:]],
                input=input,
                text=True,
                capture_output=True,
                env=self.env,
                check=False,
                timeout=15,
            )
        if operation == "create":
            self.probe = json.loads(input)
            self.probe_result = None
            spec = self.probe["spec"]
            assert spec["automountServiceAccountToken"] is False
            assert all(volume.keys() == {"name", "emptyDir"} for volume in spec["volumes"])
            assert all(not c.get("env") and not c.get("envFrom") for c in spec["containers"])
            assert spec["containers"][0]["volumeMounts"] == [
                {"name": "socket", "mountPath": "/socket"},
            ]
            postgres = spec["containers"][1]
            assert postgres["image"].startswith("postgres:17-alpine@sha256:")
            assert "listen_addresses=" in postgres["command"][2]
            assert not postgres.get("ports")
            self.events.append("probe-create")
            result = {}
        elif operation == "logs":
            assert self.probe_result is not None
            result = json.loads(self.probe_result.stdout)
            # These old compatibility payloads cannot reintroduce source/schema gates.
            result.update(
                fingerprint={"worker": "changed"},
                schemas=["additive change"],
                wire_schemas={"DTO": "changed"},
            )
        elif operation == "delete":
            assert resource.startswith("pod/agentflow-image-check-")
            assert "--force" not in args and "--grace-period=0" not in args
            self.probe = None
            self.events.append("probe-delete")
            result = {}
        elif operation == "get" and resource.startswith("pod/agentflow-image-check-"):
            if self.failure != "pull" and self.probe_result is None:
                self.probe_result = self.run_probe()
                assert self.probe_result.returncode == 0, self.probe_result.stderr
            result = {
                "metadata": self.probe["metadata"],
                "status": {
                    "phase": "Failed" if self.failure == "pull" else "Running",
                    "containerStatuses": [
                        {
                            "name": "check",
                            "imageID": "checked",
                            "state": {"terminated": {"exitCode": self.probe_result.returncode}}
                            if self.probe_result is not None
                            else {},
                        },
                        {"name": "postgres", "state": {"running": {}}},
                    ],
                },
            }
        elif operation == "get" and resource == "pod/server-0":
            result = self.pod()
        else:
            if operation == "patch":
                changes = json.loads(args[args.index("-p") + 1])
                scaling = next(c["value"] for c in changes if c["path"] == "/spec/replicas")
                if scaling == 0:
                    self.events.append("scale-zero")
                    self.store.workers.worker_connected(
                        "added-during-update", PROTOCOL_VERSION, "new"
                    )
                else:
                    assert self.old_gone
                    self.events.append("scale-one")
                    self.store.workers.recover_abandoned_assignments()
                    with self.store.connection() as connection:
                        connection.execute(
                            "ALTER TABLE workers ADD COLUMN new_worker_metadata TEXT"
                        )
            result = self.kube(args)
            if self.old_gone and "old-gone" not in self.events:
                self.events.append("old-gone")
        return subprocess.CompletedProcess(command, 0, json.dumps(copy.deepcopy(result)), "")


@pytest.fixture
def invoke_image_update(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "image_updater", TOOLS / "update-agentflow-server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def invoke(commands, apply=True):
        ticks = itertools.count()
        monkeypatch.setattr(
            module, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
        )
        monkeypatch.setattr(module.subprocess, "run", commands)
        arguments = [
            "update",
            "--context",
            "test-context",
            "--namespace",
            "test-namespace",
            "--workload",
            "statefulset/server",
            "--container",
            "app",
            "--node",
            "node",
            "--image",
            IMAGE,
            "--timeout",
            "4",
            "--state-dir",
            str(tmp_path),
        ]
        monkeypatch.setattr(sys, "argv", arguments + (["--apply"] if apply else []))
        return module.main()

    return invoke


@pytest.mark.parametrize("apply", [False, True])
def test_idle_active_flow_and_worker_changes_do_not_block_update(
    store, invoke_image_update, tmp_path, capsys, apply
):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    initial = store.jobs.enqueue_job(flow.id, "initial")
    assert store.workers.assign_job("w", "initial", "epoch")
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET state='done' WHERE id=%s", (initial.id,))
        connection.execute("UPDATE workers SET current_job_id=NULL,status='online'")
    store.workers.drain("w", True)
    store.jobs.enqueue_job(flow.id, "revision")
    commands = ImageCommands(store)
    assert invoke_image_update(commands, apply) == 0
    assert store.flows.get_flow(flow.id).state == "active"
    assert store.workers.get_flow_execution(flow.id).released_at is None
    assert next(w for w in store.workers.list_workers() if w.worker_id == "w").draining
    if apply:
        order = [
            "schema-snapshot",
            "probe-create",
            "probe-delete",
            "scale-zero",
            "old-gone",
            "scale-one",
            "new-ready",
        ]
        assert [commands.events.index(e) for e in order] == sorted(
            commands.events.index(e) for e in order
        )
        report = next(tmp_path.glob("*/operation.json"))
        saved = json.loads(report.read_text())
        assert saved["status"] == "complete"
        assert (
            saved["backup_policy"] == "disabled"
            and saved["compatibility_policy"] == "column-removal-only"
        )
        assert report.stat().st_mode & 0o777 == 0o600
        assert (
            "TEST-UPDATER-SECRET" not in report.read_text()
            and store.database_url not in report.read_text()
        )
        assert all(
            w.status == "offline" or w.worker_id == "added-during-update"
            for w in store.workers.list_workers()
        )
    else:
        assert "scale-zero" not in commands.events
        assert not list(tmp_path.glob("*/operation.json"))
    output = capsys.readouterr()
    assert "TEST-UPDATER-SECRET" not in output.out + output.err
    assert not any("pg_dump" in call or "pg_restore" in call for call in commands.calls)


@pytest.mark.parametrize(
    "change,allowed",
    [
        ("add-column", True),
        ("change-default", True),
        ("legacy-column", True),
        ("remove-definition", False),
        ("drop-readd", False),
        ("dynamic-drop", False),
        ("drop-table", False),
        ("legacy-drop", False),
    ],
)
def test_column_removal_is_rejected_before_replacement_but_additions_are_allowed(
    store, invoke_image_update, capsys, change, allowed
):
    # DROP+ADD preserves names but loses data; a legacy column left alone must not
    # turn every harmless update into a missing-declaration failure.
    store.workers.worker_connected("existing-worker", PROTOCOL_VERSION, "epoch")
    store.workers.drain("existing-worker", True)
    if change.startswith("legacy"):
        with store.connection() as connection:
            connection.execute("ALTER TABLE workers ADD COLUMN legacy_hint TEXT")
    commands = ImageCommands(store, change)
    assert invoke_image_update(commands) == (0 if allowed else 1)
    assert ("scale-zero" in commands.events) is allowed
    assert next(
        w for w in store.workers.list_workers() if w.worker_id == "existing-worker"
    ).draining
    if not allowed:
        assert commands.probe is not None
        output = capsys.readouterr()
        assert "Database column removal detected:" in output.err
        column = "legacy_hint" if change == "legacy-drop" else "draining"
        assert "workers" in output.err
        if change != "drop-table":
            assert column in output.err


@pytest.mark.parametrize("pending", ["job", "dispatch", "stop", "result", "effect"])
def test_durable_backlog_does_not_block_replacement(
    store,
    invoke_image_update,
    tmp_path,
    pending,
):
    flow = store.flows.create_flow("owner/repo", 1, 1)
    job = store.jobs.enqueue_job(flow.id, "initial")
    store.workers.worker_connected("w", PROTOCOL_VERSION, "epoch")
    with store.connection() as connection:
        if pending == "job":
            # A persisted claim is recovered by the ordinary startup sweep.
            assert store.workers.assign_job("w", "token", "epoch")
        elif pending == "dispatch":
            connection.execute(
                "UPDATE jobs SET state='failed',dispatch_authorized_at=now() WHERE id=%s", (job.id,)
            )
        elif pending == "stop":
            connection.execute(
                "INSERT INTO execution_stops(request_id,flow_id,job_id,worker_id,assignment_token,binding_at,requested_at) VALUES('stop',%s,%s,'w','token',now(),now())",
                (flow.id, job.id),
            )
        elif pending == "result":
            connection.execute(
                "UPDATE jobs SET state='done',result_received_at=now() WHERE id=%s", (job.id,)
            )
        else:
            connection.execute(
                "INSERT INTO result_applications(job_id,digest,plan,created_at) VALUES(%s,'digest','{}',now())",
                (job.id,),
            )
            connection.execute(
                "INSERT INTO result_effects(job_id,effect_key,position,payload,state,dispatched_at,next_attempt_at) VALUES(%s,'effect',0,'{}','skipped',now(),now())",
                (job.id,),
            )
    commands = ImageCommands(store)
    assert invoke_image_update(commands) == 0
    assert "scale-zero" in commands.events
    assert json.loads(next(tmp_path.glob("*/operation.json")).read_text())["status"] == "complete"


@pytest.mark.parametrize(
    "failure",
    ["pull", "pvc-delete", "hpa", "old-stuck", "readiness", "running-digest"],
)
def test_target_and_cutover_failures_preserve_safety(store, invoke_image_update, tmp_path, failure):
    commands = ImageCommands(store, failure)
    assert invoke_image_update(commands) == 1
    if failure in {"pull", "pvc-delete", "hpa"}:
        assert "scale-zero" not in commands.events
    else:
        assert (
            json.loads(next(tmp_path.glob("*/operation.json")).read_text())["status"]
            == "incomplete"
        )
        # Retrying must not blindly overwrite the incomplete operation or restart execution.
        before = list(commands.calls)
        assert invoke_image_update(commands) == 1
        assert commands.calls == before
