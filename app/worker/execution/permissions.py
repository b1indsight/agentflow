"""Evaluate worker path and TCP policy without opening file contents or connecting.

These decisions describe requested permissions; they do not sandbox a process.
"""

import re
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import PosixPath, PurePosixPath
from typing import Literal

FileAccess = Literal["read", "write", "deny"]
_PRIORITY = {"read": 0, "write": 1, "deny": 2}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class PermissionPolicyError(ValueError):
    """Invalid policy declaration or query."""


class UnsupportedPermissionProfile(RuntimeError):
    """The adapter cannot enforce the requested permissions."""


def _validate_path(path: PurePosixPath, *, absolute: bool) -> None:
    if (
        not isinstance(path, PurePosixPath)
        or path.is_absolute() != absolute
        or path.root == "//"
        or ".." in path.parts
        or "\x00" in str(path)
    ):
        raise PermissionPolicyError("Invalid POSIX path or anchor")


@dataclass(frozen=True, slots=True)
class PathRule:
    base: Literal["workspace", "absolute"]
    path: PurePosixPath
    access: FileAccess

    def __post_init__(self) -> None:
        if self.base not in ("workspace", "absolute") or self.access not in (
            "read",
            "write",
            "deny",
        ):
            raise PermissionPolicyError("Invalid path rule base or access")
        _validate_path(self.path, absolute=self.base == "absolute")


@dataclass(frozen=True, slots=True)
class UnrestrictedFileSystem:
    """Grant write (including read) for every valid path."""


@dataclass(frozen=True, slots=True)
class RestrictedFileSystem:
    rules: tuple[PathRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or not all(
            isinstance(r, PathRule) for r in self.rules
        ):
            raise PermissionPolicyError("Expected a tuple of path rules")


FileSystemPolicy = UnrestrictedFileSystem | RestrictedFileSystem


def _lexical_access(
    policy: RestrictedFileSystem, path: PurePosixPath, workspace: PurePosixPath
) -> FileAccess:
    best = (0, -1)
    access: FileAccess = "deny"
    for rule in policy.rules:
        # Joining an absolute rule discards the workspace prefix.
        prefix = workspace / rule.path
        rank = (len(prefix.parts), _PRIORITY[rule.access])
        if path.is_relative_to(prefix) and rank > best:
            best, access = rank, rule.access
    return access


def _resolved_path(path: PurePosixPath) -> PosixPath:
    """Resolve the deepest existing entry strictly, retaining any not-yet-created suffix."""
    entry = PosixPath(path)
    while True:
        try:
            entry.lstat()  # A dangling link exists: strict resolution below must reject it.
        except FileNotFoundError:
            if entry == entry.parent:
                raise
            entry = entry.parent
        else:
            return entry.resolve(strict=True) / path.relative_to(entry)


def resolve_file_access(
    policy: FileSystemPolicy, path: PurePosixPath, *, workspace: PurePosixPath
) -> FileAccess:
    """Intersect requested and resolved path grants on this worker; not a race-free I/O guard."""
    _validate_path(path, absolute=True)
    _validate_path(workspace, absolute=True)
    if isinstance(policy, UnrestrictedFileSystem):
        return "write"
    requested = _lexical_access(policy, path, workspace)
    if requested == "deny":
        return "deny"
    try:
        target = _resolved_path(path)
        root = _resolved_path(workspace)
    except (OSError, RuntimeError):  # Python 3.12 reports symlink loops as RuntimeError.
        return "deny"
    resolved = _lexical_access(policy, target, root)
    return resolved if requested == "write" or resolved == "deny" else "read"


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise PermissionPolicyError("Invalid TCP destination port")


def _canonical_host(host: str) -> tuple[str, bool]:
    """Return the canonical host and whether DNS wildcard matching applies."""
    if not isinstance(host, str) or not host.isascii() or "%" in host:
        raise PermissionPolicyError("Invalid TCP destination host")
    try:
        return str(ip_address(host)), False
    except ValueError:
        pass
    name = host.lower().removesuffix(".")
    labels = name.split(".")
    if (
        len(name) > 253
        or all(label.isdigit() for label in labels)
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise PermissionPolicyError("Invalid TCP destination host")
    return name, True


@dataclass(frozen=True, slots=True)
class TcpRule:
    host: str
    port: int
    action: Literal["allow", "deny"]

    def __post_init__(self) -> None:
        _validate_port(self.port)
        if self.action not in ("allow", "deny") or not isinstance(self.host, str):
            raise PermissionPolicyError("Invalid TCP rule")
        suffix = self.host.removeprefix("*.")
        canonical, dns = _canonical_host(suffix)
        if canonical != suffix or (suffix != self.host and not dns):
            raise PermissionPolicyError("Expected canonical host or wildcard DNS suffix")


@dataclass(frozen=True, slots=True)
class UnrestrictedNetwork:
    """Allow every valid TCP destination."""


@dataclass(frozen=True, slots=True)
class RestrictedNetwork:
    rules: tuple[TcpRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or not all(isinstance(r, TcpRule) for r in self.rules):
            raise PermissionPolicyError("Expected a tuple of TCP rules")


NetworkPolicy = UnrestrictedNetwork | RestrictedNetwork


def allows_tcp(policy: NetworkPolicy, host: str, port: int) -> bool:
    """Match a TCP destination without DNS resolution. Any matching deny wins."""
    _validate_port(port)
    name, dns = _canonical_host(host)
    if isinstance(policy, UnrestrictedNetwork):
        return True
    allowed = False
    for rule in policy.rules:
        if rule.port == port and (
            name == rule.host
            or (dns and rule.host.startswith("*.") and name.endswith(rule.host[1:]))
        ):
            if rule.action == "deny":
                return False
            allowed = True
    return allowed


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    file_system: FileSystemPolicy = UnrestrictedFileSystem()
    network: NetworkPolicy = UnrestrictedNetwork()

    def __post_init__(self) -> None:
        if not isinstance(self.file_system, FileSystemPolicy) or not isinstance(
            self.network, NetworkPolicy
        ):
            raise PermissionPolicyError("Invalid filesystem or network policy")

    @property
    def is_unrestricted(self) -> bool:
        return isinstance(self.file_system, UnrestrictedFileSystem) and isinstance(
            self.network, UnrestrictedNetwork
        )


FULL_ACCESS = PermissionProfile()


def require_supported_permissions(
    profile: PermissionProfile, *, agent: Literal["Codex", "Amp"]
) -> None:
    """Reject restricted execution until the adapter has an enforcement backend."""
    if not profile.is_unrestricted:
        raise UnsupportedPermissionProfile(f"{agent} has no restricted execution backend")
