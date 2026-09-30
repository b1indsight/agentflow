"""Typed receipts must retain persisted v9 replay identities."""

import pytest
from pydantic import ValidationError

from app.protocol import FlowToolResponse, Succeeded, terminal_digest


@pytest.mark.parametrize("receipt,expected_digest", [
    ({"status": "prepared", "head_sha": "abc", "delivery_version": 2},
     "6e90337b49e87570b7fbeba27fdd0b06a90fdf7fa067a8e891a24b6d089cfc3a"),
    ({"pr_number": 17, "url": "https://github.com/owner/repo/pull/17",
      "head_sha": "abc", "delivery_version": 3},
     "ec9efe2ecf66c36f4e6c2b0f408293a382726bcf1a1c4ac02e6e535ca8a72d43"),
])
def test_v9_receipts_keep_wire_shape_and_terminal_digest(receipt, expected_digest):
    # Old ledger and journal JSON is replayed after upgrading an existing worker.
    response = FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
    assert response.model_dump(mode="json")["result"] == receipt
    # Fixed v9 data and hashes must not follow current model defaults or serialization changes.
    legacy_result = {
        "kind": "initial", "session_id": "session", "last_message": "done", "exit_code": 0,
        "failure_stage": None, "failure_detail": None, "vcs_backend": None,
        "branch": None, "commit_sha": None, "remote_sha": None, "pushed": False,
        "open_pr_call_id": "open-pr", "pull_request": receipt,
    }
    message = Succeeded.model_validate({"job_id": 1, "assignment_token": "token",
                                       "connection_epoch": "epoch", "result": legacy_result})
    assert message.result.model_dump(mode="json") == legacy_result
    assert terminal_digest(message) == expected_digest


@pytest.mark.parametrize("receipt", [
    {"pr_number": 17},
    {"status": "prepared", "head_sha": "", "delivery_version": 0},
    {"status": "prepared", "head_sha": "abc", "delivery_version": 1, "pr_number": 17},
])
def test_receipt_rejects_incomplete_or_conflated_delivery_states(receipt):
    with pytest.raises(ValidationError):
        FlowToolResponse.model_validate({"call_id": "open-pr", "result": receipt})
