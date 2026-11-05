# OmniRoute

[OmniRoute](https://github.com/cheaper-inference/OmniRoute) runs its `execute_command` and `eval_code` skills through a pluggable container runtime. The `agent-sandbox` runtime sends each run to a warm pool of this cluster instead of a local container.

> [!NOTE]
> Status: proposed upstream in [OmniRoute#19129](https://github.com/cheaper-inference/OmniRoute/issues/19129) / [PR #19130](https://github.com/cheaper-inference/OmniRoute/pull/19130), **not merged yet**. Until it ships in an OmniRoute release you need a build from that branch.

This adapter does not use the Python core or `sbx`: OmniRoute talks to the cluster with `kubectl` directly. Each skill run is short and stateless, so there is no code sync and no host-command routing — the command, its stdout/stderr and its exit code are all that cross over.

## How a run works

1. create a `SandboxClaim` from the warm pool mapped to the requested image
2. wait until the claim is `Ready`
3. `kubectl exec` the command in the claimed pod
4. delete the claim (also on timeout or `SIGTERM`; the claim carries a `shutdownTime` backstop in case OmniRoute dies)

## Setup

Cluster side, follow the main [Quick start](../README.md#-quick-start): `cluster/rbac.yaml` already includes what OmniRoute needs (sandboxclaims create/get/watch/delete, pods get, pods/exec create in `sbx`), and `cluster/make-kubeconfig.sh` writes the kubeconfig. Render one pool per image OmniRoute should offer, using images that contain the interpreter (`sh` for `execute_command`, `python` / `node` for `eval_code`).

OmniRoute side (`.env`; the host needs `sh` and `kubectl`):

```bash
SKILLS_SANDBOX_RUNTIME=agent-sandbox
SKILLS_AGENT_SANDBOX_NAMESPACE=sbx
SKILLS_AGENT_SANDBOX_KUBECONFIG=/etc/omniroute/agent-sandbox.kubeconfig
# image OmniRoute asks for = warm pool that serves it
SKILLS_AGENT_SANDBOX_POOLS=python:3.12-alpine=sbx-py,node:22-alpine=sbx-js
SKILLS_AGENT_SANDBOX_POOL=sbx-py            # fallback for other allowed images
# SKILLS_AGENT_SANDBOX_CONTAINER=main        # container name in the pool template
# SKILLS_AGENT_SANDBOX_CLAIM_TIMEOUT_S=60
```

The pool, not the image name, decides what actually runs: OmniRoute's image allowlist (`SKILLS_ALLOWED_SANDBOX_IMAGES`) still applies first, and each allowed image must map to a pool. Isolation (gVisor) and CPU and memory limits come from the pool's `SandboxTemplate`. OmniRoute's own `--memory` / `--network none` settings are not applied in this mode, and pools rendered by `cluster/render-pool.py` set `networkPolicyManagement: Unmanaged`, so sandbox egress is whatever your cluster's network policy allows; add a `NetworkPolicy` for the pool if skill code must not reach the network.

## Verified

Against a kind cluster with agent-sandbox v1.0.9 and gVisor, calling OmniRoute's real skill handlers: `execute_command` reported kernel `4.19.0-gvisor` and the claimed pod's hostname, `eval_code` returned exit code 3 unchanged, a 3 s timeout ended with `killed: true`, and no claim was left afterwards.

---

## 中文

[OmniRoute](https://github.com/cheaper-inference/OmniRoute) 的 `execute_command`、`eval_code` 技能通过可替换的容器运行时执行。`agent-sandbox` 运行时把每次执行发到本集群的预热池，而不是起本机容器。

> [!NOTE]
> 状态：已向上游提交 [OmniRoute#19129](https://github.com/cheaper-inference/OmniRoute/issues/19129) / [PR #19130](https://github.com/cheaper-inference/OmniRoute/pull/19130)，**尚未合并**。正式发版前需要用该分支构建。

这个适配不用 Python 核心库和 `sbx`，OmniRoute 直接用 `kubectl` 访问集群。技能调用短小、无状态，所以没有代码同步和回宿主执行，只传命令、输出和退出码。

每次执行：从镜像对应的预热池创建 `SandboxClaim` → 等 `Ready` → 在认领的 Pod 里 `kubectl exec` → 删除 claim（超时、`SIGTERM` 同样删除；OmniRoute 意外退出时由 claim 的 `shutdownTime` 兜底）。

集群侧照主文档快速开始做：`cluster/rbac.yaml` 已包含所需权限，`cluster/make-kubeconfig.sh` 生成 kubeconfig；OmniRoute 要提供几种镜像，就渲染几个池。OmniRoute 侧配置见上面的 `.env` 示例（主机需要 `sh` 和 `kubectl`）。OmniRoute 的镜像白名单（`SKILLS_ALLOWED_SANDBOX_IMAGES`）仍先生效，每个允许的镜像都要映射到一个池；实际运行的是池的镜像，不看请求里的镜像名。隔离（gVisor）和 CPU/内存上限由池的 `SandboxTemplate` 决定；OmniRoute 自己的内存和断网设置在这个模式下不生效，而 `cluster/render-pool.py` 渲染的池是 `networkPolicyManagement: Unmanaged`，沙箱能否出网取决于集群的网络策略，技能代码不该联网时请给池加 `NetworkPolicy`。

已验证：kind 集群 + agent-sandbox v1.0.9 + gVisor，直接调用 OmniRoute 的技能：内核为 `4.19.0-gvisor`、主机名为沙箱 Pod，退出码 3 原样返回，3 秒超时 `killed: true`，结束后无残留 claim。
