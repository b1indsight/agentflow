"""Ephemeral Git credential environment shared by CodingAgent and remote-ref verification."""

from pydantic import SecretStr


def git_credential_environment(push_token: SecretStr | None) -> dict[str, str]:
    """Supply the current capability without placing a token in argv or repository config."""
    if push_token is None:
        return {}
    return {
        "FLOWTOOL_PUSH_TOKEN": push_token.get_secret_value(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.username",
        "GIT_CONFIG_VALUE_0": "x-access-token",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_2": '!f() { [ "$1" = get ] && printf "password=%s\\n" "$FLOWTOOL_PUSH_TOKEN"; }; f',
    }
