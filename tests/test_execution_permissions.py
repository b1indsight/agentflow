from pathlib import PurePosixPath as Path

import pytest

from app.worker.execution.amp_agent import AmpCodingAgent
from app.worker.execution.codex import CodexAgent
from app.worker.execution.permissions import (
    FULL_ACCESS,
    PathRule,
    PermissionPolicyError,
    PermissionProfile,
    RestrictedFileSystem,
    RestrictedNetwork,
    TcpRule,
    UnsupportedPermissionProfile,
    allows_tcp,
    resolve_file_access,
)


def test_path_precedence_and_boundaries():
    root = Path("/work")
    rules = (
        PathRule("workspace", Path("."), "write"),
        PathRule("workspace", Path("lib"), "deny"),
        PathRule("absolute", root / "lib", "write"),
        PathRule("workspace", Path("lib/x"), "read"),
    )
    for order in (rules, rules[::-1]):
        policy = RestrictedFileSystem(order)
        for name, expected in [("lib", "deny"), ("lib/x/file", "read"), ("library", "write")]:
            assert resolve_file_access(policy, root / name, workspace=root) == expected
        assert resolve_file_access(policy, Path("/other"), workspace=root) == "deny"
    assert resolve_file_access(RestrictedFileSystem(), root, workspace=root) == "deny"
    assert resolve_file_access(FULL_ACCESS.file_system, root, workspace=root) == "write"


def test_symlink_requires_both_path_and_target_permissions(tmp_path):
    # An alias must neither bypass a target restriction nor discard its own restriction.
    real = tmp_path / "work"
    real.mkdir()
    workspace = tmp_path / "workspace-alias"
    workspace.symlink_to(real)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (real / "via").symlink_to("second")
    (real / "second").symlink_to(outside)
    path = workspace / "via/new/file"
    for link, target, expected in [
        ("write", "write", "write"),
        ("read", "write", "read"),
        ("write", "read", "read"),
        ("deny", "write", "deny"),
        ("write", "deny", "deny"),
    ]:
        policy = RestrictedFileSystem(
            (PathRule("workspace", Path("via"), link), PathRule("absolute", outside, target))
        )
        assert resolve_file_access(policy, path, workspace=workspace) == expected

    policy = RestrictedFileSystem((PathRule("workspace", Path("."), "write"),))
    assert resolve_file_access(policy, path, workspace=workspace) == "deny"
    assert resolve_file_access(policy, workspace / "new", workspace=workspace) == "write"
    (real / "broken").symlink_to("absent")
    (real / "loop").symlink_to("loop")
    for bad in ("broken/file", "loop/file"):
        assert resolve_file_access(policy, workspace / bad, workspace=workspace) == "deny"


def test_tcp_deny_overrides_allow_and_ports_are_exact():
    rules = (
        TcpRule("*.example.com", 443, "deny"),
        TcpRule("api.example.com", 443, "allow"),
        TcpRule("*.example.com", 80, "allow"),
        TcpRule("::1", 443, "allow"),
    )
    for order in (rules, rules[::-1]):
        policy = RestrictedNetwork(order)
        assert not allows_tcp(policy, "api.example.com", 443)
        assert allows_tcp(policy, "Deep.API.Example.Com.", 80)
        assert not allows_tcp(policy, "api.example.com", 22)
        assert not allows_tcp(policy, "example.com", 80)
        assert not allows_tcp(policy, "badexample.com", 80)
        assert allows_tcp(policy, "0:0:0:0:0:0:0:1", 443)
    assert not allows_tcp(RestrictedNetwork(), "localhost", 443)
    assert allows_tcp(FULL_ACCESS.network, "localhost", 443)


@pytest.mark.parametrize("path", ["relative", "/a/../b", "//host/a", "/a\0b"])
def test_full_access_validates_paths(path):
    with pytest.raises(PermissionPolicyError):
        resolve_file_access(FULL_ACCESS.file_system, Path(path), workspace=Path("/"))


@pytest.mark.parametrize(
    "host,port",
    [
        ("host..", 1),
        ("a:80", 80),
        ("[::1]", 1),
        ("::1%lo", 1),
        ("*.example.org", 1),
        ("K.example", 1),
        ("host", True),
        ("host", 0),
        ("host", 65536),
    ],
)
def test_full_access_validates_tcp(host, port):
    with pytest.raises(PermissionPolicyError):
        allows_tcp(FULL_ACCESS.network, host, port)


@pytest.mark.parametrize(
    "construct,args",
    [
        (PathRule, ("workspace", Path("/"), "deny")),
        (PathRule, ("absolute", Path("."), "deny")),
        (PathRule, ("cwd", Path("."), "read")),
        (PathRule, ("absolute", Path("/"), "rw")),
        (RestrictedNetwork, ([],)),
        (RestrictedFileSystem, ((None,),)),
        (TcpRule, ("Host.", 1, "allow")),
        (TcpRule, ("*.127.0.0.1", 80, "allow")),
        (TcpRule, ("example.com", 1, "read")),
    ],
)
def test_invalid_rules_are_rejected(construct, args):
    with pytest.raises(PermissionPolicyError):
        construct(*args)


@pytest.mark.parametrize("agent", [CodexAgent, AmpCodingAgent])
@pytest.mark.parametrize(
    "permissions",
    [
        PermissionProfile(file_system=RestrictedFileSystem()),
        PermissionProfile(network=RestrictedNetwork()),
        PermissionProfile(RestrictedFileSystem(), RestrictedNetwork()),
    ],
)
def test_trusted_host_cannot_run_restricted_profile(agent, permissions):
    # A trusted host still has no backend that can enforce the requested restrictions.
    with pytest.raises(UnsupportedPermissionProfile):
        agent(trusted_host=True, permissions=permissions)
