"""Exercise deployment CLI behavior, using real PostgreSQL for column compatibility."""

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

from app.persistence.schema import SCHEMAS

TOOLS = Path(__file__).parents[1] / "scripts/server_update"
IMAGE = "ghcr.io/b1indsight/agentflow-serve@sha256:" + "a" * 64
REAL_RUN = subprocess.run


class ImageCommands:
    """Replace Kubernetes transport; optionally run real PostgreSQL column checks."""

    def __init__(self, store=None, failure=""):
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
                        "containers": [
                            {
                                "name": "app",
                                "image": "old:1",
                                "imagePullPolicy": "IfNotPresent",
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
        # The live snapshot validates server settings before connecting.
        self.env = dict(
            os.environ,
            FLOWTOOL_DATABASE_URL=store.database_url if store else "unused",
            FLOWTOOL_WORKER_TOKENS='{"test":"test-token"}',
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
            return {"metadata": {"name": "node"}}
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
            if self.store is None:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "name": "public",
                            "statements": ["CREATE TABLE sample (id INT)"],
                            "columns": [{"table": "sample", "column": "id", "type": "integer"}],
                        }
                    ),
                    "",
                )
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
            self.probe["metadata"]["uid"] = "probe-" + str(self.events.count("probe-create"))
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
        elif operation == "delete":
            assert resource == "--raw" and "/pods/agentflow-image-check-" in args[2]
            assert json.loads(input)["preconditions"]["uid"] == self.probe["metadata"]["uid"]
            assert "--force" not in args and "--grace-period=0" not in args
            self.probe = None
            self.events.append("probe-delete")
            result = {}
        elif operation == "get" and resource.startswith("pod/agentflow-image-check-"):
            if self.probe is None:
                assert "--ignore-not-found" in args
                return subprocess.CompletedProcess(command, 0, "", "")
            if self.failure != "pull" and self.probe_result is None:
                self.probe_result = (
                    self.run_probe()
                    if self.store
                    else subprocess.CompletedProcess(
                        [], 0, json.dumps({"removed_columns": [], "checked_columns": 1}), ""
                    )
                )
                assert self.probe_result.returncode == 0, self.probe_result.stderr
            result = {
                "metadata": self.probe["metadata"],
                "spec": self.probe["spec"],
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
                else:
                    assert self.old_gone
                    self.events.append("scale-one")
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

    invoke.module = module
    return invoke


@pytest.mark.parametrize("apply", [False, True])
def test_preflight_and_deployment_order(invoke_image_update, tmp_path, capsys, apply):
    commands = ImageCommands()
    commands.workload["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "FLOWTOOL_DATABASE_URL",
            "value": "postgresql://user:TEST-UPDATER-SECRET@localhost/database",
        }
    ]
    assert invoke_image_update(commands, apply) == 0
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
        assert report.stat().st_mode & 0o777 == 0o600
        assert "TEST-UPDATER-SECRET" not in report.read_text()
    else:
        assert "scale-zero" not in commands.events
        assert not list(tmp_path.glob("*/operation.json"))
    output = capsys.readouterr()
    assert "TEST-UPDATER-SECRET" not in output.out + output.err


@pytest.mark.parametrize(
    "change,allowed",
    [
        ("add-column", True),
        ("change-default", True),
        ("legacy-column", True),
        ("remove-definition", False),
        ("drop-readd", False),
        ("drop-table", False),
        ("legacy-drop", False),
    ],
)
def test_column_removal_is_rejected_before_replacement_but_additions_are_allowed(
    store, invoke_image_update, capsys, change, allowed
):
    # DROP+ADD preserves names but loses data; a legacy column left alone must not
    # turn every harmless update into a missing-declaration failure.
    if change.startswith("legacy"):
        with store.connection() as connection:
            connection.execute("ALTER TABLE workers ADD COLUMN legacy_hint TEXT")
    commands = ImageCommands(store, change)
    assert invoke_image_update(commands) == (0 if allowed else 1)
    assert ("scale-zero" in commands.events) is allowed
    if not allowed:
        assert commands.probe is not None
        output = capsys.readouterr()
        assert "Database column removal detected:" in output.err
        column = "legacy_hint" if change == "legacy-drop" else "draining"
        assert "workers" in output.err
        if change != "drop-table":
            assert column in output.err


@pytest.mark.parametrize(
    "failure",
    ["pull", "pvc-delete", "hpa", "old-stuck", "readiness", "running-digest"],
)
def test_target_and_cutover_failures_preserve_safety(invoke_image_update, tmp_path, failure):
    commands = ImageCommands(failure=failure)
    assert invoke_image_update(commands) == 1
    if failure in {"pull", "pvc-delete", "hpa"}:
        assert "scale-zero" not in commands.events
    else:
        assert (
            json.loads(next(tmp_path.glob("*/operation.json")).read_text())["status"]
            == "incomplete"
        )
        # Retrying observes the cluster and never repeats the stop/start patches.
        before = list(commands.calls)
        assert invoke_image_update(commands) == 1
        assert len(commands.calls) > len(before)
        assert [c for c in commands.calls if "patch" in c] == [c for c in before if "patch" in c]


@pytest.mark.parametrize("stage", ["stopping-old-pod", "starting-new-pod"])
def test_same_image_cli_resumes_cutover_without_restarting_ready_server(
    invoke_image_update, monkeypatch, tmp_path, stage
):
    # A killed updater must converge from real observed state, not require the old Pod
    # or manual deletion of operation.json. Repeat even after Ready was already saved.
    module = invoke_image_update.module
    save = module.Update.save
    interrupted = False

    def crash(update, current):
        nonlocal interrupted
        save(update, current)
        if current == stage and not interrupted:
            interrupted = True
            raise OSError("simulated process interruption")

    monkeypatch.setattr(module.Update, "save", crash)
    commands = ImageCommands()
    assert invoke_image_update(commands) == 1
    assert interrupted
    assert invoke_image_update(commands) == 0
    assert invoke_image_update(commands) == 0
    assert commands.events.count("scale-zero") == 1
    assert commands.events.count("scale-one") == 1
    assert commands.events.count("probe-create") == 1
    assert commands.probe is None
    assert commands.workload["spec"]["replicas"] == 1
    assert commands.workload["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
    assert json.loads(next(tmp_path.glob("*/operation.json")).read_text())["status"] == "complete"


@pytest.mark.parametrize("replicas", [0, 1])
def test_lost_patch_response_reconciles_and_retry_never_stops_target_image(
    invoke_image_update, replicas
):
    commands = ImageCommands()
    original = commands.__call__
    interrupted = False

    def lost_response(command, **kwargs):
        nonlocal interrupted
        result = original(command, **kwargs)
        if "patch" in command:
            changes = json.loads(command[command.index("-p") + 1])
            if (
                any(c["path"] == "/spec/replicas" and c["value"] == replicas for c in changes)
                and not interrupted
            ):
                interrupted = True
                raise subprocess.TimeoutExpired(command, 45)
        return result

    assert invoke_image_update(lost_response) == 0
    assert invoke_image_update(commands) == 0
    assert commands.events.count("scale-zero") == commands.events.count("scale-one") == 1


@pytest.mark.parametrize("conflict", ["image", "uid", "config", "corrupt"])
def test_retry_does_not_override_conflicting_image_target_or_record(
    invoke_image_update, tmp_path, conflict
):
    commands = ImageCommands(failure="readiness")
    assert invoke_image_update(commands) == 1
    if conflict == "image":
        commands.workload["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign:latest"
    elif conflict == "uid":
        commands.workload["metadata"]["uid"] = "recreated"
    elif conflict == "config":
        commands.workload["spec"]["template"]["spec"]["containers"][1]["image"] = "foreign-sidecar"
    else:
        next(tmp_path.glob("*/operation.json")).write_text("{corrupt")
    patches = [c for c in commands.calls if "patch" in c]
    assert invoke_image_update(commands) == 1
    assert [c for c in commands.calls if "patch" in c] == patches


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_candidate_request_response_loss_does_not_leave_duplicate_probe_or_restart(
    invoke_image_update, operation
):
    commands = ImageCommands()
    lost = False

    def transport(command, **kwargs):
        nonlocal lost
        result = commands(command, **kwargs)
        if command[6] == operation and not lost:
            lost = True
            raise subprocess.TimeoutExpired(command, 45)
        return result

    assert invoke_image_update(transport) == 0
    assert invoke_image_update(commands) == 0
    assert lost and commands.probe is None
    assert commands.events.count("probe-create") == 1
    assert commands.events.count("scale-zero") == 1


def test_retry_before_stop_refreshes_recreated_old_pod_instead_of_becoming_unrecoverable(
    invoke_image_update, monkeypatch
):
    commands = ImageCommands(failure="pull")
    assert invoke_image_update(commands) == 1
    assert "scale-zero" not in commands.events
    pod = commands.pod

    def restarted():
        current = pod()
        if current["metadata"]["uid"] == "old":
            current["metadata"]["uid"] = "restarted"
        return current

    monkeypatch.setattr(commands, "pod", restarted)
    commands.failure = ""
    assert invoke_image_update(commands) == 0
    assert commands.events.count("scale-zero") == 1
    assert commands.probe is None


def test_changed_candidate_inputs_are_not_reused_or_deleted(invoke_image_update):
    commands = ImageCommands(failure="pull")
    assert invoke_image_update(commands) == 1
    commands.failure = ""
    commands.probe["spec"]["containers"][0]["command"] = ["foreign-command"]
    # Avoid executing the deliberately foreign command in the transport simulator.
    commands.probe_result = subprocess.CompletedProcess([], 0, "{}", "")
    assert invoke_image_update(commands) == 1
    assert commands.probe is not None
    assert "scale-zero" not in commands.events


def test_queue_cli_resumes_completed_deployment_after_event_write_failure(monkeypatch, tmp_path):
    # Exercise the real event CLI wiring to Update.apply, not only a deploy callback.
    spec = importlib.util.spec_from_file_location("image_event_e2e", TOOLS / "image_event.py")
    events = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, events)
    spec.loader.exec_module(events)
    target = [
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
        "--timeout",
        "4",
        "--state-dir",
        str(tmp_path),
    ]
    monkeypatch.setattr(sys, "argv", ["image-event", "init", *target])
    events.main()
    queue = events.Queue(next(tmp_path.iterdir()))
    sha = "a" * 40
    queue.enqueue(2, 1, sha)
    image = "ghcr.io/b1indsight/agentflow-serve@sha256:" + "b" * 64
    monkeypatch.setattr(
        events,
        "resolve",
        lambda *_: {
            "run_id": 2,
            "attempt": 1,
            "sha": sha,
            "run_number": 2,
            "image": image,
        },
    )
    # Once verified, the image stays selected even if a later push advances main.
    monkeypatch.setattr(
        events, "github", lambda _: pytest.fail("must not recheck main after resolving the image")
    )
    commands = ImageCommands()
    monkeypatch.setattr(subprocess, "run", commands)
    # The queue CLI always deploys; retry after event persistence fails must not restart it.
    monkeypatch.setattr(sys, "argv", ["image-event", "run", *target, "--once"])
    with monkeypatch.context() as failure:
        failure.setattr(events.Queue, "finish", lambda *_: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            events.main()
    assert queue.events()[0].state == "running"
    assert json.loads((queue.directory / "operation.json").read_text())["status"] == "complete"
    events.main()
    assert queue.events()[0].state == "deployed"
    assert commands.workload["spec"]["template"]["spec"]["containers"][0]["image"] == image
    assert commands.events.count("scale-zero") == commands.events.count("scale-one") == 1
    assert commands.probe is None


def test_queue_cli_rejects_other_image_then_resumes_direct_deployment(
    invoke_image_update, monkeypatch, tmp_path
):
    # A direct CLI timeout after scale-down must not kill queue consumption or
    # let a newer image replace its intent; the same image can finish the stop/start.
    commands = ImageCommands(failure="old-stuck")
    assert invoke_image_update(commands) == 1
    record = next(tmp_path.glob("*/operation.json"))
    unfinished = record.read_bytes()
    before = list(commands.events)
    commands.failure = ""
    target = sys.argv[1:]
    image_index = target.index("--image")
    del target[image_index : image_index + 2]
    target.remove("--apply")

    # Keep the real CLI adapter, as in the event-write recovery test above.
    spec = importlib.util.spec_from_file_location("image_event_direct", TOOLS / "image_event.py")
    events = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, events)
    spec.loader.exec_module(events)
    events.initialize(record.parent)
    queue = events.Queue(record.parent)
    monkeypatch.setattr(
        events,
        "resolve",
        lambda run_id, attempt, sha: {
            "run_id": run_id,
            "attempt": attempt,
            "sha": sha,
            "run_number": run_id,
            "image": IMAGE if run_id == 3 else IMAGE.replace("a" * 64, "b" * 64),
        },
    )
    monkeypatch.setattr(sys, "argv", ["image-event", "run", *target, "--once"])
    queue.enqueue(2, 1, "a" * 40)
    with pytest.raises(events.Rejected, match="deployment failed; same event can be retried"):
        events.main()
    assert queue.events()[0].state == "failed"
    assert record.read_bytes() == unfinished
    assert commands.events == before

    queue.enqueue(3, 1, "c" * 40)
    events.main()
    assert [row.state for row in queue.events()] == ["failed", "deployed"]
    assert json.loads(record.read_text())["status"] == "complete"
    assert commands.workload["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
    assert commands.events.count("scale-zero") == commands.events.count("scale-one") == 1
