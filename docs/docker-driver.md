# Docker driver (development and CI) · Docker 驱动（开发与 CI）

> [!WARNING]
> The Docker driver is for **development and CI** on a machine you already trust. With the default runtime (runc) sandbox commands share the host kernel: **it is not a security boundary.** Production stays on Kubernetes + gVisor. `AGENT_SANDBOX_DOCKER_RUNTIME=runsc` uses gVisor where Docker has it installed, but there is still no pool, quota or cluster reaper.
>
> Docker 驱动只用于**开发和 CI**，跑在你本来就信任的机器上。默认 runc 与宿主共用内核，**不是安全隔离**；生产仍用 k8s + gVisor。装了 gVisor 的 Docker 可设 `AGENT_SANDBOX_DOCKER_RUNTIME=runsc`，但依然没有预热池、配额和集群回收。

Same `Sandbox` / `sbx` code path, no cluster:

```bash
export AGENT_SANDBOX_DRIVER=docker AGENT_SANDBOX_DOCKER_IMAGE=node:24-bookworm   # an image you already have
sbx exec --session demo -- 'make test'          # runs in a fresh container
sbx exec --session demo -- git push origin HEAD # runs on this host, as with kubernetes
```

Only the lifecycle and the exec transport differ. Host-command routing, the variable allowlist, two-way code sync, the `.git` pull-back allowlist and strict pull receipts are the same code for both drivers.

| Step | kubernetes | docker |
|---|---|---|
| claim | `SandboxClaim` from a warm pool | `docker run -d --rm` (non-root `--user`, labels `agent-sandbox/session`, `/app`, `/driver=docker`, `--memory`, `--cpus`, `--pids-limit`, `--cap-drop ALL`, `no-new-privileges`, optional `--runtime`, **no mounts**) |
| exec | `kubectl exec -i` | `docker exec -i` |
| close | delete claim (`--wait=false`) | `docker rm -f <container id>` |
| reuse (`--keep`) | claim by session label | running container by session label |
| `release_verified()` | claim/Sandbox/Pod UID read-back | `docker inspect` reports the container id absent; a live container under the same name with another id is refused |
| idle backstop | cluster `shutdownTime` | the container's main process exits after `idle_ttl` without activity and `--rm` removes it |

The image needs `bash`, `tar`, `find`, `sha256sum` (and `git` for repositories). Code is mirrored at the same absolute paths, so the driver creates those directories once as root inside the container and hands only the newly created ones to the sandbox user; commands then always run as that user. When the image has no home for the user, `/home/agent` is created and used as `HOME`.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_SANDBOX_DRIVER` | `kubernetes` | `docker` selects this driver |
| `AGENT_SANDBOX_DOCKER_IMAGE` | – (required) | image to run; never pulled by tests |
| `AGENT_SANDBOX_DOCKER` | `docker` | docker CLI binary (uses its normal context / `DOCKER_HOST`) |
| `AGENT_SANDBOX_DOCKER_RUNTIME` | – (daemon default, runc) | e.g. `runsc` when gVisor is installed for Docker |
| `AGENT_SANDBOX_DOCKER_USER` | `1000:1000` | non-root uid[:gid]; root is refused |
| `AGENT_SANDBOX_DOCKER_MEMORY` / `_CPUS` / `_PIDS_LIMIT` | `4g` / `2` / `4096` | resource limits |
| `AGENT_SANDBOX_DOCKER_NETWORK` | – (daemon default) | e.g. `none` for builds that need no network |

`Allocation.driver` is `"docker"` for these targets (`claim` = container name, `claim_uid` = container id, `namespace` = `""`); ledgers written before drivers existed load as `"kubernetes"`. `Sandbox.release(session, config={"agent_sandbox_driver": "docker"})` removes kept containers by label.

Not included: warm pools, multi-runtime selection (Kata, Nitro, …), mTLS, Helm. Docker access itself is root-equivalent on the host: give it to the developer or CI job, never to the agent inside the sandbox.

Test: `SBX_TEST_IMAGE=<local image> bash tests/e2e_docker.sh` (local daemon, never pulls, removes its containers); offline: `PYTHONPATH=src python3 tests/docker_unit.py` (fake docker CLI).
