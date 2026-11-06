"""Public wire contracts, independent of connection and application scheduling."""

import json
from collections.abc import Sequence

import pytest
from pydantic import JsonValue, ValidationError

from app.contracts.protocol import (
    PROTOCOL_VERSION,
    AcceptResultRequest,
    Assignment,
    ExecuteResult,
    Failed,
    FlowToolRequest,
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
    ("direction", "item"),
    [
        ("server", call("worker.execute", {**IDENTITY, "flow_id": 3, "task": TASK})),
        ("server", call("worker.stop", FENCE)),
        ("server", call("worker.history", {"session_id": SESSION, "cursor": "bad-cursor"})),
        (
            "server",
            call(
                "worker.reject_recovery",
                {"job_id": 5, "reason": "stale", "register_message_id": "c" * 32},
            ),
        ),
        ("worker", call("server.execution_started", IDENTITY)),
        ("worker", call("server.accept_result", {**IDENTITY, "result_id": "c" * 32})),
        ("worker", call("server.accept_stop", {**FENCE, "receipt_id": "c" * 32})),
        ("worker", call("flow.open_pr", FLOW)),
        ("worker", result("worker.execute", **IDENTITY, error="failed")),
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
        ),
        ("worker", result("worker.stop", result={**FENCE, "execution_nonce": "execution-1"})),
        ("worker", result("worker.history", error="cursor_invalid")),
        ("server", result("server.accept_result", result={**IDENTITY, "state": "failed"})),
        ("server", result("server.accept_stop", result=FENCE)),
        (
            "server",
            result(
                "flow.open_pr", call_id="call-1", error="bad request", error_kind="invalid_request"
            ),
        ),
        (
            "server",
            result(
                "flow.open_pr",
                call_id="call-1",
                result={"status": "prepared", "head_sha": "abc", "delivery_version": 1},
            ),
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
        ),
    ],
)
def test_method_contract_round_trip_and_direction(
    direction: str, item: dict[str, JsonValue]
) -> None:
    parse, other = (
        (parse_server_message, parse_worker_message)
        if direction == "server"
        else (parse_worker_message, parse_server_message)
    )
    parsed = parse(json.dumps(message([item])))
    assert parse(parsed.model_dump_json()) == parsed
    assert parsed.payload[0].method == item["method"]
    with pytest.raises(ValidationError):
        other(parsed.model_dump_json())


def test_assignment_secret_remains_wire_only_and_flow_identity_is_checked() -> None:
    arguments: dict[str, JsonValue] = {
        **IDENTITY,
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
    data = message(
        [terminal_item, accept], control="heartbeat", **IDENTITY, ack=True, ack_id="c" * 32
    )
    parsed = parse_worker_message(json.dumps(data))
    assert PROTOCOL_VERSION == 18
    assert isinstance(parsed.payload[0], ExecuteResult)
    assert parsed.payload[0].terminal() == terminal
    assert isinstance(parsed.payload[1], AcceptResultRequest)
    assert parsed.ack_id == "c" * 32
    data["assignment_token"] = "other"
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data))


def test_register_recovery_and_registered_decisions_are_control_fields() -> None:
    terminal = Failed(job_id=5, assignment_token="assignment-1", error="executor exited 1")
    from app.contracts.protocol import terminal_digest

    T = terminal_id(terminal)
    accept = {
        **call("server.accept_result", {**IDENTITY, "result_id": T}),
        "message_id": accept_result_id(T),
    }
    data = message(
        [accept],
        control="register",
        connection_epoch=None,
        worker_id="w",
        recovery={**IDENTITY, "state": "failed", "result_digest": terminal_digest(terminal)},
        inference=None,
    )
    assert parse_worker_message(json.dumps(data)).payload[0].message_id == accept_result_id(T)
    data["payload"] = [call("flow.open_pr", FLOW)]
    with pytest.raises(ValidationError):
        parse_worker_message(json.dumps(data))
    for action in ("resume", "replay"):
        registered = message(
            [], control="registered", heartbeat_interval=10, recovery={"action": action, **IDENTITY}
        )
        parsed = parse_server_message(json.dumps(registered))
        assert isinstance(parsed, Registered) and parsed.recovery is not None
        assert parsed.recovery.action == action
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


def test_registered_and_heartbeat_can_carry_independent_queries() -> None:
    query = call("worker.history", {"session_id": SESSION})
    registered = message(
        [query],
        control="registered",
        recovery=None,
        heartbeat_interval=10,
        ack=True,
        ack_id="c" * 32,
    )
    assert parse_server_message(json.dumps(registered)).payload[0].method == "worker.history"
    history = result("worker.history", error="history_unreadable")
    heartbeat = message([history], control="heartbeat", **IDENTITY)
    assert parse_worker_message(json.dumps(heartbeat)).payload[0].method == "worker.history"
    register = message([], control="register", connection_epoch=None, worker_id="w", recovery=None,
                       inference=None)
    assert parse_worker_message(json.dumps(register)).payload == []
    query["arguments"] = {"session_id": "https://ampcode.com/T-x"}
    with pytest.raises(ValidationError):
        parse_server_message(json.dumps(registered))
