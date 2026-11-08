# Changelog

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
