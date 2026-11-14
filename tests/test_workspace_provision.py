"""Capacity results are connection-local; inventory alone authorizes job allocation."""

import asyncio
import json
from io import StringIO
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

from app.communication.dispatch import Dispatcher
from app.contracts.protocol import (
    MAX_PAYLOAD_ITEMS,
    PayloadValidationError,
    ProvisionError,
    ProvisionResult,
    RepositoryDemand,
    ValidationErrorResult,
    WorkerMessageUnion,
    parse_worker_message,
    result_header,
)
from app.controler.workers.callbacks import PROVISION_RETRY_SECONDS
from app.controler.workers.errors import ProtocolViolation
from app.controler.workflow.service import ControlService
from app.telemetry.logging import configure_logging
from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import RepositoryProvisionError, RepositoryWorkspaces
from tests.test_workspace_claims import provision
from tests.wire import worker_message
from tests.worker_fixtures import server_callbacks


@pytest.fixture
async def controller(server_runtime, recording_resources):
    connections = []

    async def connect():
        callbacks = server_callbacks(server_runtime(), "worker-1", "epoch")
        callbacks.session.send = AsyncMock(return_value=True)
        routes = Dispatcher[WorkerMessageUnion]()
        callbacks.bind(routes)
        routes.freeze()
        callbacks.on_published()
        await callbacks.on_opened()
        connections.append(callbacks)
        return callbacks, routes

    yield connect
    for callbacks in connections:
        await callbacks.aclose()


def requests(callbacks):
    return [
        item
        for call in callbacks.session.send.call_args_list
        for item in call.args[0].payload
        if isinstance(item, RepositoryDemand)
    ]


async def deliver(callbacks, routes, call, error=None, *, repository=None, epoch="epoch"):
    reply = ProvisionResult(
        **result_header(call.message_id),
        method="worker.provision",
        repository=repository or call.repository,
        error=error,
    )
    message = parse_worker_message(worker_message(reply, epoch=epoch).model_dump_json())
    await callbacks.on_message(message)
    await routes.dispatch(message.payload[0], message)


async def test_bounded_demands_skip_blocked_repositories_and_reset_on_reconnect(store, controller):
    # A full page of bad repositories must not starve later demand or repeat each poll.
    demand = [f"owner/repo-{i:03}" for i in range(MAX_PAYLOAD_ITEMS + 1)]
    for repo in demand:
        assert ControlService(database=store.database).create_flow(repo, 1, 42) is not None
    callbacks, routes = await controller()
    await callbacks.poll()
    await callbacks.poll()
    batch = requests(callbacks)
    assert len(batch) == MAX_PAYLOAD_ITEMS
    for call in batch:
        await deliver(callbacks, routes, call, ProvisionError.WORKSPACE_CONFLICT)
        await deliver(callbacks, routes, call, ProvisionError.WORKSPACE_CONFLICT)  # duplicate
    await callbacks.poll()
    assert [call.repository for call in requests(callbacks)] == demand
    last = requests(callbacks)[-1]
    # Real inventory, not the capacity result, removes this repository's demand.
    for index in range(2):
        provision(store, "worker-1", index, last.repository)
    assert last.repository not in store.workers.workspace_demand("worker-1", "epoch")
    await deliver(callbacks, routes, last)
    await callbacks.poll()
    assert len(requests(callbacks)) == MAX_PAYLOAD_ITEMS + 1
    replacement, _ = await controller()
    await replacement.poll()
    assert len(requests(replacement)) == MAX_PAYLOAD_ITEMS


@pytest.mark.parametrize("failure", ["timeout", "filesystem_error"])
async def test_capacity_retry_is_delayed_and_old_results_cannot_complete_it(
    store, controller, monkeypatch, failure,
):
    assert ControlService(database=store.database).create_flow("owner/missing", 1, 42) is not None
    now = 100.0
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: now)
    callbacks, routes = await controller()
    await callbacks.poll()
    first = requests(callbacks)[0]
    if failure == "timeout":
        now += callbacks.heartbeat_timeout
        await callbacks.poll()
    else:
        await deliver(callbacks, routes, first, ProvisionError.FILESYSTEM_ERROR)
    await callbacks.poll()
    assert len(requests(callbacks)) == 1
    now += PROVISION_RETRY_SECONDS - 1
    await callbacks.poll()
    assert len(requests(callbacks)) == 1
    now += 1
    await callbacks.poll()
    second = requests(callbacks)[1]
    assert first.message_id != second.message_id
    await deliver(callbacks, routes, first)
    await callbacks.poll()
    assert len(requests(callbacks)) == 2  # A stale success cannot trigger another request.
    with pytest.raises(ProtocolViolation, match="epoch"):
        await deliver(callbacks, routes, second, epoch="old")
    with pytest.raises(ProtocolViolation, match="another repository"):
        await deliver(callbacks, routes, second, repository="owner/other")
    await deliver(callbacks, routes, second, ProvisionError.INVALID_REPOSITORY)
    now += PROVISION_RETRY_SECONDS * 10
    await callbacks.poll()
    assert len(requests(callbacks)) == 2


async def test_provision_send_failure_and_validation_rejection_retire_waiters(store, controller):
    assert ControlService(database=store.database).create_flow("owner/missing", 1, 42) is not None
    callbacks, routes = await controller()
    callbacks.session.send.side_effect = ConnectionError("write failed")
    with pytest.raises(ConnectionError):
        await callbacks.poll()
    callbacks.session.send.side_effect = None
    await callbacks.poll()
    assert len(requests(callbacks)) == 2
    call = requests(callbacks)[-1]
    rejected = ValidationErrorResult(
        **result_header(call.message_id),
        method=call.method,
        error=PayloadValidationError(
            code="validation_error", message="message payload failed validation"
        ),
    )
    await routes.dispatch(rejected, worker_message(rejected, epoch="epoch"))
    await callbacks.poll()
    assert len(requests(callbacks)) == 2
    late = ProvisionResult(
        **result_header(call.message_id), method=call.method, repository=call.repository, error=None,
    )
    assert not await routes.reject_reply(late, "late")


@pytest.mark.parametrize("created", [False, True])
async def test_failed_directory_creation_retries_only_proven_absent_slots(
    tmp_path, monkeypatch, created
):
    pool = RepositoryWorkspaces(tmp_path, "worker", limit=1)
    pool.initialize()
    mkdir = type(tmp_path).mkdir

    def fail(path, *args, **kwargs):
        if path.parent == tmp_path / "workspaces/owner/repo":
            if created:
                mkdir(path, *args, **kwargs)
            raise OSError("private path")
        return mkdir(path, *args, **kwargs)

    (tmp_path / "workspaces/owner/repo").mkdir(parents=True)
    with monkeypatch.context() as patch:
        patch.setattr(type(tmp_path), "mkdir", fail)
        with pytest.raises(RepositoryProvisionError) as raised:
            await pool.provision("owner/repo")
        assert raised.value.code is ProvisionError.FILESYSTEM_ERROR
    slot = json.loads(pool.manifest_path.read_text())["slots"][0]
    assert len(pool.inventory()) == 1  # No proven directory is advertised as capacity.
    if created:
        with pytest.raises(RepositoryProvisionError) as raised:
            await pool.provision("owner/repo")
        assert raised.value.code is ProvisionError.WORKSPACE_CONFLICT
        assert len(pool.inventory()) == 1
    else:
        slots = (await pool.provision("owner/repo"))[1:]
        assert len(slots) == 1 and slots[0].workspace == slot["slot_id"]
        assert slots[0].flow_id is None


@pytest.mark.parametrize("failure", ["identity", "write", "programmer"])
async def test_global_state_failures_are_not_repository_results(tmp_path, monkeypatch, failure):
    pool = RepositoryWorkspaces(tmp_path, "worker")
    pool.initialize()
    if failure == "identity":
        raw = json.loads(pool.manifest_path.read_text())
        raw["worker_id"] = "other"
        pool.manifest_path.write_text(json.dumps(raw))
        expected = VcsVerificationError
    else:
        expected = OSError if failure == "write" else TypeError
        monkeypatch.setattr(pool, "_write", Mock(side_effect=expected("global failure")))
    with pytest.raises(expected) as raised:
        await pool.provision("owner/repo")
    assert not isinstance(raised.value, RepositoryProvisionError)


def test_provision_logs_expose_repository_and_fixed_code_without_exception_paths():
    output = StringIO()
    configure_logging("serve", "json", output)
    try:
        logger.bind(
            event="workspace_provision_failed",
            reason_code="filesystem_error",
            worker_id="worker",
            repository="owner/repo",
        ).opt(exception=OSError("/private/secret-root")).warning("private diagnostic")
        row = json.loads(output.getvalue())
        assert (row["event"], row["reason_code"], row["repository"]) == (
            "workspace_provision_failed",
            "filesystem_error",
            "owner/repo",
        )
        assert "private" not in output.getvalue()
    finally:
        configure_logging("worker", "text")
