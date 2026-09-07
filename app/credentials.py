"""Ephemeral Git credential environment shared by CodeAgent and remote-ref verification."""

from app.models import CodeAgentContext


def git_credential_environment(context: CodeAgentContext) -> dict[str, str]:
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
