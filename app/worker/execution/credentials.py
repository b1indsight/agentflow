"""Ephemeral Git credential environment shared by CodingAgent and remote-ref verification."""

from app.domain.models import CodingExecutionContext


def git_credential_environment(context: CodingExecutionContext) -> dict[str, str]:
    """Supply the current capability without placing a token in argv or repository config."""
    if context.push_token is None:
        return {}
    return {
        "FLOWTOOL_PUSH_TOKEN": context.push_token.get_secret_value(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.username",
        "GIT_CONFIG_VALUE_0": "x-access-token",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": "!f() { echo password=$FLOWTOOL_PUSH_TOKEN; }; f",
    }
