# Explicit lifecycle ownership (0.4.0rc1)

`Sandbox` stays framework-independent. Existing Hermes/CLI callers may continue using
`close()` unchanged. Callers needing a cleanup verdict can opt into public target snapshots
and verified release; there is no new server, worker, queue or implicit reaper.

## Compatibility and new API

| API | Contract |
| --- | --- |
| `close(force_remove=False)` | Default: final best-effort pull; async claim delete unless persistent; forgets private `_claim` even on nonzero delete exit. Opt-in strict pull instead raises on failed receipt and retains the claim. Does **not** verify release. `force_remove` does not bypass strict receipt or mean Kubernetes force deletion. |
| `Sandbox.release(session, config=...)` | Unchanged: label-list and async deletes; returned integer counts requests, not confirmed reclamations. |
| `sb.allocation` | Latest immutable `Allocation`, retained after close, successful release, or failed release. No credentials. `None` before any allocation attempt. |
| `Sandbox(..., on_allocation=save_target)` | Synchronous callback before claim apply, after claim UID/binding observation, and during verified inventory. A raised callback stops progress. No additional Sandbox observer calls during default construction. |
| constructor exception `.allocation` | Latest target (possibly `None` or only a planned name), attached without replacing the original exception type. An ambiguous apply failure therefore still exposes the attempted claim. |
| `sb.release_verified(timeout=30, poll_interval=0.2)` | Explicit destructive release, even for persistent sessions. **No artifact pull**. Returns the fully inventoried allocation only after exact claim, Sandbox CR and bound Pod reads all confirm absence. |
| `Sandbox.release_allocation(target, config=..., timeout=30, poll_interval=0.2, on_allocation=save_target)` | Same verdict from a persisted target, without constructing/claiming a sandbox. Uses the target namespace, not a different namespace in config. |
| `ReleaseError.allocation` | Latest retry target on an unverified result (delete rejection, timeout, observer denial, identity mismatch, invalid response, callback failure). Cancellation/KeyboardInterrupt remains its original type and also carries `.allocation`. |

`Allocation` is an immutable named tuple with fields `namespace`, `claim`, `claim_uid`,
`sandbox`, `sandbox_uid`, `pod`, `pod_uid`, `inventoried`. Store `target._asdict()` as JSON;
restore with `Allocation(**saved_dict)`. An early snapshot can be incomplete. Do not infer
Pod identity merely from the constructor's Sandbox name: inventory reads the Sandbox CR,
including its legacy `agents.x-k8s.io/pod-name` annotation (otherwise the Pod has the
Sandbox's name). The `inventoried` bit means identities were observed, **not** that release
is verified; only successful return from verified release gives that verdict.

## Ownership pattern

```python
from self_hosted_agent_sandbox import Allocation, ReleaseError, Sandbox

# save_target is YOUR synchronous, atomic/durable ledger writer; supplied by the caller.
# It receives snapshots before apply and, importantly, complete inventory before delete.
sb = Sandbox(session="attempt-unique-id", config=config, on_allocation=save_target)
try:
    result = sb.execute("make test", timeout=60)
finally:
    # First quiesce any other execution/acquisition owners. Do not run close() first.
    # If command failure/cancellation and cleanup both fail, retain BOTH causes in your adapter.
    sb.release_verified(timeout=15)
```

For constructor failure or process restart, recover the latest persisted snapshot (or
`error.allocation`) and call `Sandbox.release_allocation(...)`. Supply the original cluster
configuration explicitly; snapshots deliberately do not carry kubeconfig paths/credentials.
Keep the target on `ReleaseError`, retry with that target, and report cleanup as unverified
until the API actually returns successfully. Constructor/setup failure does **not** silently
release resources or hide the original error.

The example's `finally` illustrates ownership only: production adapters must preserve the
original command/cancellation exception separately rather than let cleanup failure replace it.
Artifact validation/pull-back is separate: default execute/close pull errors may be logged
and swallowed; opt-in strict mode propagates them, and `pull()` always propagates receipt
errors. See [acknowledged receipt](artifact-pull.md). A release success is not an artifact-delivery receipt.

## What verified release actually does

1. Establish one monotonic budget covering instance lock acquisition (instance method),
   exact inventory reads, claim deletion and every subsequent read-back. Each kubectl gets
   only the remaining budget. Normal OS process startup/kill/reap overhead is not a realtime
   SLA; arbitrary caller callbacks cannot be forcibly deadline-limited.
2. Read the exact claim and preserve its UID and Sandbox binding. Inventory the Sandbox CR
   and actual Pod names/UIDs **before** requesting deletion. Notify the caller synchronously
   so the complete target can be persisted. Every release attempt reconfirms the complete
   snapshot through the callback before deletion, including unchanged inventoried retries.
   Callbacks may receive the same snapshot more than once and should be idempotent. A failed
   callback leaves an observed retry target, not proof of persistence, and prevents deletion
   on that attempt. UID/name/namespace mismatches fail closed.
3. Send a foreground claim DELETE with UID and, when present, resourceVersion preconditions
   using `kubectl delete --raw=<claim URI> -f -` and `v1/DeleteOptions`. This avoids deleting a
   replacement claim if the name was reused after inventory. Ordinary delete permissions
   still apply. It is **not** force deletion, and no Sandbox/Pod DELETE is sent by this API.
4. Read exact claim, Sandbox and Pod names with `--ignore-not-found -o json` until all three
   are absent. Nonzero exit, malformed nonempty response or changed UID is failure, never
   absence. Delete exit zero is never the verdict. A saved full inventory supports retry
   after the claim has vanished, including when the other two resources are still deleting.

No default RBAC changes: this opt-in API needs `get` on
`sandboxes.agents.x-k8s.io` in addition to claim reads/deletes and Pod reads. Current
`cluster/rbac.yaml` does not grant Sandbox CR observation; denial is an explicit unverified
result **before deletion**. Operators must choose observer permission or an authorized reaper.

## Deliberate boundaries

- Stop/join acquisition and execution owners before release. The core does not serialize
  execute against cleanup, stop a remote process directly, or prevent a separate actor from
  recreating resources after the final read. A read-back is a Kubernetes API observation,
  not proof of physical process death on an unreachable node.
- If a claim has not bound, or its Sandbox/Pod disappeared before complete inventory,
  verified release fails closed without deleting anything. It cannot prove an unknown
  late-created/orphaned resource absent. Recover with owner-UID inventory in a privileged
  external reaper; do not turn an empty claim list into success. Calling legacy close before
  inventory may cause exactly this information gap.
- No SIGKILL-proof ledger/reaper is provided. Persist snapshots atomically and retain the
  attempted name even when apply fails. Callbacks run in-process and may themselves fail;
  a crash between an API side effect and the next snapshot is still a caller recovery case.
- Streamed default execution owns an independent **local** process group. Timeout, writer
  failure and BaseException while waiting/draining kill that group and reap its direct child;
  successful leader exit also kills leftover local descendants so an inherited pipe cannot
  hold the stream open. The caller's group is never targeted. Local kubectl death does not
  prove the remote shell stopped. Remote termination/reclamation needs cluster-side evidence.
- Normal output drain preserves the existing slow-writer contract; command timeout is not
  an arbitrary user-writer deadline. On exceptional cleanup a permanently blocked writer can
  leave its daemon reader thread alive (join is capped at five seconds). Python cannot safely
  interrupt arbitrary user `write()` implementations.
- Buffered execution, custom `run_in_sandbox`, host routing and their timeout semantics
  are unchanged by the lifecycle component. The integrated receipt component reserves
  pending receipt across command/callback exceptions before another push; see its contract.
  This candidate does not add a cross-thread `cancel()` API.
- Framework attempt identity, heartbeat, retry/cancellation delivery, durable ownership,
  remote-marker checks, cleanup margin and original-error handling belong in adapters such
  as Temporal. Cancelling `asyncio.to_thread()` does not stop its underlying work; own a
  killable process if that is needed. Do not duplicate an entire Temporal backend in core.

## Offline checks / live acceptance gate

```sh
PYTHONPATH=src python tests/lifecycle_unit.py
python tests/route_table.py
python tests/core_unit.py
```

The lifecycle suite drives a fixed kubectl subprocess interface plus **real POSIX subprocess
groups**. It does not prove Kubernetes controller/finalizer/GC behavior, actual RBAC, gVisor,
remote cancellation, TTL expiry or Temporal server scheduling. Before rollout, a separately
authorized disposable-cluster test must verify exact UID-aware API absence and that a remote
progress marker stops; no production migration or adapter rollout is implied by these tests.

Source contract for raw DELETE stdin support: [kubectl v0.34.1 delete implementation](https://github.com/kubernetes/kubectl/blob/v0.34.1/pkg/cmd/delete/delete.go#L320-L329).
