"""Versioned Judge subprocess request and auditable decision, containing no credentials."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.context_models import ReviewContext
from app.domain.models import TriageVerdict, Verdict

ADAPTER_VERSION: Literal["bub-0.4.1/v1"] = "bub-0.4.1/v1"
Failure = Literal[
    "missing-configuration", "adapter-unavailable", "start-failed", "timeout",
    "output-limit", "nonzero-exit", "empty-output", "invalid-envelope", "invalid-verdict",
    "model-error", "incomplete-context",
]


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    session_id: str
    context: ReviewContext


class JudgeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    adapter_version: Literal["bub-0.4.1/v1"] = ADAPTER_VERSION
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str
    model: str
    verdict: TriageVerdict
    failure: Failure | None = None


def waiting(request: JudgeRequest, model: str, failure: Failure) -> JudgeDecision:
    """Return a sanitized operational failure without serializing provider errors or output."""
    return JudgeDecision(
        context_digest=request.context.digest, session_id=request.session_id, model=model,
        verdict=TriageVerdict(verdict=Verdict.WAIT, reason=failure), failure=failure,
    )


def strict_json(raw: str | bytes) -> object:
    """Reject duplicate keys and non-JSON numeric constants as well as trailing output."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
