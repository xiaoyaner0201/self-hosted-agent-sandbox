"""Opt-in git checkpoints of a sandbox session's code tree, kept in the host repository.

After each command (once its edits are pulled back) the host work tree is recorded as a commit
on a hidden ref ``refs/sbx/<session>/<seq>``, built with plumbing only (a throw-away index,
``write-tree``, ``commit-tree``, ``update-ref``). The user's branches, HEAD and index are never
touched and no hook runs. The first checkpoint's parent is the HEAD of that moment, every later
one's parent is the previous checkpoint, so ``git log refs/sbx/<session>/<seq>`` is the session
history. A checkpoint can be checked out as a new detached worktree (:func:`fork_worktree`) and
a new sandbox session started there.

What a checkpoint holds: the files of the repository work tree as the host has them, tracked
and untracked, minus ``.gitignore``d paths and the directory names the sync never mirrors
(node_modules, .venv, build, ...). It is files only: API calls, database writes, pushes and other
effects outside the work tree cannot be rolled back by forking.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

REF_PREFIX = "refs/sbx"
_IDENT = {"GIT_AUTHOR_NAME": "agent-sandbox", "GIT_AUTHOR_EMAIL": "agent-sandbox@localhost",
          "GIT_COMMITTER_NAME": "agent-sandbox", "GIT_COMMITTER_EMAIL": "agent-sandbox@localhost"}
_SEQ_WIDTH = 6
# Recording and forking run no hook (reference-transaction, post-checkout) of the user's repo.
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def session_label(session: str) -> str:
    """Same normalization the claims use; always a valid single ref component."""
    s = "".join(ch if ch.isalnum() else "-" for ch in str(session).lower())[:50].strip("-")
    return s if s and _LABEL_RE.match(s) else "default"


def _host_git_env() -> dict:
    """Same hardening as host-side commands in core: ignore discovered bare repos, no ext::."""
    env = dict(os.environ)
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for k, v in (("safe.bareRepository", "explicit"), ("protocol.ext.allow", "never")):
        env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = k, v
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    env.pop("GIT_INDEX_FILE", None)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(repo: str, *args: str, env: dict | None = None, inp: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    e = _host_git_env()
    if env:
        e.update(env)
    pre = []
    while args and args[0] == "-c":  # leading -c key=value pairs go before the subcommand
        pre += list(args[:2])
        args = args[2:]
    r = subprocess.run(["git", "-C", repo, *pre, *args], capture_output=True, text=True, env=e,
                       input=inp, timeout=300)
    if check and r.returncode != 0:
        raise RuntimeError(f"agent-sandbox checkpoint: git {args[0]} failed rc={r.returncode}: "
                           f"{r.stderr.strip()[:300]}")
    return r


def repo_root(path: str) -> str | None:
    """Top level of the work tree containing ``path`` (None when not inside one)."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if not d or not os.path.isdir(d):
        return None
    r = _git(d, "rev-parse", "--show-toplevel", check=False)
    return r.stdout.strip() or None if r.returncode == 0 else None


def find_repo(code_dirs) -> str | None:
    """The repository checkpointed for a session: that of the first code dir inside one."""
    for d in code_dirs or ():
        root = repo_root(d)
        if root:
            return root
    return None


def _head(repo: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return r.stdout.strip() or None if r.returncode == 0 else None


def list_checkpoints(repo: str, session: str) -> list[dict]:
    """[{"seq", "sha", "ref", "parent"}] of a session, oldest first."""
    base = f"{REF_PREFIX}/{session_label(session)}/"
    r = _git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(parent)", base)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        tail = parts[0][len(base):]
        if not tail.isdigit():
            continue
        out.append({"seq": int(tail), "sha": parts[1], "ref": parts[0],
                    "parent": parts[2] if len(parts) > 2 else None})
    return sorted(out, key=lambda x: x["seq"])


def create_checkpoint(repo: str, session: str, *, skip_dirs=(), message: str = "") -> dict:
    """Record the work tree of ``repo`` as the next checkpoint of ``session``.

    Returns {"seq", "sha", "ref", "parent"}. Leaves HEAD, branches and the index untouched.
    """
    label = session_label(session)
    existing = list_checkpoints(repo, session)
    seq = existing[-1]["seq"] + 1 if existing else 1
    parent = existing[-1]["sha"] if existing else _head(repo)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()
    fd, tmp_index = tempfile.mkstemp(prefix="sbx-ckpt-index-")
    os.close(fd)
    try:
        real_index = os.path.join(gitdir, "index")
        if os.path.isfile(real_index):
            shutil.copyfile(real_index, tmp_index)  # stat cache: only changed files are rehashed
        else:
            os.unlink(tmp_index)  # git creates it; an empty file is not a valid index
        env = {"GIT_INDEX_FILE": tmp_index}
        excludes = [f":(exclude,glob)**/{d}/**" for d in sorted(skip_dirs)]
        _git(repo, "add", "-A", "--", ":/", *excludes, env=env)
        # Skip dirs that were already tracked are dropped too: the checkpoint never claims
        # to hold a dependency/build tree the sandbox rebuilt but never sent back.
        tracked = _git(repo, "ls-files", "-z", "--cached", "--", *[f":(glob)**/{d}/**" for d in sorted(skip_dirs)],
                       env=env).stdout if skip_dirs else ""
        paths = [p for p in tracked.split("\0") if p]
        if paths:
            _git(repo, "update-index", "-z", "--force-remove", "--stdin", env=env,
                 inp="\0".join(paths) + "\0")
        tree = _git(repo, "write-tree", env=env).stdout.strip()
    finally:
        for p in (tmp_index, tmp_index + ".lock"):
            if os.path.exists(p):
                os.unlink(p)
    msg = message or f"sbx checkpoint {label} #{seq}"
    args = ["commit-tree", tree, "-m", msg] + (["-p", parent] if parent else [])
    sha = _git(repo, *args, env=_IDENT).stdout.strip()
    ref = f"{REF_PREFIX}/{label}/{seq:0{_SEQ_WIDTH}d}"
    # Create-only (expected old value = none): a concurrent writer fails instead of overwriting.
    _git(repo, *_NO_HOOKS, "update-ref", "-m", msg, ref, sha, "0" * len(sha))
    return {"seq": seq, "sha": sha, "ref": ref, "parent": parent}


def prune_checkpoints(repo: str, session: str) -> int:
    """Delete every checkpoint ref of ``session``; the commits become unreachable for git gc."""
    n = 0
    for c in list_checkpoints(repo, session):
        _git(repo, *_NO_HOOKS, "update-ref", "-d", c["ref"], c["sha"])
        n += 1
    return n


def fork_worktree(repo: str, sha: str, dest: str) -> str:
    """``git worktree add --detach dest sha``; returns the absolute new work tree."""
    dest = os.path.abspath(dest)
    if os.path.exists(dest) and os.listdir(dest):
        raise RuntimeError(f"agent-sandbox fork: {dest} exists and is not empty")
    full = _git(repo, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}").stdout.strip()
    _git(repo, *_NO_HOOKS, "worktree", "add", "--detach", dest, full)
    return dest
