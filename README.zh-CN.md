<div align="center">

# 🧊 self-hosted-agent-sandbox

**给 AI 编程 Agent 用的自部署沙箱：命令跑在你自己 k3s 节点上一次性的 gVisor 沙箱里，多机共享预热池，每个节点自带依赖缓存，git 凭据不进沙箱。**

[English](README.md) · 简体中文

![License](https://img.shields.io/badge/license-Apache--2.0-blue) ![k3s](https://img.shields.io/badge/k3s-v1.37-FFC61C) ![gVisor](https://img.shields.io/badge/isolation-gVisor-4285F4) ![agent-sandbox](https://img.shields.io/badge/kubernetes--sigs%2Fagent--sandbox-v1.0.9-326CE5) ![Self-hosted](https://img.shields.io/badge/self--hosted-multi--node-2E8B57) ![Python](https://img.shields.io/badge/python-3.10%2B%20·%20no%20deps-3776AB) ![Status](https://img.shields.io/badge/status-early-orange)

</div>

---

Agent 原来跑在哪台机器上，接入后还跑在那里，只有 **它执行的命令** 进沙箱。每个会话一个沙箱，从预热池领用约 0.2 秒，会话结束就删除。任务代码双向同步，原机器上的工作目录始终是 git 正本；`push`、`fetch`、`clone` 这类 git 联网命令回原机器执行，凭据就在那里。

底层用的是 [k3s](https://k3s.io)、[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) 和 [gVisor](https://gvisor.dev)。不需要 KVM，普通云主机就能部署。

## 🧩 仓库里有什么

| 部分 | 内容 |
|---|---|
| `cluster/` | 节点安装（给沙箱划固定内存上限）、gVisor、最小权限账号和 kubeconfig、按租户的预热池和每节点依赖缓存、缓存清理 |
| `src/agent_sandbox/` | 核心库（`Sandbox`）：领用、续期、释放，环境变量和文件同步，带 `.git` 白名单的代码双向同步，哪些命令回原机器执行。纯 Python，无依赖 |
| `sbx` | 基于核心库的命令行：`sbx exec`、`sbx shell`、`sbx release` |

## 🔌 适配

| Agent / 平台 | 接法 | 状态 |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 终端后端插件 [**hermes-sandbox-cluster**](https://github.com/xiaoyaner0201/hermes-sandbox-cluster) | ✅ 已在用，Hermes 0.21.5 和 0.22.7 验收全过 |
| Claude Code | 用沙箱提供的 MCP 工具替换自带的 Bash/Read/Write/Edit | 🛠️ 计划中 |
| Codex CLI | 官方远端执行环境（沙箱里跑 `codex exec-server`），备选 MCP | 🛠️ 计划中 |
| Multica 运行时 | 经上面几个 Agent 适配接入；覆盖分配任务、Webhook 自动化（如 `pull_request`）和 PR 唤醒 | 🛠️ 计划中 |
| [OmniRoute](https://github.com/cheaper-inference/OmniRoute) | 技能运行时 `agent-sandbox`：`execute_command` / `eval_code` 从预热池领 Pod 执行（[接入说明](docs/omniroute.md)） | 🔄 上游 PR [#19130](https://github.com/cheaper-inference/OmniRoute/pull/19130) 已提交、未合并；实验集群已验证 |

其他程序可以直接调命令行或核心库。

## 🚀 快速开始

**1. 节点**（每台 Linux 机器都执行；脚本会下载官方 k3s 安装器，离线环境可放在 `$SBX_DL/install-k3s.sh`）

```bash
# 第一台（作为 k3s 主节点）
SBX_NODE_IP=10.0.0.11 cluster/node-install.sh server
sudo cat /var/lib/rancher/k3s/server/node-token     # 拷到其他节点，存为 ./node-token

# 其他节点
SBX_NODE_IP=10.0.0.12 cluster/node-install.sh agent https://10.0.0.11:6443 ./node-token

# 每个节点
cluster/gvisor-node.sh
cluster/cache-gc-install.sh      # 可选：每天清理缓存
```

**2. 集群**（只在第一台执行一次，`kubectl` 即 `k3s kubectl`）

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox-with-extensions.yaml
kubectl create namespace sbx
kubectl apply -f cluster/runtimeclass.yaml -f cluster/rbac.yaml
cluster/render-pool.py --tenant team-a --image ghcr.io/you/agent-host:latest \
    --cache-mount /var/cache/pkg | kubectl apply -f -
cluster/make-kubeconfig.sh https://10.0.0.11:6443 > agent-sandbox.kubeconfig   # 拷到 Agent 主机
```

> [!TIP]
> 沙箱用和 Agent 主机**同一个镜像**，两边的路径、工具链和缓存相关变量就完全一致，不用额外配置。

**3. Agent 主机**

```bash
pip install git+https://github.com/xiaoyaner0201/self-hosted-agent-sandbox
export AGENT_SANDBOX_KUBECONFIG=$PWD/agent-sandbox.kubeconfig AGENT_SANDBOX_POOL=sbx-team-a
cd /workspaces/my-repo
sbx exec --session demo -- 'cat /proc/version; make test'     # 在新的 gVisor 沙箱里跑
sbx exec --session demo -- git push origin HEAD                # 在本机跑，用本机凭据
```

加 `--keep` 会保留沙箱，之后的 `sbx exec --session demo --keep` 都用同一个，直到 `sbx release --session demo`。`PATH` 里要有 `kubectl`，或者用 `AGENT_SANDBOX_KUBECTL` 指定。

在 Python 里用：

```python
from agent_sandbox import Sandbox
sb = Sandbox(cwd="/workspaces/my-repo", session="task-42")
print(sb.execute("pytest -q")["output"])
sb.close()
```

## 🧭 哪些在沙箱，哪些在原机器

只有「登记过的命令 ＋ 碰不到文件的辅助命令」才会回原机器执行。辅助命令包括开头的 `cd <目录> &&`（目录须已存在且路径上没有符号链接）、`echo`/`printf`，以及只从管道读入的过滤命令（`grep`、`jq`、`head`、`tail`、`sort`、`uniq`、`cut`、`tr`、`wc`，只认固定的短选项），再加上 `>/dev/null`。而且不能离开代码目录：工作目录、每个 `cd`、git 命令里的每个本地路径（clone 的目标目录、`file://` 远端）都必须落在代码目录内，`$变量`、`~` 和未加引号的通配符不在原机器上展开。只要带了命令替换、子 shell、heredoc、写文件的重定向、其他程序、开关以外的变量前缀（`GIT_TRACE=1` 这类开关可以），或会让 git 运行其他程序的参数（`--upload-pack`、`clone -u/-c/--template` 等），整条都留在沙箱。

> [!IMPORTANT]
> 登记**具体子命令**，不要登记一整个能运行自定义代码的 CLI。比如 `gh` 的 alias 和 extension 都能执行任意程序，应登记 `gh pr create`、`gh pr view`，而不是 `gh`。沙箱里的这些 CLI 只是一层占位：会打印提示并以 97 退出，Agent 看到后就会把它拆成单独一条再执行。

## ⚙️ 配置项

都用环境变量设置；适配层也可以从自己的配置里读，比如 Hermes 的 `config.yaml` 里写 `agent_sandbox_*`。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGENT_SANDBOX_POOL` | `agent-4g` | 从哪个预热池领沙箱 |
| `AGENT_SANDBOX_NAMESPACE` | `sbx` | 沙箱所在命名空间 |
| `AGENT_SANDBOX_KUBECONFIG` | `~/.kube/agent-sandbox.yaml` | 最小权限的 kubeconfig |
| `AGENT_SANDBOX_KUBECTL` | `kubectl` | kubectl 路径 |
| `AGENT_SANDBOX_SYNC_PATHS` | – | 按原路径推进沙箱的配置文件或目录 |
| `AGENT_SANDBOX_CODE_DIRS` | 任务目录 | 双向同步的代码目录 |
| `AGENT_SANDBOX_HOST_COMMANDS` | git 网络操作、`git remote add/set-url` | 回原机器执行的命令前缀，按词匹配（设置后替换默认列表） |
| `AGENT_SANDBOX_ENV_SKIP` | – | 额外不带进沙箱的变量名，支持 `*_TOKEN` 这类通配 |
| `AGENT_SANDBOX_TASK_FILES` | – | 任务根目录（`/workspaces/<ws>/<task>/`）里随工作目录推进沙箱的文件，不回传 |
| `AGENT_SANDBOX_IDLE_TTL` | `1800` | 集群侧到期时间（秒），有命令执行时自动续期 |
| `AGENT_SANDBOX_PERSISTENT` | `false` | 同一会话标签复用同一个沙箱 |
| `AGENT_SANDBOX_CLAIM_TIMEOUT` | `180` | 等沙箱就绪的最长秒数 |

## 🌐 网络白名单

下表中的方向，均指由哪一方主动发起连接。

**Agent 主机 → 集群**（核心库只需要这一条）

| 端口 | 目标 | 用途 |
|---|---|---|
| TCP 6443 | k3s 主节点 | Kubernetes API：申请和释放沙箱、执行命令（`kubectl exec` 也经 API Server 转发） |

**集群节点之间**

| 端口 | 方向 | 用途 |
|---|---|---|
| TCP 6443 | 工作节点 → 主节点 | 加入集群、调用 API |
| TCP 10250 | 主节点 → 各节点 | kubelet：执行命令和查日志时，转发到沙箱所在节点 |
| UDP 8472 | 所有节点互通 | flannel VXLAN 容器网络 |
| TCP 2379–2380 | 主节点之间 | 只在多主节点（内置 etcd 高可用）时需要 |

**沙箱 → 外部**：按构建需要开，比如 Maven、npm、PyPI 镜像源，Go 代理，以及节点拉镜像用的镜像仓库。

> [!NOTE]
> 不需要开任何入站规则：沙箱不访问 Agent 主机，外部也不访问沙箱。代码同步和凭据命令都由 Agent 主机主动发起。如果 Agent 主机只能通过代理访问集群，在 kubeconfig 的 cluster 段加上 `proxy-url:` 即可，`kubectl exec` 支持 HTTP 和 SOCKS 代理。

## 🔒 安全

- 沙箱运行在 **gVisor** 里，用非 root 用户，不挂载服务账号 token，也不注入服务发现变量。
- 推进沙箱的 git 配置去掉了 credential helper（读取凭据的那部分）、include、`extraheader`/cookie/SSL 私钥设置，以及带 `用户名:密码@` 的地址。
- 回传到 `.git` 里的内容按白名单放行，只有对象、引用、日志、`HEAD`、`index`、`packed-refs` 这类；配置、钩子、gitfile 等一律保持原机器上的样子。原机器上的 git 以 `safe.bareRepository=explicit` 运行，也拒绝经由指向代码目录之外的符号链接写入。这样沙箱里做的任何事，都没法埋一个钩子让之后在原机器上执行的 `git push` 去跑。
- 依赖缓存按租户隔离，一个租户装了有问题的包，不会污染别的租户。
- Agent 主机用的服务账号权限最小：只能在一个命名空间里管理 `sandboxclaims`，并进入 Pod 执行命令。
- 回原机器执行的命令沿用主机进程自己的 `PATH`。分流只检查命令文本，不检查 `PATH` 解析到哪个 `git`，所以主机 `PATH` 里不要放 Agent 能写入的目录。

> [!WARNING]
> 变量默认全部同步，主机进程变量里的密钥也会进沙箱，除非你把它们排除掉，例如 `AGENT_SANDBOX_ENV_SKIP="*_TOKEN,*_SECRET,*_PASSWORD,*_API_KEY"`。

## 🧪 测试

- `tests/route_table.py`（固定用例）和 `tests/route_fuzz.py`（随机生成命令交给真实 bash 执行，确认不会在代码目录外写文件）离线检查哪些命令回原机器执行：`python tests/route_table.py`。
- `tests/core_unit.py` 用假的 `kubectl` 离线检查报错、流式输出和 `release`。
- `tests/e2e.sh` 用真实集群测 `sbx` 命令行：gVisor、变量同步、文件和 commit 回传、回原机器执行、沙箱复用和释放、退出码。
- 各适配仓库有自己的验收（Hermes：26 + 15 项端到端检查）。

## 🔍 会接触什么

- **运行** 你指定的 `kubectl`，用你给的 kubeconfig，只连这个 kubeconfig 里的 API Server。
- **推进沙箱的内容**：主机进程变量（去掉排除清单）、`AGENT_SANDBOX_SYNC_PATHS` 和 `AGENT_SANDBOX_TASK_FILES` 里的文件、代码目录、适配层传入的预置目录（比如只读的 skills 副本），以及去掉凭据相关设置的 git 配置。
- **在主机上写的内容**：只写代码目录，内容是沙箱在其中新建、修改或删除的文件（`.git` 里只回传历史和 index；不会经由指向代码目录之外的符号链接写入）。
- **没有其他网络请求**，没有遥测，不保存凭据，不自带后台进程。

## 🛣️ 状态

早期版本。核心库原样取自 Hermes 插件，那边经过了九轮独立发布审查。节点为 Linux x86_64，k3s v1.37。

## 📄 许可证

[Apache-2.0](LICENSE)
