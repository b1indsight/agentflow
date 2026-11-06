"""Agent environment inheritance and assignment-scoped Git credentials."""

from collections.abc import Mapping

from pydantic import SecretStr


def inherited_agent_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Keep native CLI/MCP configuration without ambient task or Git authentication state.

    Custom credentials deliberately remain available to skills and MCP servers. This
    filtering does not isolate a full-access agent from the worker account's files.
    """
    identity = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
    parent_agent = {"AMP_CURRENT_THREAD_ID", "AMP_THREAD_ID", "CODEX_THREAD_ID", "CLAUDECODE"}
    return {
        key: value for key, value in source.items()
        if key not in parent_agent
        and (key in identity or not key.startswith(("FLOWTOOL_", "GIT_", "GH_", "GITHUB_", "SSH_")))
    }


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
