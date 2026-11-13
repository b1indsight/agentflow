"""Persisted evidence can change format without changing identity or authorizing effects."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import JsonValue

from app.contracts.protocol import (
    ArchiveResult,
    ExecutionStopped,
    Failed,
    InitialTaskResult,
    RevisionTaskResult,
    Succeeded,
    accept_stop_id,
    parse_worker_message,
    stop_call_id,
    stop_receipt_id,
    terminal_digest,
    terminal_id,
)
from app.contracts.receipts import (
    MergeArchiveNotice,
    ResultInbox,
    decode_result_inbox,
    matches_stop_receipt,
    merge_notice_digest,
    stop_receipt_digest,
)
from app.worker.state.record import decode_journal

ROOT = ("/worker-root", 1, 2)
DIGEST = "7d5ffce4a80c24063e4b20afae27f1eb2b9b2ff35c2e4457ce28ba5371199e8e"
FENCE: dict[str, JsonValue] = {
    "request_id": "stop-1",
    "flow_id": 3,
    "job_id": 5,
    "assignment_token": "assignment-1",
}


def previous_terminal(version: int = 15) -> dict[str, JsonValue]:
    return {
        "protocol_version": version,
        "type": "failed",
        "job_id": 5,
        "assignment_token": "assignment-1",
        "connection_epoch": "old-epoch",
        "error": '故障："引用"\n\u0000 🧪',
    }


def test_merge_notice_is_local_evidence_not_a_worker_result() -> None:
    notice = MergeArchiveNotice(
        job_id=5, flow_id=3, round_no=2, repo="owner/repo", pr_number=17,
        merged_at=datetime(2026, 11, 6, 8, tzinfo=UTC), merge_commit_sha="a" * 40,
    )
    inbox = ResultInbox(version=2, source="control", terminal=notice)
    saved = inbox.model_dump(mode="json")
    before = deepcopy(saved)
    digest = merge_notice_digest(notice)
    assert decode_result_inbox(saved, job_id=5, assignment_token=None,
                               result_digest=digest) == inbox
    assert saved == before
    for changes in ({"source": "worker"}, {"version": 1}):
        with pytest.raises(ValueError):
            ResultInbox.model_validate({**saved, **changes})
    conflicts: list[tuple[int, str | None, str]] = [
        (6, None, digest), (5, "real-worker-token", digest), (5, None, "f" * 64),
    ]
    for job_id, token, result_digest in conflicts:
        with pytest.raises(ValueError):
            decode_result_inbox(saved, job_id=job_id, assignment_token=token,
                                result_digest=result_digest)
    with pytest.raises(ValueError):
        parse_worker_message(notice.model_dump_json())
    with pytest.raises(ValueError):
        Succeeded.model_validate({"job_id": 5, "assignment_token": "real-worker-token",
                                  "result": notice.model_dump(mode="json")})


def previous_journal(version: int) -> dict[str, JsonValue]:
    return {
        "version": 4,
        "job_id": 5,
        "flow_id": 3,
        "assignment_token": "assignment-1",
        "state": "failed",
        "result_digest": "7d5ffce4a80c24063e4b20afae27f1eb2b9b2ff35c2e4457ce28ba5371199e8e",
        "terminal": previous_terminal(version),
        "root_identity": list(ROOT),
        "execution_nonce": "execution-1",
        "acknowledged": False,
        "cleanup_verified": True,
        "revision_phase": "fallback",
        "fallback_consumed": True,
        "invocations": [
            {"mode": "fallback", "session_id": "session", "exit_code": 1, "failure": "failed"}
        ],
        "workspace": {
            "repository": "owner/repo",
            "slot_id": "a" * 32,
            "flow_id": 3,
            "generation": 2,
        },
        "stopped": {
            **FENCE,
            "protocol_version": version,
            "type": "execution_stopped",
            "execution_nonce": "execution-1",
            "connection_epoch": "old-epoch",
        },
    }


@pytest.mark.parametrize("version", [14, 15])
def test_old_journal_decodes_without_rewriting_evidence(version: int) -> None:
    original = json.dumps(previous_journal(version))
    record = decode_journal(original, root_identity=ROOT)
    assert record.version == 5 and record.stopped is not None and record.terminal is not None
    assert terminal_digest(record.terminal) == previous_journal(version)["result_digest"]
    assert terminal_id(record.terminal) == terminal_id(
        Failed(
            job_id=5,
            assignment_token="assignment-1",
            error='故障："引用"\n\u0000 🧪',
        )
    )
    assert record.cleanup_verified and record.fallback_consumed and not record.acknowledged
    assert record.invocations[0].session_id == "session"
    assert record.workspace is not None and record.workspace.generation == 2
    saved = record.model_dump(mode="json")
    for name in ("terminal", "stopped"):
        assert "protocol_version" not in saved[name] and "connection_epoch" not in saved[name]
    assert decode_journal(record.model_dump_json(), root_identity=ROOT) == record
    assert original == json.dumps(previous_journal(version))
    with pytest.raises(ValueError):
        parse_worker_message(json.dumps(previous_terminal(version)))


# Credential-free outcomes/digests copied from baseline-protocol-vectors.json at
# b99c8e39f6d391b5685a91a8d6c428c51810bb11, not generated by the current serializers.
# Successful legacy receipts must remain readable when upgrading a worker or applying an inbox.
@pytest.mark.parametrize("version", [14, 15])
@pytest.mark.parametrize(
    ("result", "result_type", "digest"),
    [
        pytest.param(
            {
                "kind": "initial",
                "session_id": None,
                "last_message": '故障："引用"\n\u0000 🧪',
                "exit_code": 1,
                "failure_stage": None,
                "failure_detail": None,
                "vcs_backend": None,
                "branch": None,
                "commit_sha": None,
                "remote_sha": None,
                "pushed": False,
                "open_pr_call_id": None,
                "delivery_receipt": None,
            },
            InitialTaskResult,
            "3db68949ff64084d18940d4485a389872841f3b10bb35e58e9856331e5080f54",
            id="initial",
        ),
        pytest.param(
            {
                "batch_id": 11,
                "feedback_refs": [],
                "kind": "revision",
                "session_id": "session",
                "last_message": "done",
                "exit_code": 0,
                "flow_id": 3,
                "attempt_id": 7,
                "previous_session_id": None,
                "session_generation": 1,
                "mode": "fallback",
                "invocations": [],
                "output": None,
                "failure_stage": None,
                "failure_detail": None,
                "vcs_backend": None,
                "branch": None,
                "commit_sha": None,
                "remote_sha": None,
                "clean_worktree": False,
            },
            RevisionTaskResult,
            "ccbfef9988818c483dd61767c47ca6ad2cdac7a0b5fba03c5d3e5e88dcb82dc4",
            id="revision",
        ),
        pytest.param(
            {
                "flow_id": 3,
                "attempt_id": 7,
                "repo": "owner/repo",
                "worker_id": "worker-1",
                "repo_identity": "repo-1",
                "delivery_version": 2,
                "kind": "archive",
                "cleanup": "released",
            },
            ArchiveResult,
            "346083919036876711284300614f7974436c8cc6d43a8717ccd395a25525c876",
            id="archive",
        ),
    ],
)
def test_legacy_success_decoders_preserve_baseline_outcomes(
    version: int,
    result: dict[str, JsonValue],
    result_type: type[InitialTaskResult] | type[RevisionTaskResult] | type[ArchiveResult],
    digest: str,
) -> None:
    terminal: dict[str, JsonValue] = {
        "protocol_version": version,
        "type": "succeeded",
        "job_id": 5,
        "assignment_token": "assignment-1",
        "connection_epoch": "epoch-1",
        "result": result,
    }
    journal: dict[str, JsonValue] = {
        "version": 4,
        "job_id": 5,
        "flow_id": 3,
        "assignment_token": "assignment-1",
        "state": "succeeded",
        "result_digest": digest,
        "terminal": terminal,
        "revision_phase": None,
        "fallback_consumed": False,
        "invocations": [],
        "archive_task": None,
        "root_identity": list(ROOT),
        "acknowledged": False,
        "cleanup_verified": False,
        "workspace": None,
        "execution_nonce": "execution-1",
        "stopped": {
            "protocol_version": version,
            **FENCE,
            "connection_epoch": "epoch-1",
            "type": "execution_stopped",
            "execution_nonce": "execution-1",
        },
    }
    original = deepcopy(journal)
    raw = json.dumps(journal)
    inbox = decode_result_inbox(
        terminal, job_id=5, assignment_token="assignment-1", result_digest=digest
    )
    record = decode_journal(raw, root_identity=ROOT)
    assert inbox.version == 1 and inbox.source == "worker"
    assert record.version == 5 and not record.acknowledged
    assert record.terminal == inbox.terminal
    assert isinstance(inbox.terminal, Succeeded)
    assert isinstance(inbox.terminal.result, result_type)
    assert inbox.terminal.result.model_dump(mode="json") == result
    assert record.result_digest == terminal_digest(inbox.terminal) == digest
    assert record.stopped == ExecutionStopped(
        request_id="stop-1",
        flow_id=3,
        job_id=5,
        assignment_token="assignment-1",
        execution_nonce="execution-1",
    )
    assert (
        decode_result_inbox(
            json.loads(inbox.model_dump_json()),
            job_id=5,
            assignment_token="assignment-1",
            result_digest=digest,
        )
        == inbox
    )
    assert decode_journal(record.model_dump_json(), root_identity=ROOT) == record
    assert journal == original and json.dumps(journal) == raw


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("version", 3),
        ("version", 4.0),
        ("terminal.protocol_version", 13),
        ("terminal.protocol_version", 16),
        ("terminal.protocol_version", 15.0),
        ("terminal.type", None),
        ("terminal.connection_epoch", None),
        ("terminal.extra", "unexpected"),
        ("terminal.job_id", 6),
        ("terminal.assignment_token", "other"),
        ("stopped.protocol_version", 14),
        ("stopped.flow_id", 4),
        ("stopped.execution_nonce", "other"),
        ("stopped.execution_nonce", ""),
        ("result_digest", None),
        ("root_identity", ["/other-root", 1, 2]),
    ],
)
def test_invalid_or_mixed_journal_is_not_repaired(path: str, value: JsonValue) -> None:
    data = previous_journal(15)
    target = data
    *parents, key = path.split(".")
    for parent in parents:
        child = target[parent]
        assert isinstance(child, dict)
        target = child
    target[key] = value
    original = json.dumps(data)
    with pytest.raises(ValueError):
        decode_journal(original, root_identity=ROOT)
    assert json.dumps(data) == original


@pytest.mark.parametrize("version", [14, 15])
@pytest.mark.parametrize("source", ["worker", "control"])
def test_result_inbox_preserves_digest_and_origin(version: int, source: str) -> None:
    data = previous_terminal(version)
    token = "assignment-1" if source == "worker" else None
    if source == "control":
        data.update(connection_epoch="control", assignment_token="control")
    original = deepcopy(data)
    digest = "7d5ffce4a80c24063e4b20afae27f1eb2b9b2ff35c2e4457ce28ba5371199e8e"
    inbox = decode_result_inbox(data, job_id=5, assignment_token=token, result_digest=digest)
    assert inbox.source == source and inbox.version == 1
    assert isinstance(inbox.terminal, Failed)
    assert inbox.terminal.assignment_token == (token or "control")
    assert data == original
    assert (
        decode_result_inbox(
            json.loads(inbox.model_dump_json()),
            job_id=5,
            assignment_token=token,
            result_digest=digest,
        )
        == inbox
    )
    for job_id, assignment_token, result_digest in (
        (6, token, digest),
        (5, "different", digest),
        (5, token, "wrong"),
    ):
        with pytest.raises(ValueError):
            decode_result_inbox(
                data, job_id=job_id, assignment_token=assignment_token, result_digest=result_digest
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", None),
        ("protocol_version", None),
        ("connection_epoch", None),
        ("protocol_version", 13),
        ("protocol_version", 16),
        ("protocol_version", 15.0),
    ],
)
def test_legacy_inbox_requires_known_complete_metadata(field: str, value: JsonValue) -> None:
    data = previous_terminal()
    if value is None:
        del data[field]
    else:
        data[field] = value
    # Inbox has no second receipt: journal's mixed-version check cannot protect this boundary.
    with pytest.raises(ValueError):
        decode_result_inbox(data, job_id=5, assignment_token="assignment-1", result_digest=DIGEST)


def test_control_inbox_cannot_claim_worker_success_or_worker_authority() -> None:
    success: dict[str, JsonValue] = {
        "type": "succeeded",
        "job_id": 5,
        "assignment_token": "assignment-1",
        "result": {"kind": "initial", "session_id": "s", "last_message": "done", "exit_code": 0},
    }
    digest = terminal_digest(Succeeded.model_validate(success))
    cases: list[dict[str, JsonValue]] = [
        {"version": 1, "source": "control", "terminal": success},
        {"protocol_version": 15, "connection_epoch": "control", **success},
    ]
    for data in cases:
        with pytest.raises(ValueError):
            decode_result_inbox(
                data, job_id=5, assignment_token="assignment-1", result_digest=digest
            )
    with pytest.raises(ValueError):
        decode_result_inbox(
            {**previous_terminal(), "assignment_token": "control"},
            job_id=5,
            assignment_token=None,
            result_digest=DIGEST,
        )
    with pytest.raises(ValueError):
        decode_result_inbox(
            {**previous_terminal(), "unexpected": True},
            job_id=5,
            assignment_token="assignment-1",
            result_digest=DIGEST,
        )


def test_stop_digest_and_ids_are_transport_independent_but_nonce_bound() -> None:
    receipt = ExecutionStopped(
        request_id="stop-1",
        flow_id=3,
        job_id=5,
        assignment_token="assignment-1",
        execution_nonce="execution-1",
    )
    digest = stop_receipt_digest(receipt)
    assert digest == "6ca59855bc756a9dafaa69f4c04b0a33a81be55d1a43b1aa3b01c78018aa204c"
    assert matches_stop_receipt(receipt, digest)
    # This value is independently calculated from the literal v14 stored projection.
    import hashlib

    legacy_digest = hashlib.sha256(
        b'{"protocol_version":14,"request_id":"stop-1","flow_id":3,"job_id":5,"assignment_token":"assignment-1","type":"execution_stopped","execution_nonce":"execution-1"}'
    ).hexdigest()
    assert matches_stop_receipt(receipt, legacy_digest)
    changed = receipt.model_copy(update={"execution_nonce": "execution-2"})
    assert stop_call_id(receipt) == stop_call_id(changed)
    assert stop_receipt_id(receipt) != stop_receipt_id(changed)
    assert accept_stop_id(stop_receipt_id(receipt)) != accept_stop_id(stop_receipt_id(changed))
    assert not matches_stop_receipt(changed, digest)
    assert not matches_stop_receipt(receipt, "unknown")


@pytest.mark.parametrize("version", [4, 5])
def test_journal_current_and_old_formats_enforce_the_same_fences(version: Literal[4, 5]) -> None:
    old = previous_journal(15)
    record = decode_journal(json.dumps(old), root_identity=ROOT)
    data: dict[str, JsonValue] = old if version == 4 else json.loads(record.model_dump_json())
    terminal = data["terminal"]
    assert isinstance(terminal, dict)
    terminal["job_id"] = 9
    with pytest.raises(ValueError):
        decode_journal(json.dumps(data), root_identity=ROOT)
