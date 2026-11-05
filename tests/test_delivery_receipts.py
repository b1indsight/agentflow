"""Current delivery receipts carry one explicit state across protocol boundaries."""

import pytest

from app.contracts.protocol import (
    ExecuteResult,
    FlowToolResponse,
    Succeeded,
    execution_call_id,
    parse_server_message,
    parse_worker_message,
    result_header,
    terminal_id,
)
from tests.wire import server_message, worker_message


@pytest.mark.parametrize("receipt", [
    {"status": "prepared", "head_sha": "abc", "delivery_version": 2},
    {"status": "opened", "pr_number": 17, "url": "https://github.com/owner/repo/pull/17",
     "head_sha": "abc", "delivery_version": 2},
], ids=["prepared", "opened"])
def test_current_receipts_round_trip_through_tool_and_terminal_messages(receipt):
    response = FlowToolResponse.model_validate(
        {
            **result_header("1" * 32),
            "method": "flow.open_pr",
            "call_id": "open-pr",
            "result": receipt,
        }
    )
    assert response.model_dump(mode="json")["result"] == receipt
    assert parse_server_message(
        server_message(response, epoch="epoch").model_dump_json()
    ).payload == [response]
    terminal = Succeeded.model_validate(
        {
            "job_id": 1,
            "assignment_token": "token",
            "result": {
                "kind": "initial",
                "session_id": "session",
                "last_message": "done",
                "exit_code": 0,
                "delivery_receipt": receipt,
            },
        }
    )
    message = ExecuteResult(
        **result_header(execution_call_id(1, "token"), terminal_id(terminal)),
        method="worker.execute",
        job_id=1,
        assignment_token="token",
        result=terminal.result,
    )
    restored = parse_worker_message(worker_message(message, epoch="epoch").model_dump_json())
    assert restored.payload == [message]
    assert restored.payload[0].result.delivery_receipt == response.result
