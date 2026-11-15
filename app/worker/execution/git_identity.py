"""Persistent workspace-only Git attribution, independent of push authentication."""

import os
from pathlib import Path

from app.worker.execution.credentials import inherited_agent_environment
from app.worker.execution.vcs import VcsVerificationError, _run

_HOOK = '''#!/bin/sh
set -e
original=$(git config --path --get flowtool.originalHooksPath)
hook=${0##*/}
if [ -x "$original/$hook" ]; then
    "$original/$hook" "$@"
fi
if [ "$hook" = commit-msg ]; then
    bot=$(git config --worktree --get flowtool.botIdentity)
    for role in AUTHOR COMMITTER; do
        identity=$(git var "GIT_${role}_IDENT")
        if [ "${identity%>*}>" != "$bot" ]; then
            echo "Git $role identity does not match the workspace App bot" >&2
            exit 1
        fi
    done
    coauthor=$(git config --get flowtool.coauthor)
    if [ -n "$coauthor" ] && git stripspace --strip-comments < "$1" | grep -q .; then
        git interpret-trailers --in-place --if-exists addIfDifferent \\
            --trailer "Co-authored-by: $coauthor" "$1"
    fi
fi
'''


async def configure_git_identity(path: Path, bot_login: str, bot_user_id: int) -> None:
    """Initialize this worktree's bot attribution once; later commits check it in the hook.

    The caller holds the repository lock: enabling worktreeConfig changes common metadata,
    but identity, saved coauthor and hooksPath are written only to config.worktree.
    """
    if not (path / ".git").exists():
        return  # Native non-colocated jj repositories have no workspace Git config to change.
    environment = inherited_agent_environment(os.environ)

    async def git(*args: str) -> str:
        code, stdout, _ = await _run("git", *args, cwd=path, stage="identity",
                                     environment=environment)
        if code == 1 and args[0] == "config" and "--get" in args:
            return ""
        if code:
            raise VcsVerificationError("identity", f"git {args[0]} failed with exit {code}")
        return stdout.strip()

    bot_email = f"{bot_user_id}+{bot_login}@users.noreply.github.com"
    bot_identity = f"{bot_login} <{bot_email}>"
    worktree_config = await git("config", "--bool", "--get", "extensions.worktreeConfig") == "true"
    initialized = (await git("config", "--worktree", "--get", "flowtool.botIdentity")
                   if worktree_config else "")
    if initialized:
        if initialized != bot_identity:
            raise VcsVerificationError("identity", "workspace bot identity does not match the current App")
        return

    git_dir = Path(await git("rev-parse", "--absolute-git-dir"))
    hooks = git_dir / "flowtool-hooks"
    original = await git("config", "--path", "--get", "flowtool.originalHooksPath")
    if not original:
        name = (await git("config", "--get", "author.name")
                or await git("config", "--get", "user.name"))
        email = (await git("config", "--get", "author.email")
                 or await git("config", "--get", "user.email"))
        coauthor = f"{name} <{email}>" if name and email else ""
        original = await git("rev-parse", "--path-format=absolute", "--git-path", "hooks")
    else:
        coauthor = await git("config", "--get", "flowtool.coauthor")

    if not worktree_config:
        common = Path(await git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        # Git requires these main-worktree-only settings to leave common config when enabling it.
        for key in ("core.worktree", "core.bare"):
            value = await git("config", "--local", "--get", key)
            if value and (key != "core.bare" or value.lower() == "true"):
                await git("config", "--file", str(common / "config.worktree"), key, value)
                await git("config", "--local", "--unset-all", key)
        await git("config", "--local", "extensions.worktreeConfig", "true")

    if coauthor == bot_identity:
        coauthor = ""
    await git("config", "--worktree", "flowtool.coauthor", coauthor)
    await git("config", "--worktree", "flowtool.originalHooksPath", original)
    for section in ("user", "author", "committer"):
        await git("config", "--worktree", f"{section}.name", bot_login)
        await git("config", "--worktree", f"{section}.email", bot_email)

    hooks.mkdir(mode=0o700, exist_ok=True)
    source = Path(original)
    names = {p.name for p in source.iterdir() if p.is_file() and os.access(p, os.X_OK)
             and not p.name.endswith(".sample")} if source.is_dir() else set()
    for name in names | {"commit-msg"}:
        hook = hooks / name
        hook.write_text(_HOOK)
        hook.chmod(0o700)
    await git("config", "--worktree", "core.hooksPath", str(hooks))
    await git("config", "--worktree", "flowtool.botIdentity", bot_identity)
