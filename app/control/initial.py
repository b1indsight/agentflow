"""Read-only INITIAL delivery checks shared by result application and PR reopening."""

from app.models import Attempt, DeliveryState, FlowDelivery, FlowToolCall, Job
from app.protocol import InitialResult


def initial_delivery_mismatch(
    job: Job,
    attempt: Attempt,
    result: InitialResult,
    delivery: FlowDelivery | None,
    tool_call: FlowToolCall | None,
) -> str | None:
    """Validate the same INITIAL/tool invariants before recording any PR creation intent."""
    if delivery is None:
        return "delivery is missing"
    if delivery.state not in {DeliveryState.OPENING, DeliveryState.OPEN}:
        return f"delivery state is {delivery.state.value}, expected opening or open"
    if result.vcs_backend is None:
        return "verified VCS backend is missing"
    if not result.pushed:
        return "worker did not report a verified push"
    if result.branch != delivery.branch:
        return "result branch does not match delivery branch"
    if not result.commit_sha:
        return "verified commit SHA is missing"
    if result.commit_sha != result.remote_sha:
        return "local and remote commit SHAs differ"
    if result.commit_sha != delivery.head_sha:
        return "result commit SHA does not match delivery head"
    if not result.open_pr_call_id:
        return "flow.open_pr call id is missing"
    if tool_call is None:
        return "flow.open_pr call is not persisted"
    if (tool_call.flow_id, tool_call.attempt_id, tool_call.job_id) != (
        job.flow_id,
        attempt.id,
        job.id,
    ):
        return "flow.open_pr call fence does not match the result"
    if result.delivery_receipt is None:
        return "delivery receipt is missing"
    if tool_call.response != result.delivery_receipt:
        return "delivery receipt does not match the persisted tool response"
    if result.delivery_receipt.head_sha != delivery.head_sha:
        return "prepared delivery head SHA does not match delivery"
    return None
