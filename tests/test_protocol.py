"""Public wire contracts, independent of connection and application scheduling."""

import json
from collections.abc import Sequence

import pytest
from pydantic import JsonValue, ValidationError

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    AcceptResultRequest,
    Assignment,
    CancelJob,
    ExecuteResult,
    Failed,
    FlowToolRequest,
    Register,
    Registered,
    accept_result_id,
    execution_call_id,
    parse_server_message,
    parse_worker_message,
    terminal_id,
)


def message(payload: Sequence[JsonValue], **fields: JsonValue) -> dict[str, JsonValue]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": "1" * 32,
        "connection_epoch": "epoch",
        "control": None,
        "ack": False,
        "ack_id": None,
        "payload": list(payload),
        **fields,
    }


def call(method: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "tool_call",
        "message_id": "a" * 32,
        "reply_to": None,
        "method": method,
        "arguments": arguments,
    }


def result(method: str, **outcome: JsonValue) -> dict[str, JsonValue]:
    return {
        "type": "tool_result",
        "message_id": "b" * 32,
        "reply_to": "a" * 32,
        "method": method,
        **outcome,
    }


IDENTITY: dict[str, JsonValue] = {"job_id": 5, "assignment_token": "assignment-1"}
LOCATION: dict[str, JsonValue] = {"workspace": "a" * 32, "generation": 1}
INVENTORY: list[JsonValue] = [{
    "workspace": "b" * 32, "generation": 1, "repository": None, "flow_id": None,
}]
FENCE: dict[str, JsonValue] = {**IDENTITY, "flow_id": 3, "request_id": "stop-1"}
TASK: dict[str, JsonValue] = {
    "kind": "initial",
    "flow_id": 3,
    "attempt_id": 7,
    "repo": "owner/repo",
    "issue_number": 1,
    "issue_title": "test",
    "issue_body": "",
    "branch": "flow/3",
}
FLOW: dict[str, JsonValue] = {
    **IDENTITY,
    "flow_id": 3,
    "attempt_id": 7,
    "call_id": "call-1",
    "arguments": {"title": "Fix it"},
    "vcs_backend": "git",
    "branch": "flow/3",
    "commit_sha": "abc",
    "remote_sha": "abc",
}
SESSION = "T-019eac3b-a60c-7a93-a442-3718f660fc8c"


@pytest.mark.parametrize(
    ("direction", "item", "valid"),
    [
        ("server", call("worker.execute", {**IDENTITY, **LOCATION, "flow_id": 3, "task": TASK}), True),
        ("server", call("worker.stop", FENCE), True),
        ("server", call("worker.history", {"session_id": SESSION, "cursor": "bad-cursor"}), True),
        ("server", call("worker.history", {"session_id": "https://ampcode.com/T-x"}), False),
        (
            "server",
            call(
                "worker.reject_recovery",
                {"job_id": 5, "reason": "stale", "register_message_id": "c" * 32},
            ),
            True,
        ),
        ("worker", call("server.execution_started", IDENTITY), True),
        ("worker", call("server.accept_result", {**IDENTITY, "result_id": "c" * 32}), True),
        ("worker", call("server.accept_stop", {**FENCE, "receipt_id": "c" * 32}), True),
        ("worker", call("flow.open_pr", FLOW), True),
        ("worker", result("worker.execute", **IDENTITY, error="failed"), True),
        (
            "worker",
            result(
                "worker.execute",
                **IDENTITY,
                result={
                    "kind": "initial",
                    "session_id": None,
                    "last_message": "done",
                    "exit_code": 0,
                },
            ),
            True,
        ),
        ("worker", result("worker.stop", result={**FENCE, "execution_nonce": "execution-1"}), True),
        ("worker", result("worker.history", error="cursor_invalid"), True),
        ("server", result("server.accept_result", result={**IDENTITY, "state": "failed"}), True),
        ("server", result("server.accept_stop", result=FENCE), True),
        (
            "server",
            result(
                "flow.open_pr", call_id="call-1", error="bad request", error_kind="invalid_request"
            ),
            True,
        ),
        (
            "server",
            result(
                "flow.open_pr",
                call_id="call-1",
                result={"status": "prepared", "head_sha": "abc", "delivery_version": 1},
            ),
            True,
        ),
        (
            "server",
            result(
                "flow.open_pr",
                call_id="call-1",
                error="throttled",
                error_kind="rate_limited",
                retryable=True,
                retry_after="30",
            ),
            True,
        ),
    ],
)
def test_method_contract_round_trip_and_direction(
    direction: str, item: dict[str, JsonValue], valid: bool,
) -> None:
    parse, other = (
        (parse_server_message, parse_worker_message)
        if direction == "server"
        else (parse_worker_message, parse_server_message)
    )
    if not valid:
        with pytest.raises(ValidationError):
            parse(json.dumps(message([item])))
        return
    parsed = parse(json.dumps(message([item])))
    assert parse(parsed.model_dump_json()) == parsed
    assert parsed.payload[0].method == item["method"]
    with pytest.raises(ValidationError):
        other(parsed.model_dump_json())


def test_assignment_secret_remains_wire_only_and_flow_identity_is_checked() -> None:
    arguments: dict[str, JsonValue] = {
        **IDENTITY,
        **LOCATION,
        "flow_id": 3,
        "task": TASK,
        "secret": {
            "push": {
                "token": "push-token",
                "repository": "owner/repo",
                "expires_at": "2026-09-05T01:00:00Z",
            }
        },
    }
    parsed = parse_server_message(json.dumps(message([call("worker.execute", arguments)])))
    item = parsed.payload[0]
    assert isinstance(item, Assignment) and item.arguments.secret is not None
    assert item.arguments.secret.push.token.get_secret_value() == "push-token"
    assert "push-token" not in repr(parsed)
    assert "push-token" in parsed.model_dump_json()
    arguments["flow_id"] = 4
    with pytest.raises(ValidationError):
        parse_server_message(json.dumps(message([call("worker.execute", arguments)])))


@pytest.mark.parametrize("method", ["flow.future_tool", "worker.unknown", "custom"])
def test_unknown_flow_methods_keep_the_typed_error_path(method: str) -> None:
    parsed = parse_worker_message(json.dumps(message([call(method, FLOW)])))
    assert isinstance(parsed.payload[0], FlowToolRequest)
    assert parsed.payload[0].method == method
    # An unknown lifecycle shape must not enter the Flow fallback; known methods cannot either.
    for malformed in (call(method, IDENTITY), call("server.accept_result", FLOW)):
        with pytest.raises(ValidationError):
            parse_worker_message(json.dumps(message([malformed])))


@pytest.mark.parametrize(
    "key",
    ["protocol_version", "message_id", "connection_epoch", "ack", "ack_id", "control", "payload"],
)
def test_message_metadata_is_required_on_the_wire(key: str) -> None:
    data = message([call("server.execution_started", IDENTITY)])
    del data[key]
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data))


@pytest.mark.parametrize("key", ["type", "method", "message_id", "reply_to"])
def test_item_metadata_is_required_on_the_wire(key: str) -> None:
    item = call("server.execution_started", IDENTITY)
    del item[key]
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(message([item])))


@pytest.mark.parametrize(
    "fields",
    [
        {"protocol_version": 15},
        {"protocol_version": 16},
        {"protocol_version": 17.0},
        {"protocol_version": 18},
        {"protocol_version": 19},
        {"message_id": "A" * 32},
        {"ack": 1},
        {"ack": "true"},
        {"ack": True},
        {"ack_id": "a" * 32},
        {"connection_epoch": None},
        {"connection_epoch": ""},
        {"connection_epoch": "x" * 65},
        {"unexpected": True},
    ],
)
def test_invalid_message_headers_are_rejected(fields: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        parse_worker_message(
            json.dumps(message([call("server.execution_started", IDENTITY)], **fields))
        )


def test_mixed_control_ack_and_business_have_independent_identities() -> None:
    terminal = Failed(job_id=5, assignment_token="assignment-1", error="executor exited 1")
    E, T = execution_call_id(5, "assignment-1"), terminal_id(terminal)
    A = accept_result_id(T)
    assert (E, T, A) == (
        "ee3eaa171e5652b8bff155694215e29b",
        "7eecf358047357e3904fae12781b482c",
        "b7723aac9a2f534e822941852084c2f2",
    )
    terminal_item = {
        **result("worker.execute", **IDENTITY, error=terminal.error),
        "message_id": T,
        "reply_to": E,
    }
    accept = {**call("server.accept_result", {**IDENTITY, "result_id": T}), "message_id": A}
    history = result("worker.history", error="history_unreadable")
    data = message(
        [terminal_item, accept, history], control="heartbeat", **IDENTITY, ack=True, ack_id="c" * 32
    )
    parsed = parse_worker_message(json.dumps(data))
    assert PROTOCOL_VERSION == 20
    assert isinstance(parsed.payload[0], ExecuteResult)
    assert parsed.payload[0].terminal() == terminal
    assert isinstance(parsed.payload[1], AcceptResultRequest)
    assert parsed.payload[2].method == "worker.history"
    assert parsed.ack_id == "c" * 32
    data["assignment_token"] = "other"
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data))
    data["payload"] = [history]
    assert parse_worker_message(json.dumps(data)).payload == [parsed.payload[2]]


def test_register_recovery_and_registered_decisions_are_control_fields() -> None:
    terminal = Failed(job_id=5, assignment_token="assignment-1", error="executor exited 1")
    from app.contracts.protocol import terminal_digest

    T = terminal_id(terminal)
    accept = {
        **call("server.accept_result", {**IDENTITY, "result_id": T}),
        "message_id": accept_result_id(T),
    }
    data = message(
        [],
        control="register",
        connection_epoch=None,
        worker_id="w",
        workspace="b" * 32,
        repo_workspace_limit=2,
        inventory=INVENTORY,
        inventory_complete=True,
        recoveries=[{**IDENTITY, "state": "failed", "result_digest": terminal_digest(terminal)}],
        inference=None,
    )
    parsed = parse_worker_message(json.dumps(data))
    assert isinstance(parsed, Register) and len(parsed.recoveries) == 1
    assert parsed.payload == [] and parsed.inventory_complete
    empty = parse_worker_message(json.dumps({**data, "recoveries": []}))
    assert isinstance(empty, Register) and empty.recoveries == empty.payload == []
    for payload in ([accept], [call("flow.open_pr", FLOW)]):
        with pytest.raises(ValidationError):
            parse_worker_message(json.dumps({**data, "payload": payload}))
    for action, ack in (("resume", False), ("replay", False), (None, True)):
        registered = message(
            [], control="registered", heartbeat_interval=10,
            recoveries=[{"action": action, **IDENTITY}] if action is not None else [], ready=True,
            ack=ack, ack_id="c" * 32 if ack else None,
        )
        decision = parse_server_message(json.dumps(registered))
        assert isinstance(decision, Registered) and decision.ready
        assert decision.payload == [] and decision.ack is ack
        assert decision.ack_id == ("c" * 32 if ack else None)
        assert len(decision.recoveries) == (1 if action is not None else 0)
        if action is not None:
            assert decision.recoveries[0].action == action
        registered["payload"] = [call("worker.history", {"session_id": SESSION})]
        with pytest.raises(ValidationError):
            parse_server_message(json.dumps(registered))


def test_batch_static_validation_and_pure_ack_boundaries() -> None:
    ack = message([], ack=True, ack_id="c" * 32, connection_epoch=None)
    for parse in (parse_worker_message, parse_server_message):
        assert parse(json.dumps(ack)).payload == []
        with pytest.raises(ValidationError):
            parse(json.dumps(message([], connection_epoch=None)))
        with pytest.raises(ValidationError):
            parse(json.dumps({**ack, "connection_epoch": "epoch"}))
    item = call("server.execution_started", IDENTITY)
    for payload in (
        [item, item],
        [{**item, "reply_to": "d" * 32}],
        [result("worker.history", error="cursor_invalid", page={"session_id": SESSION})],
        [{**result("worker.execute", **IDENTITY, error="failure"), "reply_to": "b" * 32}],
    ):
        with pytest.raises(ValidationError):
            parse_worker_message(json.dumps(message(payload)))
    batch: list[JsonValue] = [{**item, "message_id": f"{i:032x}"} for i in range(32)]
    assert len(parse_worker_message(json.dumps(message(batch))).payload) == 32
    batch.append({**item, "message_id": "f" * 32})
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(message(batch)))
    error = message(
        [], control="error", connection_epoch=None, code="version", detail="unsupported"
    )
    assert parse_server_message(json.dumps(error)).payload == []
    error["payload"] = [call("worker.history", {"session_id": SESSION})]
    with pytest.raises(ValidationError):
        parse_server_message(json.dumps(error))


def test_inference_fences_and_cancel_control_are_required_and_directional() -> None:
    task: dict[str, JsonValue] = {
        "request_id": "a" * 32,
        "messages": [{"role": "user", "content": "Answer from supplied facts"}],
    }
    request = {**call("worker.infer", task), **IDENTITY}
    response = result("worker.infer", **IDENTITY, failure={"code": "cancelled"})
    for parse, item in ((parse_server_message, request), (parse_worker_message, response)):
        assert parse(json.dumps(message([item]))).payload[0].method == "worker.infer"
        for key in IDENTITY:
            malformed = {name: value for name, value in item.items() if name != key}
            with pytest.raises(ValidationError):
                parse(json.dumps(message([malformed])))
    cancel = message([], control="cancel_job", **IDENTITY)
    assert isinstance(parse_server_message(json.dumps(cancel)), CancelJob)
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(cancel))
    for fields in ({"payload": [request]}, {"connection_epoch": None}, {"job_id": 0},
                   {"assignment_token": ""}, {"flow_id": 3}):
        with pytest.raises(ValidationError):
            parse_server_message(json.dumps({**cancel, **fields}))


def test_infer_only_registration_cannot_claim_coding_recovery() -> None:
    data = message([], control="register", connection_epoch=None, worker_id="mini",
                   recoveries=[], inference={"adapter_version": "bub/0.4.1"},
                   workspace="b" * 32, repo_workspace_limit=2,
                   inventory=INVENTORY, inventory_complete=True)
    ordinary = parse_worker_message(json.dumps(data))
    assert isinstance(ordinary, Register) and ordinary.execute
    mini = parse_worker_message(json.dumps({**data, "execute": False}))
    assert isinstance(mini, Register) and not mini.execute
    for fields in ({"recoveries": [{**IDENTITY, "state": "started"}]}, {"execute": "false"},
                   {"payload": [call("server.execution_started", IDENTITY)]}):
        with pytest.raises(ValidationError):
            parse_worker_message(json.dumps({**data, "execute": False, **fields}))
