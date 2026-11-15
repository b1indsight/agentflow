"""Frozen review input and auditable decisions, containing no credentials."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.context_models import ReviewContext
from app.domain.inference_models import AdapterVersion, FailureCode
from app.domain.models import TriageVerdict, Verdict

ADAPTER_VERSION: Literal["bub-0.4.1/v1"] = "bub-0.4.1/v1"
Failure = Literal[
    "missing-configuration", "adapter-unavailable", "start-failed", "timeout",
    "output-limit", "nonzero-exit", "empty-output", "invalid-envelope", "invalid-verdict",
    "model-error", "incomplete-context",
] | FailureCode


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    session_id: str
    context: ReviewContext


class JudgeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    adapter_version: AdapterVersion = ADAPTER_VERSION
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str
    model: str | None
    verdict: TriageVerdict
    failure: Failure | None = None


def waiting(request: JudgeRequest, model: str | None, failure: Failure, *,
            adapter_version: str = "review/v1") -> JudgeDecision:
    """Return a sanitized operational failure without serializing provider errors or output."""
    return JudgeDecision(
        context_digest=request.context.digest, session_id=request.session_id, model=model,
        adapter_version=adapter_version,
        verdict=TriageVerdict(verdict=Verdict.WAIT, reason=failure), failure=failure,
    )
