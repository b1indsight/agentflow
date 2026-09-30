"""Typed receipts must retain persisted v9 replay identities."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.protocol import FlowToolResponse, InitialResult, Succeeded, terminal_digest


@pytest.mark.parametrize("receipt", [
    {"status": "prepared", "head_sha": "abc", "delivery_version": 2},
    {"pr_number": 17, "url": "https://github.com/owner/repo/pull/17",
     "head_sha": "abc", "delivery_version": 3},
])
def test_v9_receipts_keep_wire_shape_and_terminal_digest(receipt):
    # Old ledger and journal JSON is replayed after upgrading an existing worker.
    response = FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
    assert response.model_dump(mode="json")["result"] == receipt
    legacy_result = InitialResult(session_id="session", last_message="done", exit_code=0).model_dump(mode="json")
    legacy_result["pull_request"] = receipt
    legacy_result["open_pr_call_id"] = "open-pr"
    message = Succeeded.model_validate({"job_id": 1, "assignment_token": "token",
                                       "connection_epoch": "epoch", "result": legacy_result})
    canonical = json.dumps({"type": "succeeded", "result": legacy_result},
                           sort_keys=True, separators=(",", ":"))
    assert message.result.model_dump(mode="json") == legacy_result
    assert terminal_digest(message) == hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize("receipt", [
    {"pr_number": 17},
    {"status": "prepared", "head_sha": "", "delivery_version": 0},
    {"status": "prepared", "head_sha": "abc", "delivery_version": 1, "pr_number": 17},
])
def test_receipt_rejects_incomplete_or_conflated_delivery_states(receipt):
    with pytest.raises(ValidationError):
        FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
