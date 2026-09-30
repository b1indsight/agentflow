"""Current delivery receipts carry one explicit state across protocol boundaries."""

import pytest
from pydantic import ValidationError

from app.contracts.protocol import FlowToolResponse, Succeeded, parse_worker_message


@pytest.mark.parametrize("receipt", [
    {"status": "prepared", "head_sha": "abc", "delivery_version": 2},
    {"status": "opened", "pr_number": 17, "url": "https://github.com/owner/repo/pull/17",
     "head_sha": "abc", "delivery_version": 3},
])
def test_current_receipts_round_trip_through_tool_and_terminal_messages(receipt):
    response = FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
    assert response.model_dump(mode="json")["result"] == receipt
    message = Succeeded.model_validate({
        "job_id": 1, "assignment_token": "token", "connection_epoch": "epoch",
        "result": {"kind": "initial", "session_id": "session", "last_message": "done",
                   "exit_code": 0, "delivery_receipt": receipt},
    })
    restored = parse_worker_message(message.model_dump_json())
    assert restored == message
    assert restored.result.delivery_receipt == response.result


@pytest.mark.parametrize("receipt", [
    {"pr_number": 17},
    {"status": "prepared", "head_sha": "", "delivery_version": 0},
    {"status": "prepared", "head_sha": "abc", "delivery_version": 1, "pr_number": 17},
])
def test_receipt_rejects_incomplete_or_conflated_delivery_states(receipt):
    with pytest.raises(ValidationError):
        FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
