# Changelog

## 0.4.0rc1 — prerelease

- Add recoverable `Allocation` snapshots, `on_allocation` acquisition callbacks,
  `ReleaseError`, and opt-in `release_verified()` / `release_allocation()` APIs.
  Verified cleanup persists the current UID-aware inventory before each attempt
  and verifies resource absence; it does not prove remote process death or pull artifacts.
- Reap local streaming process groups on timeout, writer failure and exceptional
  exits while retaining the original exception and recoverable allocation.
- Verify artifact envelopes and checksums, apply locally, then ACK separately.
  Failed receipts remain pending and are retried before another host push.
  `Sandbox.pull()` supports explicit recovery; strict receipt handling is opt-in
  through `agent_sandbox_strict_pull` / `AGENT_SANDBOX_STRICT_PULL`.
- Compatibility: default `close()` / `release()` remain best-effort and execute
  results retain `{output, returncode}`. Strict close preserves the claim on
  receipt failure. Pending receipt failures block the next command in either mode.
  File/directory type conflicts fail closed without ACK: remove the conflicting
  host entry explicitly, then call `pull()` on the same instance. Host trees are
  not recursively deleted to convert types automatically.
- Local gates cover routing, fuzzing, core behavior, lifecycle (31), artifact ACK
  (11), integrity (10), and composed lifecycle/receipt/stream regressions (5).
  Docker transport and local POSIX process groups are real; lifecycle API fixtures
  are fake. The historical v0.3.0 Temporal canary is not acceptance of these new APIs.
- **Not verified:** new-API Kubernetes/gVisor/Temporal live canaries, remote physical
  process death, durable recovery across workers, or production rollout. Verified
  cleanup may require additional observer permissions; RBAC is not silently enlarged.
- Distribution is a GitHub/Gitea prerelease, not a PyPI publication or production
  upgrade. The original private candidate remains archived separately; public
  tag `v0.4.0rc1` and its assets are immutable. See [release notes](docs/releases/0.4.0rc1.md).

## 0.3.0

- **Published on PyPI**: `pip install self-hosted-agent-sandbox`. Releases are built and uploaded
  by `.github/workflows/publish.yml` on a version tag, through a PyPI trusted publisher (no stored token).
- **Import name is now `self_hosted_agent_sandbox`** (was `agent_sandbox`), matching the
  distribution name; another PyPI project already installs a top-level `agent_sandbox`.
  Upgrading: `from self_hosted_agent_sandbox import Sandbox`. Behaviour, the `sbx` command,
  the `AGENT_SANDBOX_*` variables and `agent_sandbox_*` config keys are unchanged.

## 0.2.0

- **Variables are an allowlist now.** Host variables reach the sandbox only when
  `AGENT_SANDBOX_FORWARD_ENV` (adapters: `agent_sandbox_forward_env`) names them, by name or glob;
  the default is empty. `AGENT_SANDBOX_ENV_SKIP` still withholds names the allowlist matches.
  Upgrading: list the variables your builds need, e.g. `MY_BUILD_FLAG,NPM_CONFIG_REGISTRY`; never tokens or keys.
- A variable forwarded earlier in the session and then dropped (from the host or the allowlist)
  is unset in the sandbox.
- Git config is an exact key allowlist: identity and everyday settings from `~/.gitconfig`; for
  each repository's `.git/config` and `config.worktree` in the code dirs also format, branches, remote URLs and fetch
  refspecs. No aliases, `core.sshCommand`, `core.askpass`, hooks paths, helpers, includes,
  `http.*`, `url.*`, nor values with `user:pass@`. A reused sandbox's copy is always replaced.
- Tests: `core_unit.py` and `route_table.py` cover the allowlists with a host API key, credential
  settings in key names and in values (aliases, `sshCommand`), a repository config with a token in
  its remote URL, and unset of a dropped variable.

## 0.1.0 — first release

- Core library `agent_sandbox.Sandbox`, extracted unchanged from the Hermes plugin
  (hermes-sandbox-cluster 0.22.7-1): session-scoped claims with a renewed cluster-side expiry,
  wholesale variable forwarding with a glob skip list, declared file sync, two-way code sync with a
  `.git` allowlist, host-command routing confined to the code dirs.
- `sbx` CLI: `exec` (with `--keep`), `shell`, `release`.
- Cluster scripts moved here from the plugin repo; resource names are neutral (`agent-sandbox`
  service account, `agent-4g` example pool).
- `Sandbox.release(session)` deletes kept claims without claiming one; `stream=` accepts any binary
  file-like object.
- Known limit: the agent host must be Linux or macOS (process groups are used to stop timed-out
  commands).
- Tests: offline routing table, bash-backed fuzzer and core unit checks; `tests/e2e.sh` for the CLI
  on a real cluster.
