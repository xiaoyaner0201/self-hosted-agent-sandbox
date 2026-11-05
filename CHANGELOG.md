# Changelog

## 0.2.0

- **Variables are an allowlist now.** Host variables reach the sandbox only when
  `AGENT_SANDBOX_FORWARD_ENV` (adapters: `agent_sandbox_forward_env`) names them, by name or glob;
  the default is empty. `AGENT_SANDBOX_ENV_SKIP` still withholds names the allowlist matches.
  Upgrading: list the variables your builds need, e.g. `MY_BUILD_FLAG,NPM_CONFIG_REGISTRY`; never tokens or keys.
- The git config copied into the sandbox is an allowlist: `user`, `core`, `init`, `pull`, `push`
  and `alias`, minus keys that name a token, password, secret, credential, key or proxy, and
  values with `user:pass@`.
- Tests: `core_unit.py` and `route_table.py` cover both allowlists, including a host API key and
  credential-bearing git settings that must not reach the sandbox.

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
