"""Queue/partition access rules (servers.yaml `partitions.<p>.access`) and the requesting identity.

Static rules: a partition without `access:` is open to everyone in the group; with one, a user
may use it when their Unix user name is in `users` OR they belong to ANY group in `groups`.

Identity (who is asking) matters because the process that evaluates the rules is not always the
person submitting the job: on the shared MCP/HTTP server the process runs as a service account.
Hence:

* `queue_access()` / `check_resources()` never guess: they use the `user`/`groups` passed in, or
  the local process identity only with `use_local_identity=True`.
* `client_identity()` resolves the identity for callers that act on behalf of a client
  (the input-checker bridge, MCP tools, the CLI), in this order:
    1. an explicit `identity=(user, groups)` argument,
    2. the context variable `CLIENT_IDENTITY` (set per request by the REST/MCP layer, e.g. with
       `with client_identity_scope("alice", ["chem"]): ...`),
    3. environment variables `RAG_DRG_CLIENT_USER` and `RAG_DRG_CLIENT_GROUPS` (comma-separated),
    4. the local process identity (`getpass.getuser()`, `os.getgroups()`), but only when
       `local_identity_allowed()`, i.e. `RAG_DRG_SERVER_MODE` is unset/false. A shared server
       sets `RAG_DRG_SERVER_MODE=1` so its service account is never taken for the user.
  Anything missing stays None (= unknown), which makes access checks report "unknown", not "denied".
"""

from __future__ import annotations

import contextlib
import getpass
import os
from contextvars import ContextVar
from typing import Iterator, List, Optional, Tuple

from .model import ACCOUNT_NAME_RE, Partition, Server

Identity = Tuple[Optional[str], Optional[List[str]]]

CLIENT_IDENTITY: ContextVar[Identity | None] = ContextVar("rag_drg_client_identity", default=None)
ENV_USER = "RAG_DRG_CLIENT_USER"
ENV_GROUPS = "RAG_DRG_CLIENT_GROUPS"
ENV_SERVER_MODE = "RAG_DRG_SERVER_MODE"
_FALSE = ("", "0", "false", "no", "off")


def local_identity_allowed() -> bool:
    """False inside a shared server (RAG_DRG_SERVER_MODE set to anything but 0/false/no/off)."""
    return os.environ.get(ENV_SERVER_MODE, "").strip().lower() in _FALSE


def local_identity() -> tuple[str | None, list[str] | None]:
    """(user name, group names) of this process; (None, None) where they cannot be determined."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry / no USER variable
        user = None
    try:
        import grp

        gids = set(os.getgroups())
        try:
            gids.add(os.getgid())
        except OSError:
            pass
        groups = []
        for gid in sorted(gids):
            try:
                groups.append(grp.getgrgid(gid).gr_name)
            except KeyError:
                continue
    except (ImportError, OSError):  # e.g. Windows
        groups = None
    return user, groups


def _clean_user(user) -> str | None:
    if user is None:
        return None
    user = str(user).strip()
    return user if user and ACCOUNT_NAME_RE.match(user) else None


def _clean_groups(groups) -> list[str] | None:
    if groups is None:
        return None
    if isinstance(groups, str):
        groups = groups.split(",")
    return [g for g in (str(x).strip() for x in groups) if g and ACCOUNT_NAME_RE.match(g)]


def client_identity(identity: Identity | None = None, *, with_source: bool = False):
    """The requesting user's (user, groups); see the module docstring for the order.

    With `with_source=True` returns (user, groups, source), source one of
    "explicit", "context", "env", "local", or None when nothing is known.
    """
    user = groups = None
    source = None
    ctx = CLIENT_IDENTITY.get()
    if identity is not None:
        user, groups, source = identity[0], identity[1], "explicit"
    elif ctx is not None:
        user, groups, source = ctx[0], ctx[1], "context"
    elif os.environ.get(ENV_USER) or os.environ.get(ENV_GROUPS):
        user = os.environ.get(ENV_USER) or None
        groups = os.environ.get(ENV_GROUPS) or None
        source = "env"
    elif local_identity_allowed():
        user, groups = local_identity()
        source = "local"
    user, groups = _clean_user(user), _clean_groups(groups)
    if user is None and groups is None:
        source = None
    return (user, groups, source) if with_source else (user, groups)


@contextlib.contextmanager
def client_identity_scope(user: str | None, groups: list[str] | str | None) -> Iterator[None]:
    """Set CLIENT_IDENTITY for the duration of a request (thread/async-task local)."""
    token = CLIENT_IDENTITY.set((user, _clean_groups(groups)))
    try:
        yield
    finally:
        CLIENT_IDENTITY.reset(token)


# ----------------------------------------------------------------- evaluation

def _resolve_partition(server: Server, partition: str | Partition | None) -> Partition | None:
    if isinstance(partition, Partition):
        return partition
    if partition is None:
        return server.default_partition
    return server.partitions.get(partition)


def queue_access(server: Server, partition: str | Partition | None, user: str | None = None,
                 groups: list[str] | None = None, *, use_local_identity: bool = False) -> dict:
    """May this user use `partition` of `server` according to servers.yaml?

    Returns {"allowed": True | False | None, "reason": str, "partition": name, "notes": str|None};
    None means a rule exists but the user/groups needed to decide are unknown.
    `use_local_identity=True` fills a missing user/groups from this process (only correct when
    the process runs as the person submitting, e.g. the CLI on the cluster itself).
    """
    part = _resolve_partition(server, partition)
    if part is None:
        return {"allowed": False, "partition": partition,
                "reason": f"unknown partition {partition!r} on {server.name}", "notes": None}
    if use_local_identity and (user is None or groups is None):
        lu, lg = local_identity()
        user = user if user is not None else lu
        groups = groups if groups is not None else lg
    where = f"{server.name}:{part.name}"
    acc = part.access
    if acc is None:
        return {"allowed": True, "partition": part.name, "reason": f"{where} has no access restriction",
                "notes": None}
    out = {"partition": part.name, "notes": acc.notes}
    if user is not None and user in acc.users:
        return {**out, "allowed": True, "reason": f"user {user} is listed for {where}"}
    if groups is not None:
        hit = [g for g in acc.groups if g in set(groups)]
        if hit:
            return {**out, "allowed": True, "reason": f"member of group {hit[0]}, which may use {where}"}
    # Not admitted by what we know: denied only if the unknown part could not have admitted.
    user_unknown = user is None and bool(acc.users)
    groups_unknown = groups is None and bool(acc.groups)
    hint = f" ({acc.notes})" if acc.notes else ""
    if user_unknown or groups_unknown:
        missing = " and ".join(x for x, u in (("user name", user_unknown), ("groups", groups_unknown)) if u)
        return {**out, "allowed": None,
                "reason": f"{where} is restricted to {acc.describe()}; cannot tell whether you may use it "
                          f"(your {missing} unknown){hint}"}
    who = f"user {user}" if user else "you"
    return {**out, "allowed": False,
            "reason": f"{where} is restricted to {acc.describe()}; {who} "
                      f"{'(groups: ' + ', '.join(groups) + ') ' if groups else ''}may not use it{hint}"}


def access_table(server: Server, user: str | None, groups: list[str] | None) -> list[dict]:
    """queue_access() for every partition of a server."""
    return [queue_access(server, p, user, groups) for p in server.partitions.values()]


# ----------------------------------------------------------------- for callers acting on behalf of a client

def identity_for(server: Server, identity: Identity | None = None
                 ) -> tuple[str | None, list[str] | None, str | None, bool]:
    """(user, groups, source, authoritative) of the requesting client for checks about `server`.

    The local process identity is authoritative only when this machine is the cluster
    (scheduler `local`, or running on the login host itself): a laptop's user name and groups
    usually differ from the person's account on the cluster.
    """
    from .cluster import is_local

    user, groups, source = client_identity(identity, with_source=True)
    authoritative = source is not None and (source != "local" or is_local(server))
    return user, groups, source, authoritative


def access_problem(server: Server, partition: str | Partition | None,
                   identity: Identity | None = None) -> dict | None:
    """{severity, message} about the requesting client using `partition`, or None (open/allowed).

    error: denied for an authoritative identity; warning: denied, but judged from this machine's
    identity while the cluster is elsewhere; info: restricted and the identity is unknown.
    """
    part = _resolve_partition(server, partition)
    if part is None or part.access is None:
        return None
    user, groups, _source, authoritative = identity_for(server, identity)
    r = queue_access(server, part, user, groups)
    if r["allowed"] is True:
        return None
    how = (f"pass your cluster user/groups (RAG_DRG_CLIENT_USER / RAG_DRG_CLIENT_GROUPS) or check with "
           f"`rag-drg servers access {server.name} --live`")
    if r["allowed"] is None:
        return {"severity": "info", "message": f"{r['reason']}; {how}"}
    if not authoritative:
        return {"severity": "warning", "message": f"{r['reason']} - judged from this machine's identity, which "
                                                  f"may differ from your account on {server.name}; {how}"}
    return {"severity": "error", "message": r["reason"]}
