# Git checkpoints and forks (opt-in)

Turn it on with `agent_sandbox_checkpoint: true` (or `AGENT_SANDBOX_CHECKPOINT=true`). It is off
by default; then `execute()` returns exactly `{output, returncode}` and no ref is written.

```python
from self_hosted_agent_sandbox import Sandbox

sb = Sandbox(cwd="/workspaces/ws/task/workdir", session="attempt-1",
             config={"agent_sandbox_checkpoint": True})
r1 = sb.execute("pytest -x || true")        # {"output", "returncode", "checkpoint": "<sha>"}
r2 = sb.execute("apply-some-refactor")
sb.checkpoints()                              # [{"seq": 1, "sha": ..., "ref": ..., "parent": ...}, ...]

# Go back to the tree after step 1 and try something else, in a new session:
alt = sb.fork(r1["checkpoint"], "/workspaces/ws/task-alt/workdir", session="attempt-1-alt")
alt.execute("try-another-approach")
```

CLI:

```bash
sbx checkpoints --session attempt-1 --cwd /path/in/repo            # seq sha ref, oldest first
sbx checkpoints --session attempt-1 --cwd /path/in/repo --json
sbx fork <sha> /new/dir --cwd /path/in/repo                         # prints the new worktree
sbx exec --session attempt-1-alt --cwd /new/dir -- make test        # new session in the fork
sbx checkpoints --session attempt-1 --cwd /path/in/repo --prune    # delete that session's refs
```

## What is recorded

After a command finishes **and its edits were pulled back to the host**, the host work tree of the
repository that holds the first code dir is written as a commit:

- built with git plumbing only: a throw-away copy of the index (`GIT_INDEX_FILE`), `add -A`,
  `write-tree`, `commit-tree`, and a create-only `update-ref` to `refs/sbx/<session>/<seq>`
  (`<session>` normalized like the claim label, `<seq>` zero-padded from `000001`);
- the user's **HEAD, branches, tags and index are not changed**, and no hook of the repository runs
  (`core.hooksPath=/dev/null` for the ref update and for `worktree add`);
- parent: the previous checkpoint of the session, or for the first one the HEAD of that moment,
  so `git log refs/sbx/<session>/<last>` is the session's history;
- a command with a non-zero exit code is recorded too (its partial edits are real state);
- `execute()` adds `"checkpoint": "<sha>"`; it is `None` when no code dir is inside a git work tree
  or recording failed (logged as a warning; the command result is never masked).
- When best-effort pull-back fails, no checkpoint is taken and the key is absent: the host tree may
  miss the command's edits. With `agent_sandbox_strict_pull` the pull-back error is raised as before.

Host commands (git network operations that run on the host) are checkpointed the same way.

## What is not in a checkpoint

- `.gitignore`d paths (and `.git/info/exclude`, `core.excludesFile`), as git decides them.
- The directories the code sync never mirrors, at any depth: `node_modules`, `.venv`, `venv`,
  `target`, `build`, `dist`, `.gradle`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.next`,
  `.turbo` — also when they are tracked in the repository. Copy named outputs to a non-excluded dir
  such as `out/` (see [artifact receipt](artifact-pull.md)) if they must be part of the state.
- `.git` internals: commits, branches and the index the agent made inside the sandbox are pulled back
  as before, but the checkpoint is a tree snapshot, not a copy of the repository's refs. The pull-back
  never writes or deletes `.git/refs/sbx/...`, so sandbox code cannot forge or erase checkpoints.
- When the code dir is a sub-directory of the repository, the rest of the work tree is recorded as
  the host has it (it was never synced, so the sandbox could not have changed it).
- Anything outside that repository's work tree: other code dirs in other repositories, `sync_paths`
  (host to sandbox only), task files, seed dirs, the sandbox HOME, `/tmp`, caches, files the sandbox
  wrote outside the code dirs.
- Nested repositories and submodules: recorded as git records them (a gitlink commit id), not their files.
- Symlinks: recorded as links (git semantics); the sync itself does not mirror them.

## Boundaries

- **Files only.** A fork restores the code directory's files. API calls, database writes, pushes,
  messages, package publishes and other effects outside the work tree are not undone.
- No process, memory or sandbox-pod snapshot: a fork starts a fresh sandbox from the warm pool and
  pushes the forked tree like any new session.
- `fork()` is `git worktree add --detach <dir> <sha>`. The target must be absent or empty. The new
  Sandbox uses that directory as its only code dir and inherits the parent's config (override with
  `config=`). The fork's own checkpoints chain from the forked commit under its own session name.
- Checkpoints live in the host repository: they are local refs, not pushed by default refspecs
  (`refs/sbx/*` is not under `refs/heads`). `--prune` deletes the refs; the commits are then left to
  `git gc`. Forked worktrees are ordinary worktrees: remove them with `git worktree remove`.
- Two sandboxes writing the same session name into one repository race on the sequence number; the
  create-only ref update makes the loser fail (logged, `checkpoint: None`) instead of overwriting.
  Use one session name per Sandbox.
- Cost: each checkpoint hashes changed files into the host object store (unchanged files reuse the
  index stat cache). Large generated files in non-excluded dirs grow `.git/objects` until pruned and gc'd.
