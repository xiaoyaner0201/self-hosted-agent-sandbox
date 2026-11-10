# Temporal 接入（候选）

[English](temporal.md) · [README](../README.zh-CN.md)

**状态：接入契约与脱敏 canary 摘要，不是仓库自带的 Temporal adapter，也不是生产切流。** 本项目仍是通用沙箱底座；Temporal 消费其 Python API，不另建一套 Kubernetes Job 控制面。

## 正式入口与责任边界

正式 Python 入口是 `from self_hosted_agent_sandbox import Sandbox`；命令行入口为 `sbx exec`、`sbx shell`、`sbx release`。SDK **0.3.0 没有 HTTP server**、提交队列、持久作业状态 API，也没有 `submit/status/wait/cancel/logs` CLI 协议。Hermes terminal plugin 同样是调用方，不是通用执行服务。

已记录 canary 的源码基线为 [3819c07](https://github.com/xiaoyaner0201/self-hosted-agent-sandbox/tree/3819c07523ee81a870914fe37f1674a0f0b816b4)，其中 Python core 与 v0.3.0 一致。**当前整合源码候选另已实现 allocation 快照、显式 verified release、本地 stream 进程组收割与 ACK/strict pull，但未使用这些新能力重跑原 Temporal/Kubernetes/gVisor canary。** 未发布本地候选暂保留 0.3.0 包元数据，安装基线 PyPI 包不能取得候选新 API。

### 当前候选 API 与历史基线的区别

- `Sandbox(..., on_allocation=save_target)` 同步提供不可变公共 `Allocation` 快照，包含 apply 前的记录；构造异常保留 `.allocation`。早期部分 handle 不等于完整 inventory，更不等于已回收。
- 停止并 join 所有申请/执行所有者后，显式调用 `sb.release_verified(...)`，或用 `Sandbox.release_allocation(target, config=...)` 恢复。每次删除尝试前持久化完整 inventory；`ReleaseError.allocation` 是后续重试目标。详见[生命周期合同](lifecycle.md)中的绑定不全 fail-closed、callback deadline、UID 校验和 Sandbox CR observer RBAC。没有默认扩权。
- 配置 `agent_sandbox_strict_pull=True` 使回传失败抛出；同实例 `pull()` 重试接收，不通过重放副作用命令补收。两种模式的 pending 都会阻止旧宿主文件覆盖远端。调用方仍须核验预期文件/hash。strict `close()` 收失败保留 allocation；verified release **不回传文件**，可能放弃未收输出。详见[制品合同](artifact-pull.md)。
- stream 超时、writer/cancellation 路径现在收割自有的**本地**独立进程组；不是跨线程 cancel API、Activity 总 deadline、远端物理进程死亡证明或申请自动取消。
- 本地生命周期 fixture、Docker tar/ACK 和 POSIX 进程组测试不等于真实 Temporal 验收。下文历史证据和 NOT RUN 项仍有效；不得依本文自动升级生产 SDK。

职责仍按以下划分：

| 归属 | 职责 |
|---|---|
| Sandbox SDK | 向指定预热池申请 `SandboxClaim`、等 Ready、经 API Server exec、续期 idle expiry、同步配置的文件、请求删除 claim |
| 共享沙箱平台 | pool/template 的镜像和工具链、gVisor、资源限制、namespace quota、身份/RBAC、缓存策略、NetworkPolicy、controller 运维 |
| Temporal adapter（调用方维护） | 输入校验、attempt 身份、heartbeat、有界进程监管、取消、结果/制品持久化、幂等和**回收验证** |
| Temporal Workflow | 确定性编排与 Activity 重试/超时策略；**不做 SDK、文件、子进程、网络或 Kubernetes IO** |

调用方不能绕过 SDK 自行拼 Job。下文过渡期的清理/回读观察的是同一批 claims，不是另一套调度器。共享 HTTP 服务需要单独评审契约，本次接入并不需要它。

## 一个 Activity attempt，一个沙箱

```text
Workflow → Activity → 可信端准备输入、记录唯一 attempt 身份
                    → Sandbox(...) → claim → Ready / 绑定 Pod
                    → execute(...) → 校验并持久化结果/制品
                    → 停止并回收本地 SDK 进程 → 请求删除
                    → 回读 claims 和所有已观察到的绑定 Pod 均不存在
                    → 返回结果（或“回收未验证”错误）
```

1. 在可信 worker 校验业务操作和不可变输入版本，带认证的输入获取留在沙箱外。分配独立 attempt 目录和短的不透明 session，例如 run/activity/attempt 加独立分配 nonce 的小写十六进制摘要；申请前先记录。SDK 会把 session 小写化、替换标点并截断到 50 字符，不能直接拿原始 Workflow ID 当唯一键。
2. 使用 `agent_sandbox_persistent=False`。构造函数同步申请沙箱：创建带 `warmPoolRef` 的 claim、轮询 Ready、读取 `status.sandbox.name` 作为 exec 目标。镜像/CPU/内存来自预置 pool/template，不是每条命令的请求参数。补热归 controller，不归 Temporal。
3. 阻塞 SDK 调用放在受监管的执行上下文，不阻塞 async Activity event loop；申请、执行、清理期间独立发 heartbeat。同一对象的命令串行执行；同一任务的相关命令收在一个 Activity 中，不跨 Activity 靠 `--keep` 串联。
4. 收集 `output`、`returncode`（或有界二进制流输出），区分命令失败、申请失败、超时/取消、回收失败。大日志/制品持久化在 Workflow history 之外，返回有界摘要/引用。需要文件时在可信端核验存在性和 checksum：退出码为零不证明回传成功。
5. 每条退出路径都须先使本地 SDK 工作收敛，再请求删除并独立验证回收，然后才报告成功。不仅处理拿到 `Sandbox` 对象后的异常，也处理构造失败。保留原始错误和清理错误；重试必须有明确幂等策略，不承诺副作用 exactly-once。

### 历史 0.3.0 调用形态，不是当前候选接入示例

下例保留基线 canary 的旧 API 形态，不是当前候选的推荐 verified cleanup。`trusted_config`、`attempt_session`、沙箱镜像/工具链应事先按下节策略配置。外层 adapter 仍须处理构造失败、heartbeat、取消、本地有界停止、绑定日志和删除回读。**单独复制这段不满足生命周期契约。** 当前候选应按上面的生命周期/制品合同实施，不要在 inventory 前先调用 legacy close。

```python
from self_hosted_agent_sandbox import Sandbox

sandbox = Sandbox(cwd="/task", session=attempt_session,
                  config=trusted_config, app="temporal")
try:
    result = sandbox.execute("printf 'sandbox smoke\n'", timeout=30)
finally:
    sandbox.close(force_remove=True)  # 仅请求删除，不代表已验证回收
```

## 可信配置与隔离

- 复现本文基线时锁定 `self-hosted-agent-sandbox==0.3.0`，worker 安装 `kubectl`。镜像须兼容 SDK 的 bash/git/tar/find 操作及业务解释器（本次 canary 用 Python），不能随意换成只有最小 shell 的镜像。
- 本调用方使用**独立 namespace、ServiceAccount/kubeconfig、pool**，不默认复用现役 Agent 的身份或池。任务与 worker 分别设置资源预算：ResourceQuota、Pod requests/limits、claim/Pod 数、预热池大小、Activity 并发和准入策略。
- 以 [cluster/rbac.yaml](../cluster/rbac.yaml) 为起点，收窄到独立 namespace：claims get/list/watch/create/patch/delete、pods get、pods/exec get/create。不授予 Jobs、Pod create/delete/list、Secret 读取、pool/template 修改或 cluster-admin。session/app 标签**不是认证边界**，同一凭据能作用于整个 namespace。
- 显式配置 `agent_sandbox_host_commands=[]`、`agent_sandbox_forward_env=[]`；无文件传输基线再设 `agent_sandbox_env_skip=["*"]`、`agent_sandbox_sync_paths=[]`、`agent_sandbox_task_files=[]`、`agent_sandbox_code_dirs=[]`。使用受控子进程环境和空的临时 HOME，避免继承的 `AGENT_SANDBOX_*` 或主机 git 身份/配置改变策略。本 adapter 禁用回宿主命令路由和主机环境变量转发。
- **0.3.0 的空 code dirs 不是全局禁同步开关。** SDK 会自动收纳 `/workspaces/<workspace>/<task>/...` 路径，包括命令中发现的任务路径。canary 使用此布局之外的 `/task`、受控命令和显式有界输入写入，不做目录镜像。未来适配若启用同步，须限定脱敏 attempt 目录，并单独验收制品合同。
- kubeconfig、Temporal 认证、代码托管/API token、状态回写留在可信 worker；不得放进命令 payload、同步文件、seed 目录或转发变量。其他文件不会自动去密。任务请求不得指定 kubeconfig、kubectl、namespace、host commands 或任意 sync paths。
- pool 配置 gVisor、非 root、无 SA token 和合适的存储隔离。canary 使用只读根文件系统、无 hostPath；通用的缓存池渲染器不等同于该配置。显式配置 NetworkPolicy：渲染器的 `networkPolicyManagement: Unmanaged` 不保证默认断网。短期 worker 凭据到期前须轮换或停用；canary 成功不等于长期凭据治理完成。

## 历史 0.3.0 生命周期与超时局限

本表只描述已记录基线；当前候选差异见上文，不能将本表作为候选测试结论。

| API/行为 | 实际保证与适配要求 |
|---|---|
| `Sandbox(...)` | 阻塞申请，默认 Ready 等待 180 秒；各子进程及初始化还会耗时，不是整个 Activity deadline。创建后中断可能没有返回对象可供 close。 |
| `execute(timeout=...)` | 限制本地命令子进程等待，不覆盖所有同步、输出及清理阶段。杀掉本地 `kubectl` 不证明远端进程树终止；stream 模式另开本地进程 session，只杀 runner 进程组不足够。 |
| Idle TTL | 默认 1800 秒，命令期间续期；是滑动 idle expiry，不是不可延长的执行总时限。worker/node 死亡及 controller 回收须另测。 |
| `close(force_remove=True)` | 最后回传后以 `--wait=false` 删除 claim；delete 非零仅记日志，不作为回收验证错误抛出，子进程异常本身仍可能抛出。返回不代表 claim/Pod 已消失。 |
| `Sandbox.release(session, config=...)` | 不申请新沙箱，列出并删除同 session claims，delete 失败会报错；返回数量不证明消失，selector 不区分 app/pool。仅用于独立 namespace 内独占的 attempt session。 |
| Persistent 复用 | 查找匹配 claim，没有原子预约或持久命令/结果状态；并发同 session 不适合 attempt 隔离。 |
| 文件回传 | 失败可能仅 warning，命令结果照常返回；制品完成须另验。 |

要验证回收，必须在删除前记录 claim 身份及**每个已观察到的绑定 Pod**；claim 消失后无法再从它恢复绑定关系。先确认本地申请/exec 工作不会再创建晚到 claim，再按独占 session 列出/删除，并逐个按精确名称回读已记录 Pod。有界回读必须证明匹配 claims 为空、所有已观察到的 Pod 均不存在。list/get 失败、绑定未知、构造路径未收敛或超过 deadline，一律是**回收未验证**，不能报告成功。晚绑定/构造中断竞态仍是下文明确未验收项，空列表本身不足够。

分别预算申请、执行、本地停止/reap、输出/制品处理、回收验证和重试退避，再设置 Temporal heartbeat、start-to-close / schedule-to-close。取消 `asyncio.to_thread(...)` 不会停止底层线程，必须监管并等待真实工作收敛。不要把进程监管或运行时 monkey-patch 隐藏在号称可生产使用的示例里。

## 真实证据边界

**记录的隔离 canary：2026-11-10；本次文档修改没有重跑。** 单独维护的调用方 adapter 使用 SDK 0.3.0、Python 3.13.18、temporalio 1.35.0、k3s v1.37.1+k3s1 与 gVisor。测试使用独立队列、worker/task namespace、身份、pool 和显式预算；未切换生产 worker/webhook/状态回写，既有池 UID/spec 前后快照未变。这不能证明业务延迟完全不变。

| 已记录演练 | 观察结果 |
|---|---|
| Smoke | Workflow 完成，命令返回 0，内核 `4.19.0-gvisor` |
| 非零命令 | 两次 attempt 后 Workflow 失败，命令退出码 3 被上报 |
| 执行超时 | 两次 attempt 命中 SDK 约 90 秒 execute 超时，Workflow 失败；不是 adapter 210 秒外层预算或 6 分钟 Activity start-to-close 测试，诊断文字仍较泛。 |
| Temporal cancel | 确认 claim 已绑定 Pod 后取消；Workflow 取消，完成回收回读 |
| 动态分配/回收 | 四种演练共观察到 6 个唯一 session/claim 和 6 个绑定 Pod；测试 claim/Pod 逐个回读不存在，按设计留下 1 个补热 Pod。 |
| RBAC | 修正后的授权矩阵 20/20，另有真实 forbidden list 请求。最初 exec 子资源探测是假阴性的 CLI 参数解析问题，不是放宽 RBAC 的理由。 |
| 网络/凭据 | task 对选定内网服务/API/DNS 端口的 TCP 连接被拒，同时间 worker 基线可连接，exec 仍成功；task 未观察到 SA token 或应用凭据环境变量。**未测 UDP DNS。** |

这是私有验收记录的脱敏摘要，**不是可公开复现的测试包，也不是独立公开验证**。不附原始日志、私有地址、身份、token 或代码托管路径。离线 core/routing fixture 检查不能替代真实调用方 canary；canary 也不意味着 adapter 已内置进 SDK。

仍 **NOT RUN / 未验收**：kill-worker/kill-node TTL 兜底、构造中断/晚到 claim、delete 失败注入、并发 quota 压力、坏镜像池、UDP DNS 与更多网络路径、完整制品合同、真实受保护分支 CI、生产切流。不能从 smoke/cancel 成功推导这些结论。

## 原提案在整合候选中的落地范围（非实机验收）

原文提出以下三项 SDK 后续需求；当前整合源码只实现其中有界子集，不能把原提案误读为当前尚无 API，也不能当作实机验收：

1. **公共 allocation handle 与状态/绑定观察接口**：阻塞等 Ready 前即可取得 namespace、claim 名称/UID，并记录绑定 Pod 身份，使构造中断可协调处理，不依赖 `_claim` / `_pod`，也不丢绑定。
2. **基于 handle 的有界幂等 verified release**：返回结构化删除/回读状态，上报权限/controller 错误，区分删除请求已接受与 claim/Pod 实际不存在；删除前保全绑定。
3. **可取消的申请/执行所有权**：停止并 reap 所有本地子进程（含 stream 独立 session），组合各阶段 deadline，明确远端终止以沙箱销毁验证建立，而非只看本地进程退出。

第 1–2 项已有上文链接的 `Allocation`、`on_allocation`、异常 `.allocation`、`release_verified()`、`release_allocation()` 公共合同，仅有本地 fixture 覆盖。第 3 项仅实现 stream 本地进程组收割；申请所有权、Activity 总 deadline 组合及跨线程取消仍由调用方负责。绑定不全需要外部 reaper。Temporal heartbeat/retry、attempt/结果持久化、业务授权、制品校验及凭据仍归 adapter。范围不包含 HTTP 服务、新调度器、Job backend、exactly-once 承诺或生产发版。

## 源码核对入口

- [Python 导出](../src/self_hosted_agent_sandbox/__init__.py)、[core 实现](../src/self_hosted_agent_sandbox/core.py)：构造/申请、`execute`、`release`、`close`、同步和路由语义。
- [CLI](../src/self_hosted_agent_sandbox/cli.py)、[包元数据](../pyproject.toml)：正式命令与版本。
- [RBAC](../cluster/rbac.yaml)、[pool 渲染器](../cluster/render-pool.py)：namespace 权限与池策略基线，不是本次 canary 部署清单。
- [离线 core 检查](../tests/core_unit.py)、[路由检查](../tests/route_table.py)、[集群 E2E](../tests/e2e.sh)：不同测试层，集群脚本不是 Temporal 验收套件。
