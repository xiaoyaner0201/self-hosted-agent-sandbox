# Temporal integration (candidate)

[简体中文](temporal.zh-CN.md) · [README](../README.md)

**Status: integration contract and sanitized canary summary, not a bundled Temporal adapter or a production rollout.** This repository remains a general-purpose sandbox foundation. Temporal is a consumer of its Python API, not a second Kubernetes Job control plane.

## Entry point and ownership

The supported Python entry point is `from self_hosted_agent_sandbox import Sandbox`. The public command interface is `sbx exec`, `sbx shell`, and `sbx release`. SDK **0.3.0 has no HTTP server**, submit queue, durable job-status API, or `submit/status/wait/cancel/logs` CLI protocol. The Hermes terminal plugin is another consumer, not a generic execution service.

The recorded canary baseline is [3819c07](https://github.com/xiaoyaner0201/self-hosted-agent-sandbox/tree/3819c07523ee81a870914fe37f1674a0f0b816b4); its Python core matches v0.3.0. **This integrated source candidate additionally implements allocation snapshots, opt-in verified release, local stream process-group reaping and acknowledged/strict pull. None of those additions has been rerun in the recorded Temporal/Kubernetes/gVisor canary.** Package metadata remains 0.3.0 for this unpublished local candidate; a baseline PyPI install does not contain the candidate APIs.

### Current candidate API versus historical baseline

- Use `Sandbox(..., on_allocation=save_target)` to persist immutable public `Allocation` snapshots, including before apply; constructor exceptions retain `.allocation`. A partial handle is not a complete inventory or proof of reclamation.
- After stopping/joining all execution/acquisition owners, opt into `sb.release_verified(...)` or recover with `Sandbox.release_allocation(target, config=...)`. Persist the full inventory before every deletion attempt; `ReleaseError.allocation` remains the retry target. See [lifecycle contract](lifecycle.md) for incomplete-binding fail-closed behavior, callback deadlines, UID checks and required Sandbox CR observer RBAC. No default RBAC expansion is included.
- Enable `agent_sandbox_strict_pull=True` for receipt errors to propagate; use `pull()` on the same instance to retry receipt, never replay side effects just to recover files. Pending receipt blocks a stale host push in either mode. Check expected files and hashes yourself. Strict `close()` retains the allocation on receipt failure; verified release deliberately does **no pull** and may abandon unreceived outputs. See [artifact contract](artifact-pull.md).
- Stream timeout/writer/cancellation paths now reap the independently owned **local** process group. This is not a cross-thread cancel API, a whole-Activity deadline, remote process-death proof, or automatic acquisition cancellation.
- Local lifecycle fixtures, Docker tar/ACK tests and POSIX group tests are distinct from a live Temporal acceptance. Keep the historical evidence and NOT RUN gates below; do not auto-upgrade production SDKs from this guide.

The responsibility split remains:

| Owner | Responsibility |
|---|---|
| Sandbox SDK | Request a `SandboxClaim` from a configured warm pool, wait for Ready, exec through the API server, renew idle expiry, synchronize configured files, request claim deletion |
| Shared sandbox platform | Pool/template image and toolchain, gVisor, resource limits, namespace quotas, identities/RBAC, cache policy, NetworkPolicy and controller operation |
| Temporal adapter (consumer-owned) | Input validation, attempt identity, heartbeat, bounded process supervision, cancellation, result/artifact persistence, idempotency and **verified cleanup** |
| Temporal Workflow | Deterministic orchestration and Activity retry/timeout policy; **no SDK, filesystem, subprocess, network or Kubernetes I/O** |

A consumer must not bypass the SDK by constructing Jobs. The temporary cleanup/read-back logic described below observes the same claims; it is not a separate scheduler. A shared HTTP service would need a separately reviewed contract and is not required for this integration.

## One Activity attempt, one sandbox

```text
Workflow -> Activity -> trusted staging and unique attempt identity
                    -> Sandbox(...) -> claim -> Ready / bound pod
                    -> execute(...) -> validate and persist result/artifacts
                    -> stop/reap local SDK work -> request deletion
                    -> read back claims AND all observed bound pods as absent
                    -> report result (or cleanup-unverified failure)
```

1. Validate the business operation and immutable input revision on the trusted worker. Fetch authenticated inputs there, outside the sandbox. Allocate an isolated attempt directory and a short opaque session (e.g. a lowercase hex digest of run/activity/attempt plus a unique allocation nonce); record it before acquisition. Session normalization lowercases, replaces punctuation and truncates at 50 characters, so a raw Workflow ID is not a safe unique key.
2. Use `agent_sandbox_persistent=False`. Construction is synchronous and acquires the sandbox immediately. It creates a claim with `warmPoolRef`, polls Ready, then uses `status.sandbox.name` as the exec target. Image/CPU/memory come from the preconfigured pool/template, not per-command request fields. Pool replenishment belongs to the controller, not Temporal.
3. Run blocking SDK calls in a supervised execution context; do not block an async Activity event loop. Heartbeat independently, including while acquiring or cleaning up. Serialize commands on each object. Keep a task's related commands in one Activity, rather than passing `--keep` across Activities.
4. Collect `output` and `returncode` (or bounded binary streaming output). Map command failure, acquisition failure, timeout/cancel and cleanup failure distinctly. Persist large logs/artifacts outside Workflow history and return bounded summaries/references. If files matter, verify their existence and checksums on the trusted side: a zero return code does not prove pull-back succeeded.
5. In all exit paths, settle local SDK execution, request claim deletion, and independently verify cleanup before reporting success. Account for constructor failure, not just exceptions after a `Sandbox` object was returned. Preserve the original failure as well as cleanup errors. Retry only under an explicit idempotency policy; side effects are not exactly-once.

### Historical 0.3.0 SDK shape, not a current-candidate recipe

The following preserves the baseline canary's legacy API shape, not recommended verified cleanup for the current candidate. `trusted_config`, `attempt_session`, and the sandbox image/toolchain must already be provisioned under the policy below. An outer adapter must still handle constructor failure, heartbeat, cancellation, bounded local shutdown, binding journals and deletion read-back. **Copying this block alone does not meet the lifecycle contract.** For the candidate, follow the lifecycle and artifact contracts above instead of calling legacy close before inventory.

```python
from self_hosted_agent_sandbox import Sandbox

sandbox = Sandbox(cwd="/task", session=attempt_session,
                  config=trusted_config, app="temporal")
try:
    result = sandbox.execute("printf 'sandbox smoke\n'", timeout=30)
finally:
    sandbox.close(force_remove=True)  # deletion request, NOT verified reclamation
```

## Trusted configuration and isolation

- Pin `self-hosted-agent-sandbox==0.3.0` for the recorded baseline; install `kubectl` on the worker. Use an image compatible with the SDK's bash/git/tar/find operations and any business interpreter (Python in the canary), not an arbitrary minimal shell image.
- Give this consumer its **own namespace, ServiceAccount/kubeconfig and pool**, separate from existing agents. Set task and worker resource budgets independently (ResourceQuota, per-pod requests/limits, claim/pod counts, pool size, Activity concurrency and admission policy). Do not consume another agent's pool or identity by default.
- Start from [cluster/rbac.yaml](../cluster/rbac.yaml), scoped to the dedicated namespace: claims get/list/watch/create/patch/delete, pods get, pods/exec get/create. Do not grant Jobs, Pod create/delete/list, Secret reads, pool/template mutations or cluster-admin. A session/app label is **not authorization**: the credential can act throughout its namespace.
- Set `agent_sandbox_host_commands=[]`, `agent_sandbox_forward_env=[]`, and, for a no-transfer baseline, `agent_sandbox_env_skip=["*"]`, `agent_sandbox_sync_paths=[]`, `agent_sandbox_task_files=[]`, `agent_sandbox_code_dirs=[]`. Use a controlled child environment and empty temporary HOME so inherited `AGENT_SANDBOX_*` settings and host git identity/config cannot silently change policy. No host-command routing or host environment forwarding is allowed for this adapter.
- **Empty code dirs are not a universal sync-disable switch in 0.3.0.** The SDK auto-adopts paths under `/workspaces/<workspace>/<task>/...`, including task paths found in commands. The canary uses `/task`, outside that layout, and controlled commands with explicit bounded input staging, not directory mirroring. If a future adapter opts into sync, restrict it to sanitized attempt directories and separately validate the artifact contract.
- Kubeconfig, Temporal authentication, source-control/API tokens and status callbacks stay on the trusted worker; never put them in command payloads, synced files, seed directories or forwarded variables. Other files are not automatically secret-scrubbed. Input requests must not choose kubeconfig, kubectl, namespace, host commands or arbitrary sync paths.
- Configure gVisor, non-root, no service-account token and appropriate storage isolation in the pool. The canary used a read-only root filesystem and no hostPath; the generic cache-enabled renderer is not that profile. Establish explicit NetworkPolicy: the renderer's `networkPolicyManagement: Unmanaged` is not a deny-all guarantee. Rotate or stop short-lived worker credentials before expiry; one successful canary is not a credential-lifecycle solution.

## Historical 0.3.0 lifecycle and timeout limits

This table describes the recorded baseline only. Candidate API differences are stated above; it is not a table of candidate test results.

| API/behavior | Actual guarantee and adapter requirement |
|---|---|
| `Sandbox(...)` | Blocks for acquisition (Ready wait defaults to 180 s); individual subprocesses and initialization add time. This is not a whole-Activity deadline. A constructor interrupted after create may leave no returned object to close. |
| `execute(timeout=...)` | Bounds the local command subprocess wait, not every sync/output/cleanup phase. Killing local `kubectl` is not proof the remote process tree stopped. Stream mode starts a new local process session; killing only the runner's process group is insufficient. |
| Idle TTL | Default 1800 s, renewed during commands; sliding idle expiry, not a hard wall-clock execution limit. Worker/node death and controller reclamation require separate tests. |
| `close(force_remove=True)` | Final pull-back, then delete claim with `--wait=false`. Nonzero delete results are logged, not raised as verified cleanup errors; subprocess exceptions can still raise. Return does not mean the claim or pod is gone. |
| `Sandbox.release(session, config=...)` | Lists/deletes matching session claims without acquiring a new sandbox; reports delete errors. Its count is not proof of disappearance, and its selector does not filter app/pool. Use only an exclusively owned attempt session in a dedicated namespace. |
| Persistent reuse | Selects a matching claim, without atomic reservation or durable command/result state. Concurrent same-session calls are unsafe for attempt isolation. |
| File pull-back | Failures may be warnings while the command result is returned. Artifact completion must be checked separately. |

For verified cleanup, journal claim identity and **every observed bound pod before deletion**; once a claim disappears its binding cannot be recovered from it. First ensure no local acquisition/exec work can create a late claim, then list/delete by the exclusively owned session and read each recorded pod by exact name. A bounded read-back must show no matching claims and every observed pod absent. List/get errors, unknown bindings, unfinished constructor work or deadline expiry mean **cleanup unverified**, not success. A late-binding/constructor interruption race remains an explicit untested gate below; an empty list alone is not sufficient.

Budget acquisition, execution, local stop/reap, output/artifact handling, cleanup verification and retry backoff separately, then configure Temporal heartbeat and start-to-close/schedule-to-close limits to accommodate the intended policy. Canceling `asyncio.to_thread(...)` does not stop its thread; supervise and settle actual work instead. Do not hide process supervision or runtime monkey-patching inside a purportedly production-ready example.

## Evidence boundary

**Recorded isolated canary, 2026-11-10; not rerun by this documentation change.** A separately maintained consumer adapter used SDK 0.3.0 with Python 3.13.18, temporalio 1.35.0, k3s v1.37.1+k3s1 and gVisor. It used a dedicated queue, worker and task namespaces/identities/pool with explicit budgets. Production workers/webhooks/status writes were not switched; existing pool UID/spec snapshots were unchanged. This does not establish unchanged business latency.

| Recorded exercise | Observed result |
|---|---|
| Smoke | Workflow completed; command returned 0 and gVisor kernel `4.19.0-gvisor` |
| Nonzero command | Workflow failed after two attempts; command exit code 3 surfaced |
| Execution timeout | Two attempts hit the SDK's approximately 90 s execute timeout; Workflow failed. Not a test of the adapter's 210 s outer budget or a 6 minute Activity start-to-close deadline; diagnostic text remained generic. |
| Temporal cancel | Canceled after claim-to-pod binding was observed; Workflow canceled and cleanup read-back completed |
| Dynamic allocation/reclamation | Six unique sessions/claims and six bound pods observed across the four exercises; every test claim and bound pod read back absent. One replacement warm pod remained by design. |
| RBAC | Corrected 20/20 authorization matrix, plus actual forbidden list requests. The initial exec-subresource probe was a CLI-parsing false negative, not a reason to broaden RBAC. |
| Network/credentials | Tested task TCP connections to selected internal services/API/DNS port refused while worker baselines connected; exec still worked. No service-account token or application credentials observed in task env. **UDP DNS was not tested.** |

This is a sanitized summary of private acceptance records, **not a publicly reproducible test bundle or independent public verification**. Raw logs, private endpoints, identities, tokens and source-control paths are intentionally not included. Offline core/routing checks verify SDK behavior with fixtures; they do not substitute for this live consumer canary, and the canary does not make the adapter part of this SDK.

Still **NOT RUN**: kill-worker/kill-node TTL recovery, interrupted constructor/late claim creation, delete-failure injection, concurrent quota pressure, broken-image pools, UDP DNS and broader network paths, a full artifact contract, real protected-branch CI and production cutover. Do not infer these from smoke/cancel success.

## Follow-up disposition in the integrated candidate (not live-accepted)

The original guide proposed three SDK follow-ups. This integrated source candidate implements a bounded subset; the original proposal must not be mistaken for current absence or for live acceptance:

1. **Public allocation handle and status/binding observation** available before the blocking Ready wait, including namespace, claim identity/UID and bound pod identity, so constructor interruption can be reconciled without `_claim`/`_pod` access or losing bindings.
2. **Bounded, idempotent verified release** on that handle: return structured deletion/read-back state, surface permission/controller errors, and distinguish request accepted from claim/pod actually absent. Preserve bindings before deletion.
3. **Cancellation-aware execution/acquisition ownership**: stop/reap all locally spawned subprocesses (including stream sessions), compose deadlines across phases, and document that remote termination is established by verified sandbox teardown rather than local process exit alone.

Items 1–2 now have the public `Allocation`, `on_allocation`, exception `.allocation`, `release_verified()` and `release_allocation()` contracts linked above, with local fixture coverage only. Item 3 is only partly implemented: stream process-group reaping is local; acquisition ownership, composable whole-Activity deadlines and cross-thread cancellation are still caller responsibilities. Incomplete binding requires an external reaper. Temporal heartbeat/retry policy, attempt/result persistence, business authorization, artifact validation and credentials remain adapter responsibilities. No HTTP service, new scheduler, Job backend, exactly-once promise or production release is included in this scope.

## Source checkpoints

- [Python export](../src/self_hosted_agent_sandbox/__init__.py), [core implementation](../src/self_hosted_agent_sandbox/core.py): construction/acquisition, `execute`, `release`, `close`, sync and routing semantics.
- [CLI](../src/self_hosted_agent_sandbox/cli.py), [package metadata](../pyproject.toml): supported commands and version.
- [RBAC](../cluster/rbac.yaml), [pool renderer](../cluster/render-pool.py): namespace permission and pool policy baseline; not the canary deployment manifests.
- [Offline core checks](../tests/core_unit.py), [routing checks](../tests/route_table.py), [cluster E2E](../tests/e2e.sh): distinct test layers; the cluster suite is not a Temporal acceptance suite.
