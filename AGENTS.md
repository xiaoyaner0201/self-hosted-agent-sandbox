# Repository guide

- `README.md` and `README.zh-CN.md` are the bilingual entry points; align their capability/status claims.
- Python 3.10+, standard library only. Public SDK: `src/self_hosted_agent_sandbox/core.py` and `__init__.py`; CLI: adjacent `cli.py`. No HTTP server or new scheduler.
- Read `docs/lifecycle.md`, `docs/artifact-pull.md` and `docs/temporal.md` before changing their contracts. Temporal's recorded v0.3.0 canary is historical evidence, not live acceptance of this integrated candidate.
- Preserve default `close()`/`release()` and `{output, returncode}` compatibility. Strict pull is opt-in; pending receipt blocks stale host pushes in either mode. Keep directory/git/symlink exclusions unchanged.
- Use one serialized command stream per `Sandbox`. Stop/join execution and acquisition owners before verified release; deletion acceptance, verified API absence, remote process death and artifact receipt are distinct verdicts.
- Allocation snapshots and original cancellation/errors must remain recoverable. Persist complete inventory before every verified-delete attempt, including retries. Verified release does not pull artifacts.
- Opt-in verified cleanup needs Sandbox CR observer permission; do not silently enlarge `cluster/rbac.yaml`. `cluster/` is platform provisioning/policy examples, not consumer production config.
- Keep framework heartbeat/retry, durable ledgers/reapers, business authorization and artifact storage/validation in callers. No bundled Temporal backend or production rollout.
- Offline gates: `python3 tests/route_table.py`, `python3 tests/route_fuzz.py`, `python3 tests/core_unit.py`, `PYTHONPATH=src python3 tests/lifecycle_unit.py`.
- Disposable Docker gates: `python3 tests/artifact_pull.py`, `python3 tests/artifact_integrity.py`, `PYTHONPATH=src python3 tests/integration_cross.py`; require an already-local image, never pull or bind production data. Lifecycle is fake; transport/tar and local POSIX process groups are real. On macOS set `TMPDIR` under `/Users` or `/Volumes`.
- `tests/e2e.sh` requires an explicitly authorized disposable cluster. Do not run cluster writes, production fault injection, push, tag, publish or deploy without separate authorization.
- Keep secrets, private endpoints, local evidence paths and personal identities out of tracked docs. Do not publish private acceptance bundles.
