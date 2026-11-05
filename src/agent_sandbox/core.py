"""Core of self-hosted-agent-sandbox: one session = one sandbox claimed from a warm pool.

Runs commands inside a kubernetes-sigs/agent-sandbox Sandbox (gVisor on the cluster side) claimed
from a SandboxWarmPool. Transport = ``kubectl exec`` (spawn-per-call). No agent framework is
imported here; adapters (Hermes plugin, MCP server, CLI) build on :class:`Sandbox`.

What travels into the sandbox ("host" below = the machine the agent runs on):
* Environment: only the host variables named in ``forward_env`` (names or globs; empty by
  default, so nothing), never host-identity names that would break the sandbox shell (PATH,
  HOME, HOSTNAME, ...). The sandbox runs and builds code; credentials stay on the host. Re-written
  when it changed and sourced by every command, so a changed value is seen by the next command.
* Git config: an exact key allowlist (identity and everyday settings; no aliases, commands,
  helpers, http/url/include) of the host ~/.gitconfig, and of each repository's .git/config inside
  the code dirs (plus what git needs there: format, branches, remotes); values with user:pass@
  are dropped; if sanitizing fails the sandbox gets an empty file. Other files in the code dirs,
  sync paths and seed dirs are sent as they are.
* Declared files/dirs (``sync_paths``): pushed at the SAME absolute path, change-checked before
  every command. The host copy is the source of truth; sandbox edits are never copied back.
* Code (``code_dirs``; default = the task cwd when it is under /workspaces): pushed like declared
  files; after every command, files the sandbox created/changed are pulled back and files it
  deleted are deleted on the host, so the host tree stays the git source of truth.
* Host commands (``host_commands``): a command made only of these plus glue that cannot touch
  files (cd, echo, piped grep/jq/head/...), with no substitutions, subshells or file
  redirections, confined to the code dirs, runs on the host itself (git network operations, plus
  exact sub-commands you list) so the credentials they need stay out of the sandbox. Code is
  pulled back first and re-pushed after. Inside the sandbox the same tools are shell functions
  that explain this and exit 97.
* Pull-back into ``.git`` is an allowlist (objects, refs, logs, HEAD, index, packed-refs and the
  like); config, hooks, gitfiles and everything else stay as the host has them, and host-side
  git runs with ``safe.bareRepository=explicit``, so nothing built in the sandbox can plant a hook
  or config that a later host-side ``git push`` would run. Writes through host symlinks that leave
  the code dirs are refused.
* Lifecycle: one claim per session (kept between commands, deleted by :meth:`Sandbox.close`).
  The claim also carries lifecycle.shutdownTime = now + ``idle_ttl`` (default 1800 s), renewed
  while commands run, so the cluster reclaims it if the host process dies.
"""
from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import logging
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def bash_argv(cmd_string: str, login: bool = False) -> list[str]:
    return ["bash", "-l", "-c", cmd_string] if login else ["bash", "-c", cmd_string]


def _popen_bash(cmd: list[str], stdin_data: str | None = None) -> subprocess.Popen:
    """Spawn with merged stdout/stderr; stdin is written on a thread so large payloads can't deadlock."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    if stdin_data is not None:
        def _write():
            try:
                proc.stdin.buffer.write(stdin_data.encode("utf-8", "surrogateescape"))
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=_write, daemon=True).start()
    return proc


class _FileSync:
    """Push host files that changed (mtime+size) since the last sync; delete ones that vanished.

    ``files_fn`` returns (host_path, remote_path) pairs. ``synced`` maps host path -> signature
    of what the sandbox holds, and is shared with the pull-back so pulled files don't bounce."""

    def __init__(self, files_fn, push_fn, delete_fn):
        self._files_fn, self._push_fn, self._delete_fn = files_fn, push_fn, delete_fn
        self.synced: dict[str, tuple[int, int]] = {}
        self._remote: dict[str, str] = {}

    def sync(self, *, force: bool = False) -> None:
        current = {}
        for host, remote in self._files_fn():
            k = _mtime_key(host)
            if k is not None:
                current[host] = (remote, k)
        changed = [(h, r) for h, (r, k) in current.items() if force or self.synced.get(h) != k]
        gone = [self._remote[h] for h in self.synced if h not in current and h in self._remote]
        if changed:
            self._push_fn(changed)
        if gone:
            self._delete_fn(gone)
        self.synced = {h: k for h, (_r, k) in current.items()}
        self._remote = {h: r for h, (r, _k) in current.items()}

_CLAIM_API = "extensions.agents.x-k8s.io/v1beta1"
_STATE_DIR = "/tmp/.agent-sbx"
_ENV_FILE = f"{_STATE_DIR}/env.sh"
_MARK_FILE = f"{_STATE_DIR}/mark"
# Host-identity names that must come from the sandbox itself (or the shell breaks / lies);
# never forwarded, even when the allowlist names them.
_ENV_SKIP = frozenset({
    "PATH", "HOME", "HOSTNAME", "PWD", "OLDPWD", "SHLVL", "_", "TERM", "SHELL", "USER", "LOGNAME",
    "TMPDIR", "SSH_AUTH_SOCK", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "DISPLAY", "PS1",
    "KUBECONFIG", "AGENT_SANDBOX_KUBECONFIG", "AGENT_SANDBOX_KUBECTL",
    # Sized for the sandbox's own memory limit by its template, not the host's.
    "JAVA_TOOL_OPTIONS",
})
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GLOB_CHARS = frozenset("*?[")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Glue that may accompany a host-only command on the host, as an allowlist of exact
# option letters per command. Nothing here can name a file: filters read only from a pipe,
# value-taking options take values that are not paths, and no long options are accepted.
#   flags: boolean short options (combinable, e.g. -inv)
#   vals:  short options that take a value (separate or attached), never a path
#   pos:   max positional words (patterns, jq filter, tr sets; never files)
#   pipe:  must read from a pipe (cannot be the first command of a pipeline)
_GLUE = {
    "echo": dict(flags="neE", vals="", pos=99, pipe=False),
    "printf": dict(flags="", vals="", pos=99, pipe=False),
    "true": dict(flags="", vals="", pos=0, pipe=False),
    "false": dict(flags="", vals="", pos=0, pipe=False),
    ":": dict(flags="", vals="", pos=99, pipe=False),
    "pwd": dict(flags="LP", vals="", pos=0, pipe=False),
    "uname": dict(flags="amnrsv", vals="", pos=0, pipe=False),
    "head": dict(flags="q", vals="nc", pos=0, pipe=True),
    "tail": dict(flags="q", vals="nc", pos=0, pipe=True),
    "grep": dict(flags="EFGicnovqwxsh", vals="emAB", pos=1, pipe=True),
    "egrep": dict(flags="icnovqwxsh", vals="emAB", pos=1, pipe=True),
    "fgrep": dict(flags="icnovqwxsh", vals="emAB", pos=1, pipe=True),
    "wc": dict(flags="lcwm", vals="", pos=0, pipe=True),
    "sort": dict(flags="nrufhVbs", vals="t", pos=0, pipe=True),
    "uniq": dict(flags="cdui", vals="", pos=0, pipe=True),
    "cut": dict(flags="s", vals="dfcb", pos=0, pipe=True),
    "tr": dict(flags="dsc", vals="", pos=2, pipe=True),
    "jq": dict(flags="rcejMSaC", vals="", pos=1, pipe=True),
}
_LIGHT_CMDS = frozenset(_GLUE) | {"cd"}


def _glue_ok(words: list, piped: bool) -> bool:
    """True when ``words`` is an allowlisted glue command with allowlisted options only."""
    name, args = words[0], words[1:]
    spec = _GLUE.get(name)
    if spec is None or (spec["pipe"] and not piped):
        return False
    if name in ("echo", "printf", ":"):
        return True  # only print their words ($ and ~ were rejected earlier)
    pos, i = 0, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            return False  # long options (--output=, --file=, --rawfile, ...) never allowed
        if a.startswith("-") and len(a) > 1:
            if name in ("head", "tail") and a[1:].isdigit():
                i += 1
                continue  # head -5
            for k, ch in enumerate(a[1:]):
                if ch in spec["flags"]:
                    continue
                if ch in spec["vals"]:
                    if not a[k + 2:]:
                        i += 1  # value is the next word (a pattern, count or delimiter)
                        if i >= len(args):
                            return False
                    break
                return False
        else:
            pos += 1
        i += 1
    return pos <= spec["pos"]


_SEPARATORS = frozenset({";", "&&", "||", "|"})
# Anything that substitutes, spawns or redirects keeps the whole command in the sandbox.
# "#" too: shlex and bash disagree on where a comment starts (bash: only at a word start).
_UNSAFE_SNIPPETS = ("$(", "`", "<(", ">(", "<<", "#", "\r")
_REMOTE_URL_RE = re.compile(r"^(?!file:)[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_LIKE_RE = re.compile(r"^[^/:\s]+:(?!/)")  # git@host:group/repo.git, origin-style host:path
_USERINFO_RE = re.compile(r"://[^/\s@]+@")
# Git config that reaches the sandbox is an exact allowlist of keys whose values are names,
# booleans or refspecs, never commands or credentials (no aliases, sshCommand, askpass, helpers,
# http.*, url.*, include). Values carrying URL userinfo (user:token@) are dropped as well.
_GIT_SAFE_KEYS = frozenset({
    "user.name", "user.email", "init.defaultbranch", "pull.rebase", "pull.ff", "push.default",
    "push.autosetupremote", "core.autocrlf", "core.eol", "core.filemode", "core.ignorecase",
    "core.quotepath", "core.precomposeunicode", "core.symlinks", "core.safecrlf",
})
# Additionally kept in a repository's own .git/config (needed for git to work in the sandbox).
_GIT_REPO_KEYS = frozenset({"core.repositoryformatversion", "core.bare", "core.logallrefupdates",
                            "core.worktree", "extensions.objectformat", "extensions.worktreeconfig",
                            "extensions.refstorage"})
_GIT_REPO_SUBKEYS = {"branch": {"remote", "merge", "rebase"}, "remote": {"url", "pushurl", "fetch"},
                     "submodule": {"url", "active", "path", "branch"}}


def _gitconfig_key_forwarded(key: str, repo: bool = False) -> bool:
    """``key`` as printed by ``git config --list`` (section and name lower-cased)."""
    k = key.lower()
    if k in _GIT_SAFE_KEYS or (repo and k in _GIT_REPO_KEYS):
        return True
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return repo and bool(sub) and name.lower() in _GIT_REPO_SUBKEYS.get(section.lower(), ())


def _sanitized_gitconfig(src: str, repo: bool) -> list[tuple[str, str]]:
    """(key, value) pairs of ``src`` that may enter the sandbox."""
    r = subprocess.run(["git", "config", "--file", src, "--list", "-z"], capture_output=True, timeout=10)
    out = []
    for item in r.stdout.decode(errors="surrogateescape").split("\0"):
        key, _, val = item.partition("\n")
        if key and _gitconfig_key_forwarded(key, repo) and not _USERINFO_RE.search(val):
            out.append((key, val))
    return out


def _is_repo_gitconfig(path: str) -> bool:
    """A git repository's own config file anywhere under a ``.git`` dir: ``config`` and
    ``config.worktree`` of the repository, its submodules (``modules/``) and worktrees."""
    parts = Path(path).parts
    return len(parts) >= 2 and parts[-1] in ("config", "config.worktree") and ".git" in parts[:-1]


_TASK_PATH_RE = re.compile(r"/workspaces/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/workdir")
# Default host commands: git network operations only. A CLI that can run user-defined code
# (aliases, extensions, plugins) must not be listed wholesale; list exact sub-commands instead,
# e.g. "gh pr create", "gh pr view".
_DEFAULT_HOST_CMDS = ("git push", "git pull", "git fetch", "git clone", "git ls-remote",
                      "git remote update", "git remote add", "git remote set-url", "git submodule update")
# Per git sub-command: options that make git run another program or read config/templates from
# a path the sandbox could have written. Refused on the host.
_GIT_UNSAFE_ARGS = {
    "clone": ("-u", "--upload-pack", "--template", "-c", "--config", "--separate-git-dir"),
    "fetch": ("--upload-pack",), "pull": ("--upload-pack",), "ls-remote": ("--upload-pack",),
    "push": ("--receive-pack", "--exec"), "remote": ("--upload-pack",), "submodule": (),
}
# Env assignments allowed in front of a host command; anything else (GIT_SSH_COMMAND, GIT_EXEC_PATH,
# LD_PRELOAD, ...) could make the host run sandbox-chosen code.
# Values are switches only: GIT_TRACE=/path would make git write a file on the host.
_SAFE_ASSIGN_RE = re.compile(r"^((GIT_TRACE[A-Z_]*|GIT_CURL_VERBOSE|GIT_TERMINAL_PROMPT|NO_COLOR)=(0|1|2|true|false)?"
                             r"|(LANG|LC_[A-Z]+)=[A-Za-z0-9_.@-]*)$")
# Pull-back into a .git dir is an allowlist: history and index only. Everything else (config,
# hooks, commondir, gitdir, modules, worktrees, info, ...) stays as the host has it, and a
# ``.git`` *file* (gitfile redirect) is never written, so the host's git never runs
# sandbox-written hooks or config.
_GIT_PULL_TOP = frozenset({"objects", "refs", "logs"})
_GIT_PULL_FILES = frozenset({"HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE",
                             "CHERRY_PICK_HEAD", "REVERT_HEAD", "AUTO_MERGE", "index", "packed-refs",
                             "COMMIT_EDITMSG", "description", "shallow"})
# Directory names never mirrored as code (rebuilt from cache inside the sandbox).
_CODE_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "target", "build", "dist", ".gradle",
                             "__pycache__", ".pytest_cache", ".mypy_cache", ".next", ".turbo"})


def _cfg(cc: dict, key: str, env: str, default):
    v = cc.get(key)
    if v is None or v == "":
        v = os.environ.get(env)
    return default if v is None or v == "" else v


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[,\n]", v) if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def _task_dirs(path: str, task_files: tuple = ()) -> list[str]:
    """Code dirs implied by a cwd. Task-runner layout /workspaces/<ws>/<task>/workdir: the
    workdir, the sibling output dir and any ``task_files`` found in the task root (pushed only,
    e.g. requester metadata a git wrapper reads). Otherwise the path itself when task-deep."""
    parts = Path(os.path.abspath(path or "/")).parts  # ('/', 'workspaces', ws, task, ...)
    if len(parts) < 4 or parts[1] != "workspaces":
        return []
    task = os.path.join(*parts[:4])
    if len(parts) >= 5 and parts[4] == "workdir":
        out = [os.path.join(task, "workdir")]
        for name in task_files:
            meta = os.path.join(task, name)
            if os.path.isfile(meta):
                out.append(meta)
        if os.path.isdir(os.path.join(task, "output")):
            out.append(os.path.join(task, "output"))
        return [d for d in out if os.path.exists(d)]
    return [os.path.abspath(path)] if os.path.isdir(path) else []


def _git_protected(path: str, is_dir: bool = False) -> bool:
    """True unless ``path`` is outside any .git dir or on the .git pull-back allowlist.
    A path named .git that is not a directory (a gitfile) is always protected."""
    parts = Path(path).parts
    if ".git" not in parts:
        return False
    i = parts.index(".git")
    rest = parts[i + 1:]
    if not rest:
        return not is_dir
    if rest[0] in _GIT_PULL_TOP and ".git" not in rest:
        return False
    return not (len(rest) == 1 and rest[0] in _GIT_PULL_FILES)


def _inside(path: str, roots: list) -> bool:
    """Real-path containment: refuses host symlinks that lead out of every root."""
    real = os.path.realpath(path)
    for r in roots:
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr.rstrip("/") + "/"):
            return True
    return False


def _safe_makedirs(path: str, roots: list) -> bool:
    """mkdir -p that never creates or enters anything outside ``roots``: walks down from the
    root, refusing any existing component that is a symlink or not a directory."""
    path = os.path.abspath(path)
    root = next((r for r in roots if path == r or path.startswith(r.rstrip("/") + "/")), None)
    if root is None or not _inside(root, [root]):
        return False
    cur = root
    for part in Path(os.path.relpath(path, root)).parts if path != root else ():
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
        if not os.path.exists(cur):
            try:
                os.mkdir(cur)
            except FileExistsError:
                pass
        if os.path.islink(cur) or not os.path.isdir(cur):
            return False
    return _inside(path, roots)


def _host_git_env() -> dict:
    """Environment for host-side commands: git ignores bare repositories it merely discovers
    (a directory the sandbox wrote with HEAD/config/hooks) and never uses the ext:: transport."""
    env = dict(os.environ)
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for k, v in (("safe.bareRepository", "explicit"), ("protocol.ext.allow", "never")):
        env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = k, v
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


def _mtime_key(p: str):
    try:
        st = os.stat(p)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


class Sandbox:
    """One SandboxClaim per session; persistent mode reuses the claim by session label.

    ``config`` keys are the ``agent_sandbox_*`` names (each falls back to its AGENT_SANDBOX_*
    environment variable). ``app`` labels the claims; ``seed_dirs`` are (host_dir, path under the
    sandbox HOME) pairs copied in once at start; ``extra_env_skip`` adds names never forwarded.
    Host variables reach the sandbox only when ``agent_sandbox_forward_env`` names them (empty by
    default): the sandbox runs and builds code, credentials stay on the host."""

    def __init__(self, *, cwd: str = "", timeout: int = 600, session: str = "default",
                 config: dict | None = None, app: str = "agent-sandbox",
                 seed_dirs: tuple = (), extra_env_skip: tuple = ()):
        cc = config or {}
        self._kubectl = str(_cfg(cc, "agent_sandbox_kubectl", "AGENT_SANDBOX_KUBECTL", "kubectl"))
        self._kubeconfig = str(_cfg(cc, "agent_sandbox_kubeconfig", "AGENT_SANDBOX_KUBECONFIG",
                                    str(Path.home() / ".kube" / "agent-sandbox.yaml")))
        self._ns = str(_cfg(cc, "agent_sandbox_namespace", "AGENT_SANDBOX_NAMESPACE", "sbx"))
        self._pool = str(_cfg(cc, "agent_sandbox_pool", "AGENT_SANDBOX_POOL", "agent-4g"))
        self._container = str(_cfg(cc, "agent_sandbox_container", "AGENT_SANDBOX_CONTAINER", "main"))
        # Default: one sandbox per session, deleted at close.
        self._persistent = str(_cfg(cc, "agent_sandbox_persistent", "AGENT_SANDBOX_PERSISTENT",
                                    False)).lower() in ("true", "1", "yes")
        # Cluster-side backstop: the claim expires unless renewed by activity (host crash, OOM, kill -9).
        self._idle_ttl = int(_cfg(cc, "agent_sandbox_idle_ttl", "AGENT_SANDBOX_IDLE_TTL", 1800))
        self._ttl_renewed = 0.0
        self._claim_timeout = int(_cfg(cc, "agent_sandbox_claim_timeout", "AGENT_SANDBOX_CLAIM_TIMEOUT", 180))
        self._sync_paths = [os.path.expanduser(p) for p in _as_list(
            _cfg(cc, "agent_sandbox_sync_paths", "AGENT_SANDBOX_SYNC_PATHS", ""))]
        host_cmds = _cfg(cc, "agent_sandbox_host_commands", "AGENT_SANDBOX_HOST_COMMANDS", None)
        self._host_cmds = tuple(_as_list(host_cmds)) if host_cmds is not None else _DEFAULT_HOST_CMDS
        # Allowlist of host variables to forward (names or globs such as NPM_CONFIG_*). Empty by default.
        fwd = _as_list(_cfg(cc, "agent_sandbox_forward_env", "AGENT_SANDBOX_FORWARD_ENV", ""))
        self._env_forward = frozenset(x for x in fwd if not _GLOB_CHARS & set(x))
        self._env_forward_globs = tuple(x for x in fwd if _GLOB_CHARS & set(x))
        skip = _as_list(_cfg(cc, "agent_sandbox_env_skip", "AGENT_SANDBOX_ENV_SKIP", ""))
        self._env_skip = (_ENV_SKIP | frozenset(extra_env_skip)
                          | frozenset(x for x in skip if not _GLOB_CHARS & set(x)))
        self._env_skip_globs = tuple(x for x in skip if _GLOB_CHARS & set(x))  # e.g. *_TOKEN
        self._task_files = tuple(_as_list(_cfg(cc, "agent_sandbox_task_files", "AGENT_SANDBOX_TASK_FILES", "")))
        # Code and configs are mirrored at identical absolute paths, so keep the host's cwd.
        start_cwd = cwd if cwd and cwd.startswith("/") else "/workspace"
        code_dirs = _as_list(_cfg(cc, "agent_sandbox_code_dirs", "AGENT_SANDBOX_CODE_DIRS", ""))
        if not code_dirs:
            code_dirs = _task_dirs(start_cwd, self._task_files)
        self._auto_code_dirs = not _as_list(_cfg(cc, "agent_sandbox_code_dirs", "AGENT_SANDBOX_CODE_DIRS", ""))
        self._code_dirs = [os.path.abspath(d).rstrip("/") for d in code_dirs]
        self.cwd, self.timeout = start_cwd, timeout
        self._app = app
        self._seed_dirs = list(seed_dirs)
        self._task_label = "".join(ch if ch.isalnum() else "-" for ch in str(session).lower())[:50].strip("-") or "default"
        self._lock = threading.Lock()
        self._claim = None
        self._pod = None
        self._env_digest = None
        self._acquire()
        self._remote_home = (self._exec_simple("printf %s \"$HOME\"") or "/root").strip() or "/root"
        self._exec_simple(f"mkdir -p {_STATE_DIR} && chmod 700 {_STATE_DIR} && : > {_ENV_FILE}; "
                          f"mkdir -p {shlex.quote(self.cwd)} 2>/dev/null || true")
        self._reset_remote_dirs(self._code_dirs)
        self._seed()
        self._push_gitconfig()
        self._file_sync = self._make_file_sync()
        if self._file_sync is not None:
            self._file_sync.sync(force=True)
        self._push_env(force=True)
        if self._code_dirs:  # reused claim: mark "now" so only later sandbox edits flow back
            self._exec_simple(f"touch {_MARK_FILE}")

    # ---- kubectl plumbing -------------------------------------------------
    def _k(self, *args: str) -> list[str]:
        return [self._kubectl, "--kubeconfig", self._kubeconfig, "-n", self._ns, *args]

    def _run_k(self, *args: str, inp: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(self._k(*args), input=inp, capture_output=True, text=True, timeout=timeout)

    def _exec_argv(self, *argv: str, stdin: bool = False) -> list[str]:
        return self._k("exec", *(["-i"] if stdin else []), self._pod, "-c", self._container, "--", *argv)

    def _exec_simple(self, script: str, timeout: int = 60, inp: bytes | None = None) -> str:
        r = subprocess.run(self._exec_argv("bash", "-c", script, stdin=inp is not None),
                           input=inp, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"agent-sandbox exec failed rc={r.returncode}: "
                               f"{r.stderr.decode(errors='replace').strip()[:300]}")
        return r.stdout.decode(errors="replace")

    # ---- claim lifecycle --------------------------------------------------
    def _find_existing_claim(self) -> str | None:
        r = self._run_k("get", "sandboxclaims", "-l", f"agent-sandbox/session={self._task_label}", "-o", "json")
        if r.returncode != 0:
            return None
        items = json.loads(r.stdout or "{}").get("items", [])
        return items[0]["metadata"]["name"] if items else None

    def _lifecycle(self) -> dict:
        lc = {"shutdownPolicy": "Delete"}
        if self._idle_ttl > 0:
            t = time.gmtime(time.time() + self._idle_ttl)
            lc["shutdownTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", t)
        return lc

    def _renew_ttl(self) -> None:
        """Push shutdownTime forward while the session is active (at most once a minute)."""
        if self._idle_ttl <= 0 or self._claim is None or time.monotonic() - self._ttl_renewed < 55:
            return
        self._ttl_renewed = time.monotonic()
        patch = json.dumps({"spec": {"lifecycle": self._lifecycle()}})
        try:
            r = self._run_k("patch", "sandboxclaim", self._claim, "--type", "merge", "-p", patch, timeout=20)
            if r.returncode != 0:
                logger.warning("agent-sandbox: ttl renew failed: %s", r.stderr.strip()[:200])
        except Exception as e:
            logger.warning("agent-sandbox: ttl renew failed: %s", e)

    def _acquire(self) -> None:
        name = self._find_existing_claim() if self._persistent else None
        created = name is None
        if name is None:
            name = f"{self._app[:20]}-{self._task_label[:30]}-{uuid.uuid4().hex[:6]}"
            manifest = {
                "apiVersion": _CLAIM_API, "kind": "SandboxClaim",
                "metadata": {"name": name, "labels": {"agent-sandbox/session": self._task_label, "agent-sandbox/app": self._app[:63]}},
                "spec": {"warmPoolRef": {"name": self._pool}, "lifecycle": self._lifecycle()},
            }
            r = self._run_k("apply", "-f", "-", inp=json.dumps(manifest))
            if r.returncode != 0:
                raise RuntimeError(f"agent-sandbox: claim create failed: {r.stderr.strip()[:300]}")
        t0 = time.monotonic()
        while time.monotonic() - t0 < self._claim_timeout:
            r = self._run_k("get", "sandboxclaim", name, "-o", "json")
            st = json.loads(r.stdout or "{}").get("status", {}) if r.returncode == 0 else {}
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", [])):
                self._claim, self._pod = name, st["sandbox"]["name"]
                self._ttl_renewed = time.monotonic()
                logger.info("agent-sandbox: claim %s -> pod %s in %.2fs", name, self._pod, time.monotonic() - t0)
                return
            time.sleep(0.25)
        if created:  # don't leave a pending claim queued behind a failed session start
            self._run_k("delete", "sandboxclaim", name, "--wait=false")
        raise RuntimeError(f"agent-sandbox: claim {name} not Ready within {self._claim_timeout}s")

    # ---- tar transport ------------------------------------------------------
    def _tar_push(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for host, remote in pairs:
                try:
                    if _is_repo_gitconfig(host) and os.path.isfile(host):
                        try:
                            self._add_repo_gitconfig(tar, host, remote)
                        except (OSError, subprocess.SubprocessError) as e:
                            # fail closed: an empty file replaces whatever the sandbox held
                            logger.warning("agent-sandbox: %s sent empty, sanitizing failed: %s", host, e)
                            info = tar.gettarinfo(host, arcname=remote.lstrip("/"))
                            info.size = 0
                            tar.addfile(info, io.BytesIO(b""))
                    else:
                        tar.add(host, arcname=remote.lstrip("/"), recursive=False)
                except (OSError, subprocess.SubprocessError):
                    continue
        r = subprocess.run(self._exec_argv("tar", "xpf", "-", "-C", "/", "--no-same-owner", stdin=True),
                           input=buf.getvalue(), capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"agent-sandbox: push failed: {r.stderr.decode(errors='replace')[:300]}")

    @staticmethod
    def _add_repo_gitconfig(tar: tarfile.TarFile, host: str, remote: str) -> None:
        """Push a repository's .git/config with only the allowlisted keys (see _GIT_SAFE_KEYS)."""
        with tempfile.TemporaryDirectory(prefix="agent-sbx-gitcfg-") as d:
            tmp = os.path.join(d, "config")
            open(tmp, "w").close()
            for key, val in _sanitized_gitconfig(host, repo=True):
                subprocess.run(["git", "config", "--file", tmp, "--add", key, val],
                               capture_output=True, timeout=10, check=True)
            info = tar.gettarinfo(host, arcname=remote.lstrip("/"))
            data = open(tmp, "rb").read()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    def _rm_remote(self, paths: list[str]) -> None:
        if paths:
            self._exec_simple("rm -f -- " + " ".join(shlex.quote(p) for p in paths))

    # ---- seed dirs (one-time copy, e.g. an agent's skills) -----------------
    def _seed(self) -> None:
        for src, rel in self._seed_dirs:
            try:
                src = str(src)
                if os.path.isdir(src):
                    dst = f"{self._remote_home}/{str(rel).strip('/')}"
                    self._tar_push([(h, dst + h[len(src):]) for h, _ in self._walk(src, code=True)])
            except Exception as e:  # a convenience; never block the session on it
                logger.warning("agent-sandbox: seed %s failed: %s", src, e)

    # ---- declared files + code: host -> sandbox, change-checked per command
    @staticmethod
    def _walk(root: str, code: bool) -> list[tuple[str, str]]:
        if os.path.isfile(root):
            return [(root, root)]
        out = []
        for d, dirs, files in os.walk(root):
            if code:
                dirs[:] = [x for x in dirs if x not in _CODE_SKIP_DIRS]
            for f in files:
                p = os.path.join(d, f)
                if not os.path.islink(p) and os.path.isfile(p):
                    out.append((p, p))
        return out

    def _code_files(self) -> list[tuple[str, str]]:
        return [x for d in self._code_dirs if os.path.isdir(d) for x in self._walk(d, code=True)]

    def _code_meta(self) -> list[tuple[str, str]]:
        return [(f, f) for f in self._code_dirs if os.path.isfile(f)]

    def _all_files(self) -> list[tuple[str, str]]:
        return ([x for p in self._sync_paths for x in self._walk(p, code=False)]
                + self._code_meta() + self._code_files())

    def _make_file_sync(self):
        if not self._sync_paths and not self._code_dirs:
            return None
        return _FileSync(self._all_files, self._tar_push, self._rm_remote)

    # ---- code: sandbox -> host after each command ------------------------
    def _pull_code(self) -> tuple[int, int]:
        """Pull files the sandbox changed since the last mark; mirror sandbox deletions."""
        if not self._code_dirs or self._pod is None:
            return (0, 0)
        dirs = " ".join(shlex.quote(d) for d in self._code_dirs if os.path.isdir(d))
        if not dirs:
            return (0, 0)
        prune = " -o ".join(f"-name {shlex.quote(n)}" for n in sorted(_CODE_SKIP_DIRS))
        find = f"find {dirs} -type d \\( {prune} \\) -prune -o -type f"
        script = (f"set -e; touch {_MARK_FILE}.new; cd /; "
                  f"{find} -print0 2>/dev/null > {_STATE_DIR}/all; "
                  f"{{ {find} -newer {_MARK_FILE} -print0; find {dirs} -type d \\( {prune} \\) -prune -o -type d -empty -print0; }} "
                  f"2>/dev/null > {_STATE_DIR}/changed; "
                  f"tar cf {_STATE_DIR}/changed.tar --no-recursion --null -T {_STATE_DIR}/changed 2>/dev/null || true; "
                  f"mv -f {_MARK_FILE}.new {_MARK_FILE}; "
                  f"tar cf - -C {_STATE_DIR} all changed.tar")
        r = subprocess.run(self._exec_argv("bash", "-c", script), capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"pull-back failed rc={r.returncode}: {r.stderr.decode(errors='replace')[:200]}")
        outer = tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:")
        remote_all = set(filter(None, outer.extractfile("all").read().decode(errors="surrogateescape").split("\0")))
        written = 0
        inner_f = outer.extractfile("changed.tar")
        inner_bytes = inner_f.read() if inner_f else b""
        if inner_bytes:
            with tarfile.open(fileobj=io.BytesIO(inner_bytes), mode="r:") as tar:
                for m in tar.getmembers():
                    target = "/" + m.name.lstrip("/")
                    if ".." in Path(target).parts or _git_protected(target, m.isdir()):
                        continue
                    roots = [d for d in self._code_dirs if os.path.isdir(d)]
                    if m.isdir() and any(target == d or target.startswith(d + "/") for d in roots):
                        _safe_makedirs(target, roots)  # empty dirs matter (e.g. .git/refs/tags)
                        continue
                    if not m.isfile() or not any(target.startswith(d + "/") for d in roots):
                        continue
                    if not _safe_makedirs(os.path.dirname(target), roots) or os.path.islink(target):
                        logger.warning("agent-sandbox: refused pull-back through a symlink: %s", target)
                        continue  # never write outside the declared code dirs (symlinks included)
                    data = tar.extractfile(m).read()
                    fd, tmp = tempfile.mkstemp(prefix=".agent-sbx-", dir=os.path.dirname(target))
                    try:
                        with os.fdopen(fd, "wb") as dst:
                            dst.write(data)
                        os.chmod(tmp, m.mode & 0o777)
                        os.replace(tmp, target)  # replaces the name; never follows a symlink at target
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp)
                        raise
                    written += 1
        deleted = 0
        gone = []
        synced = self._file_sync.synced if self._file_sync is not None else {}
        for host, _ in self._code_files():
            # Only delete what the sandbox once had and no longer has; a file created on the
            # host since the last push was never synced and is left alone.
            if host not in remote_all and host in synced and not _git_protected(host, os.path.isdir(host)):
                try:
                    os.unlink(host)
                    deleted += 1
                    gone.append(host)
                except OSError:
                    pass
                synced.pop(host, None)
        if (written or deleted) and self._file_sync is not None:
            for host, _ in self._code_files():  # pulled state = synced state (no bounce back)
                k = _mtime_key(host)
                if k is not None:
                    synced[host] = k
        if written or deleted:
            logger.info("agent-sandbox: pull-back wrote %d, deleted %d %s", written, deleted, gone[:8])
        return (written, deleted)

    # ---- environment: allowlist only, re-written when changed ----------------
    def _env_forwarded(self, k: str) -> bool:
        return ((k in self._env_forward or any(fnmatch.fnmatchcase(k, g) for g in self._env_forward_globs))
                and k not in self._env_skip and bool(_NAME_RE.match(k))
                and not any(fnmatch.fnmatchcase(k, g) for g in self._env_skip_globs))

    def _env_payload(self) -> str:
        names = sorted(k for k in os.environ if self._env_forwarded(k))
        # A name forwarded earlier in this session but not any more (removed from the host or
        # from the allowlist) is unset, so a value kept by an adapter's shell snapshot goes too.
        sent = getattr(self, "_env_sent", set())
        gone = sorted(sent - set(names))
        sent.update(names)
        self._env_sent = sent
        env = "".join(f"unset {k}\n" for k in gone)
        env += "".join(f"export {k}={shlex.quote(os.environ[k])}\n" for k in names)
        return env + self._shims()

    def _shims(self) -> str:
        """Shell functions that stop host-only tools from silently failing inside the sandbox."""
        msg = ("[agent-sandbox] '%s' is not available inside the code sandbox. Run it as a SEPARATE "
               "terminal call (cd, echo and piped grep/jq/head are fine); keep build/test/inspection commands "
               "in their own calls so they stay in the sandbox.")
        tops = sorted({h.split()[0] for h in self._host_cmds if h.split()[0] != "git"})
        out = [f"{t}() {{ printf '%s\\n' {shlex.quote(msg % t)} >&2; return 97; }}" for t in tops]
        gsub = sorted({h.split()[1] for h in self._host_cmds
                       if h.startswith("git ") and len(h.split()) == 2})  # "git remote update" etc. stay local-checked
        if gsub:
            case = "|".join(gsub)
            out.append("git() { case \"$1\" in " + case + ") printf '%s\\n' "
                       + shlex.quote(msg % "git <network>") + " >&2; return 97;; esac; command git \"$@\"; }")
        return "\n".join(out) + "\n"

    def _push_env(self, force: bool = False) -> None:
        payload = self._env_payload()
        digest = hash(payload)
        if not force and digest == self._env_digest:
            return
        self._exec_simple(f"umask 077; cat > {_ENV_FILE}.tmp && mv -f {_ENV_FILE}.tmp {_ENV_FILE}",
                          inp=payload.encode())
        self._env_digest = digest

    # ---- per-command hooks (adapters with their own shell flow call these) ------
    def before_command(self) -> None:
        """Push changed files and env; call right before running a command in the sandbox."""
        if self._file_sync is not None:
            self._file_sync.sync(force=True)
        self._push_env()

    @staticmethod
    def env_prefix() -> str:
        """Shell line that loads the host env; put it in front of every sandbox command (after
        any session snapshot an adapter sources, so current values win)."""
        return f"[ -r {_ENV_FILE} ] && . {_ENV_FILE} >/dev/null 2>&1\n"

    def exec_argv(self, *argv: str, stdin: bool = False) -> list[str]:
        """kubectl argv that runs ``argv`` in the sandbox container."""
        return self._exec_argv(*argv, stdin=stdin)

    def _default_run(self, command: str, cwd: str, timeout: int, stream=None) -> dict:
        self.before_command()
        script = f"{self.env_prefix()}cd {shlex.quote(cwd)} 2>/dev/null || cd /\n{command}"
        if stream is None:
            r = subprocess.run(self._exec_argv("bash", "-c", script), capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout + r.stderr).decode(errors="replace")
            return {"output": out, "returncode": r.returncode}
        p = subprocess.Popen(self._exec_argv("bash", "-c", script), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        failure: list[BaseException] = []

        def _copy():  # every byte, in order, into any blocking binary file-like object
            try:
                for chunk in iter(lambda: p.stdout.read1(65536), b""):
                    view = memoryview(chunk)
                    while view:
                        n = stream.write(view)
                        if not isinstance(n, int) or n <= 0:
                            raise OSError("agent-sandbox: stream accepted no bytes "
                                          "(stream= needs a blocking binary writer)")
                        view = view[n:]
                    if hasattr(stream, "flush"):
                        stream.flush()
            except BaseException as e:  # reported to the caller below
                failure.append(e)
                with contextlib.suppress(OSError, AttributeError):
                    os.killpg(p.pid, 9)  # stop the command rather than run it with nobody listening
        t = threading.Thread(target=_copy, daemon=True)
        t.start()
        try:
            rc = p.wait(timeout=timeout)  # the deadline holds even while output keeps coming
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, 9)  # kubectl and anything it spawned locally, so the pipe closes
            except (OSError, AttributeError):
                p.kill()
            p.wait()
            t.join(5)
            raise
        t.join()  # the command is done only when its output is: a slow writer delays, never drops
        p.stdout.close()
        if failure:
            raise failure[0]
        return {"output": "", "returncode": rc}

    def _is_host_command(self, command: str, cwd: str = "") -> bool:
        """Run on the host only when the command is host-only tools plus read-only glue,
        confined to the synced code dirs.

        Conservative by design: any substitution, variable expansion (except ``$?``), subshell,
        background job, heredoc, file redirection (other than to /dev/null or fd duplication),
        multi-line script, working directory or local path outside the code dirs keeps the
        command in the sandbox, where the host-only tools are stubs that print a hint."""
        text = command.strip()
        if not text or "\n" in text or any(x in text for x in _UNSAFE_SNIPPETS):
            return False
        if "$" in text.replace("$?", "") or "~" in text:
            return False  # host env vars and home paths never reach a host command
        roots = [d for d in self._code_dirs if d]
        here = os.path.abspath(cwd or self.cwd or "/")
        if not roots or not os.path.isdir(here) or not _inside(here, roots) or os.path.realpath(here) != here:
            return False  # must be an existing, non-symlinked dir inside the code dirs
        try:
            lex = shlex.shlex(text, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            lex.commenters = ""
            toks = list(lex)
        except ValueError:
            return False
        try:  # same split without quote removal: tells which words bash sees quoted
            rlex = shlex.shlex(text, posix=False, punctuation_chars=True)
            rlex.whitespace_split = True
            rlex.commenters = ""
            raw = list(rlex)
        except ValueError:
            return False
        if len(raw) != len(toks):
            return False
        for t, r in zip(toks, raw):  # unquoted glob/brace characters would be expanded by bash
            unquoted = not (len(r) >= 2 and r[0] == r[-1] and r[0] in "'\"")
            if unquoted and any(c in t.replace("$?", "") for c in "*?[]{}"):
                return False
        segs, cur, i, piped, pipes, seps = [], [], 0, False, [], []
        while i < len(toks):
            t = toks[i]
            if t in _SEPARATORS:
                segs.append(cur)
                pipes.append(piped)
                seps.append(t)
                piped = t == "|"
                cur = []
            elif t in (">", ">>", "&>"):
                if i + 1 >= len(toks) or toks[i + 1] != "/dev/null":
                    return False
                if cur and cur[-1].isdigit():
                    cur.pop()
                i += 1
            elif t == ">&":
                if not (cur and cur[-1].isdigit() and i + 1 < len(toks) and toks[i + 1].isdigit()):
                    return False
                cur.pop()
                i += 1
            elif t and set(t) <= set("();<>|&"):
                return False  # (, ), &, <, |&, ...
            else:
                cur.append(t)
            i += 1
        segs.append(cur)
        pipes.append(piped)
        seps.append(None)
        has_host = False

        def _confined(arg: str) -> bool:
            """Local paths (absolute, relative or file://) must stay inside the code dirs;
            remote URLs, scp-style host:path and plain words (refs, remotes) are fine."""
            if arg.startswith("-"):
                if "=" not in arg:
                    return True
                arg = arg.split("=", 1)[1]
            if arg.startswith("file:"):
                arg = arg[5:].lstrip("/") and "/" + arg[5:].lstrip("/")
            elif _REMOTE_URL_RE.match(arg) or _SCP_LIKE_RE.match(arg):
                return True
            if not arg:
                return True
            # realpath: a symlink in the tree (e.g. checked out by a host-side git pull) must not
            # lead a host command out of the code dirs
            full = os.path.realpath(os.path.join(here, arg))
            return _inside(full, roots)

        for seg_no, (words, piped) in enumerate(zip(segs, pipes)):
            assigns = 0
            while assigns < len(words) and _ASSIGN_RE.match(words[assigns]):
                assigns += 1
            if any(not _SAFE_ASSIGN_RE.match(w) for w in words[:assigns]):
                return False
            words = words[assigns:]
            if not words:
                continue
            if words[0] == "cd":
                # Only a leading "cd <dir> && ...": one plain argument, an existing directory with no
                # symlink on the way (bash's logical path == the real path), and && so that a
                # failed cd stops the command instead of running it from the old directory.
                if (seg_no != 0 or len(words) != 2 or words[1].startswith("-") or seps[0] != "&&"):
                    return False
                logical = os.path.normpath(os.path.join(here, words[1]))
                if not os.path.isdir(logical) or os.path.realpath(logical) != logical \
                        or not _inside(logical, roots):
                    return False
                here = logical
                continue
            if words[0] in _GLUE:
                if not _glue_ok(words, piped):
                    return False
                continue
            match = next((h.split() for h in self._host_cmds if words[:len(h.split())] == h.split()), None)
            # git takes local paths (clone target, file remotes): keep them inside the code dirs.
            # Other listed CLIs get their arguments verbatim; listing one is trusting it.
            if match is not None and match[0] == "git" and not all(_confined(a) for a in words[len(match):]):
                return False
            if match is not None:
                if words[0] == "git" and len(words) > 1:
                    bad = _GIT_UNSAFE_ARGS.get(words[1], ())
                    if any(a == u or a.startswith(u + "=") or (len(u) == 2 and a.startswith(u))
                           for a in words[2:] for u in bad):
                        return False  # e.g. --upload-pack=<cmd>, clone -c core.sshCommand=<cmd>
                if any(a.startswith(("ext::", "fd::")) for a in words[len(match):]):
                    return False  # git remote helpers that spawn commands
                has_host = True
            else:
                return False
        return has_host

    def _push_gitconfig(self) -> None:
        """Replace the sandbox ~/.gitconfig with the allowlisted part of the host's (identity and
        everyday settings, see _GIT_SAFE_KEYS). Always replaced, so a reused claim keeps nothing
        from an earlier session."""
        src = os.path.expanduser("~/.gitconfig")
        try:
            pairs = _sanitized_gitconfig(src, repo=False) if os.path.isfile(src) else []
        except Exception as e:  # fail closed: the sandbox copy is still cleared below
            logger.warning("agent-sandbox: host gitconfig not read, sandbox gets none: %s", e)
            pairs = []
        lines = [f"git config --global --add {shlex.quote(k)} {shlex.quote(v)}" for k, v in pairs]
        self._exec_simple("; ".join(["rm -f ~/.gitconfig"] + lines))

    def _reset_remote_dirs(self, dirs: list[str]) -> None:
        """host is the source of truth when a code dir is adopted: drop whatever a reused
        sandbox still holds there (stale files older than the mark would never flow back)."""
        dirs = [d for d in dirs if os.path.isdir(d)]
        if dirs:
            q = " ".join(shlex.quote(d) for d in dirs)
            self._exec_simple(f"rm -rf {q} && mkdir -p {q}")

    def _ensure_code_dirs(self, cwd: str) -> None:
        """Task runners (ACP) may create the env at /workspaces and pass the task dir per command:
        adopt that task's workdir as a code dir the first time it is seen."""
        if not self._auto_code_dirs or not cwd:
            return
        new = [d for d in _task_dirs(cwd, self._task_files) if d not in self._code_dirs]
        if not new:
            return
        self._code_dirs.extend(new)
        self._reset_remote_dirs(new)
        self._exec_simple(f"touch {_MARK_FILE}")
        if self._file_sync is None:
            self._file_sync = self._make_file_sync()
        logger.info("agent-sandbox: code dirs now %s", self._code_dirs)

    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None,
                run_in_sandbox=None, stream=None) -> dict:
        """Run ``command``: on the host when it is a confined host command, else in the sandbox
        (via ``run_in_sandbox(command, cwd, timeout)`` when an adapter has its own shell flow).
        Returns {"output", "returncode"}; with ``stream`` (a file object) output goes there."""
        self._renew_ttl()
        self._ensure_code_dirs(cwd or self.cwd)
        for m in _TASK_PATH_RE.finditer(command):  # absolute task paths used by file tools
            self._ensure_code_dirs(m.group(0))
        done = threading.Event()

        def _keepalive():  # long commands must not outlive the claim's cluster-side expiry
            while not done.wait(60):
                self._renew_ttl()
        threading.Thread(target=_keepalive, daemon=True).start()
        try:
            if self._is_host_command(command, cwd or self.cwd):
                self._pull_code()
                run_cwd = cwd or self.cwd
                p = subprocess.run(["bash", "-c", command], cwd=run_cwd,
                                   capture_output=True, text=True, timeout=timeout or self.timeout,
                                   stdin=subprocess.DEVNULL, env=_host_git_env())
                if self._file_sync is not None:  # e.g. git pull changed the tree on the host
                    self._file_sync.sync(force=True)
                if stream is not None:
                    stream.write((p.stdout + p.stderr).encode())  # binary stream, like the sandbox path
                    return {"output": "", "returncode": p.returncode}
                return {"output": p.stdout + p.stderr, "returncode": p.returncode}
            if run_in_sandbox is not None:
                result = run_in_sandbox(command, cwd or self.cwd, timeout or self.timeout)
            else:
                result = self._default_run(command, cwd or self.cwd, timeout or self.timeout, stream)
        finally:
            done.set()
        try:
            self._pull_code()
        except Exception as e:
            logger.warning("agent-sandbox: code pull-back failed: %s", e)
        return result

    @classmethod
    def release(cls, session: str, *, config: dict | None = None) -> int:
        """Delete every claim kept for ``session`` (persistent mode) without claiming a new one.
        Returns how many claims were deleted."""
        sb = cls.__new__(cls)
        cc = config or {}
        sb._kubectl = str(_cfg(cc, "agent_sandbox_kubectl", "AGENT_SANDBOX_KUBECTL", "kubectl"))
        sb._kubeconfig = str(_cfg(cc, "agent_sandbox_kubeconfig", "AGENT_SANDBOX_KUBECONFIG",
                                  str(Path.home() / ".kube" / "agent-sandbox.yaml")))
        sb._ns = str(_cfg(cc, "agent_sandbox_namespace", "AGENT_SANDBOX_NAMESPACE", "sbx"))
        label = "".join(ch if ch.isalnum() else "-" for ch in str(session).lower())[:50].strip("-") or "default"
        r = sb._run_k("get", "sandboxclaims", "-l", f"agent-sandbox/session={label}", "-o", "json")
        if r.returncode != 0:
            raise RuntimeError(f"agent-sandbox: list claims failed: {r.stderr.strip()[:300]}")
        names = [i["metadata"]["name"] for i in json.loads(r.stdout or "{}").get("items", [])]
        failed = []
        for n in names:
            d = sb._run_k("delete", "sandboxclaim", n, "--wait=false", "--ignore-not-found")
            if d.returncode != 0:
                failed.append(f"{n}: {d.stderr.strip()[:200]}")
        if failed:
            raise RuntimeError("agent-sandbox: release failed for " + "; ".join(failed))
        return len(names)

    def close(self, *, force_remove: bool = False):
        """Final pull-back, then delete the claim (kept when persistent unless ``force_remove``)."""
        with self._lock:
            if self._claim is None:
                return
            try:
                self._pull_code()
            except Exception as e:
                logger.warning("agent-sandbox: final pull-back failed: %s", e)
            if force_remove or not self._persistent:
                r = self._run_k("delete", "sandboxclaim", self._claim, "--wait=false")
                logger.info("agent-sandbox: deleted claim %s rc=%s", self._claim, r.returncode)
            self._claim = None
