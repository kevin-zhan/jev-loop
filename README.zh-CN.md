# jev-loop

[![CI](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | **简体中文**

**为「持续观察、行动、核对结果」的 AI 任务准备的 Python 运行时。**

`jev-loop` 让模型只选择允许的下一步，执行、状态与成功校验都归代码；模型不需要记住试过什么，也不决定
什么时候停。可选的 [pi](https://github.com/earendil-works/pi) 集成（pi-jev）把合适的项目 bundle 放进
独立 worker，主 agent 推理时环境可以继续运动。

本仓库目前是私有、仅源码分发：克隆后使用；没有发布到 PyPI 或 npm。

## 问题

重复性的交互任务可能遇到模型自己注意不到的失败：

- 已经证明没有效果的动作可能又被选中，因为没有任何机制迫使它换一个选择；
- 可能把"工具返回了"当成"动作生效了"，再把"动作生效了"当成"任务完成了"；
- 循环可能在模型"思考"期间停摆，或者烧完整个步数预算也不肯命名这个环；
- 事后可能没有可回放的记录，说不清试过什么、真正改变了什么、为什么相信它成功了。

把循环、状态、停止条件和成功判定放进代码，只给模型一个收窄的、结构良好的选择——这就是本项目要
解决的问题。

## 方案

```text
observe → offer → decide → validate → execute → reduce → guard
   ↑                                                       │
   └───────────────────────────────────────────────────────┘
```

- **循环归代码所有。** 策略（Jev 或任何实现）拿到一帧不可变的 frame，只能返回该帧内的候选 id。
  观测什么、提供哪些动作、执行前的校验、停止条件和预算，全部是代码。
- **事件就是状态。** 状态由纯 reducer 从 append-only 事件日志推导，因此一次 run 可以被回放和解释，
  而不是从对话历史里重新拼出来。
- **有回执不等于有推进。** 每次执行记录四态回执（`completed / rejected / pending / unknown`）；
  `pending` 与 `unknown` 只许查询、不许盲目重发，`idempotency=NONE` 永不重试。
- **验证是独立的。** `request_finish` 只是请求验证；只有 verifier 给出 `satisfied` 才算成功，
  `unknown` 保持 unknown。
- **守卫让循环要么推进、要么诚实地停下。** 无效果写、同一观测下的重复、frame 重现，会依次收窄
  候选集、挂起、最后以一个明确原因结束。完整守卫表与背后的不变量见 [docs/design.zh-CN.md](docs/design.zh-CN.md)。

## 用例

适合：

- **慢速推理模型 + 长时间环境操作。** 持续操作环境由 bundle 负责；host 给它独立 worker 和一条向
  主 agent 异步提问的通道，循环不会阻塞在一次回复上。
- **必须能安全停止、能对账的循环。** 独占资源声明、租约、两阶段停止，以及 worker 消失且无法确认释放
  时的 quarantine。
- **需要审计轨迹的 run。** append-only 的 `events.jsonl`、bundle 自己拥有的 evidence，以及一个不是
  模型的 verifier。

本仓目前实际包含什么：

- `uv run jev-loop-demo` 三个场景（`clean`、`noop`、`cycle`）——用确定性 mock 环境演示内核语义，
  不联网、不调用模型。
- [`examples/bundles/switchboard`](examples/bundles/switchboard)——一个很小的 bundle，通过 pi-jev 跑
  内核；是你自己 bundle 的模板，不是业务适配器。
- 内建 `diagnostic` bundle——只证明桥接语义（cognition pending 时继续推进、停止时释放输入），
  永远不能作为用户任务完成的证据。

本仓不包含什么：浏览器、手机、游戏或站点适配器，也没有通用 MCP server。如果你的环境需要其中之一，
你要自己实现并验收一个项目 bundle，见 [docs/pi-integration.zh-CN.md](docs/pi-integration.zh-CN.md)。

### 一个真实集成案例（独立项目）

只读的 United 商务舱升舱（MUA）可用性查询是在**另一个私有项目**（`united-pz-jev`）里作为任务专用
bundle 实现的，不在本仓。那个 bundle 驱动一个专用、隔离、且已被站点认可的浏览器 profile，严格只读，
只报告独立证据能确认的结论。有两条边界值得在这里重复：官网显示的升舱可确认状态不等于数字 PZ 库存；
读不到库存时答案必须保持未知，不能写成零。

## 快速开始

前提：Linux / macOS / WSL 上的 Python 3.12+（host 的资源声明用 Unix `fcntl`）；推荐使用
[uv](https://docs.astral.sh/uv/getting-started/installation/)，下面的命令基于它。运行时零第三方依赖。

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # 私有仓库：需要 GitHub 访问权限
cd jev-loop
uv sync
uv run jev-loop-demo --scenario clean
```

预期输出：

```text
status=succeeded stop_reason=None
steps=3 executions=2 decisions=3 world={'a': True, 'b': True}
verifications=['satisfied'] (only 'satisfied' counts as success)
```

该 demo 完全离线：不需要 API key、不联网、不调用模型——它是语义 smoke test，不是付费 Jev 或真实业务
验收。`--scenario noop` 与 `--scenario cycle` 覆盖另外两条守卫路径（无效果收窄候选、挂起而不是烧完
步数预算），两者都在 [docs/getting-started.zh-CN.html](docs/getting-started.zh-CN.html) 里解释。

不用 uv（运行时本来就没有依赖）：

```sh
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/jev-loop-demo --scenario clean
```

## 接入方式

### A. 作为 Python 库

```python
from jev_loop import Loop, TaskSpec
from jev_loop.policies.jev import JevPolicy, http_request_fn

loop = Loop(
    task=TaskSpec(
        goal="开启深色模式",
        success_criteria=("设置页显示深色模式已开启",),
    ),
    environment=my_adapter,                  # observe / offer / execute / query / validate
    policy=JevPolicy(request_fn=http_request_fn(api_key=key)),
    verifier=my_verifier,                    # 独立证据；不接受模型的 DONE
)
result = loop.run()
print(result.status, result.stop_reason)
```

这段代码是集成示意，不是可直接运行的示例：`my_adapter`、`my_verifier` 与 `key` 都需要你自己提供——
一个 `Environment` 实现、一个独立的 `Verifier`，以及 TypeSafe / Jev API key。

内核只依赖三个协议——`Environment`、`Policy`、`Verifier`；`policies/jev.py` 是唯一碰网络的模块
（经由可注入的 `request_fn`，所以测试保持离线）。内核入口与 ports 从 `jev_loop` 导出；
完整走查示例见 [docs/getting-started.zh-CN.html](docs/getting-started.zh-CN.html)。

### B. 在 pi 里用，配合项目 bundle

仓库根本身就是一个 pi package（`package.json` 声明了 `extensions/pi-jev.ts` 与 `skills/pi-jev`）。请按 pi 的官方安装说明安装 pi（见 [pi 仓库](https://github.com/earendil-works/pi)），并确认系统里有 Python 3.12+：

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # 需要访问权限
cd jev-loop
pi install .            # 把本地 package 注册进 pi（就地引用这个克隆目录）
# 可选的远端非钉版本安装（私有仓库，仍需访问权限）：
# pi install git:github.com/kevin-zhan/jev-loop
```

之后在已打开的会话里执行 `/reload`。该 package 提供 `jev_loop` 工具
（`start / list / inspect / events / update / respond / stop / release_resources`）、`/jev-runs`、
`/jev-self-test`、`/jev-stop-all` 以及 `pi-jev` skill。扩展默认调用 `python3`
（用 `JEV_LOOP_PYTHON` 覆盖），并把本包 `src/` 加进 worker 的 `PYTHONPATH`，因此不需要把 Python 包
装到全局环境。

bundle manifest、controller 契约、运行中 cognition、生命周期与安全语义：
[docs/pi-integration.zh-CN.md](docs/pi-integration.zh-CN.md)。

### C. 接你自己的宿主

不依赖 pi。环境同步好后，`uv run jev-loop-host rpc` 从 stdin 读一个 JSON 请求、往 stdout 写一个 JSON
回应（`start / list / inspect / events / heartbeat / update / stop / respond / release_resources`）；
宿主 API 从 `jev_loop.host` 导出。JSON 是长什么样的完整示例见
[docs/getting-started.zh-CN.html](docs/getting-started.zh-CN.html)。

## 边界与安全

- **单环境、单写执行。** 第一版内核没有多环境并行写、没有自动规划、没有子 loop。每个 resource key
  最多一个活动 owner；重叠的 claim 会冲突。
- **适配器由你负责。** 本仓的 mock 环境是测试替身。真实适配器必须自己证明：观测里包含它所有前置
  条件与 verifier 需要的字段。
- **bundle manifest 是代码。** manifest 会以你的权限加载项目代码，只运行可信项目根目录内经审查的
  manifest。运行时 cognition 上下文是不可信数据，不是用户授权。
- **停止是两阶段的。** `accepted` 不代表输入已释放；需要 terminal status 加 `resources_released=true`，
  真实设备还必须有进程外 watchdog。
- **活着不等于成功。** 进程还在、诊断绿灯，都不能说明任务完成；只有 bundle 的 verifier 证据加
  terminal run status 可以。
- **内核是同步的，worker 不是。** `Loop.step()` 一次执行一个动作；pi-jev 的 managed host 能在
  cognition job pending 时让环境继续运动——这不是把同步内核改造成了异步内核。
- **仅源码分发。** 没有发布到 PyPI 或 npm；除 `pyproject.toml` 与 `package.json` 里的版本号字段外
  没有版本 tag。

## 文档

| 文档 | English | 简体中文 |
|---|---|---|
| 外部工程师上手说明（三条路径、bundle 契约、安全、常见坑） | [getting-started.html](docs/getting-started.html) | [getting-started.zh-CN.html](docs/getting-started.zh-CN.html) |
| pi 集成与 bundle 契约 | [pi-integration.md](docs/pi-integration.md) | [pi-integration.zh-CN.md](docs/pi-integration.zh-CN.md) |
| 内核不变量与守卫表 | [design.md](docs/design.md) | [design.zh-CN.md](docs/design.zh-CN.md) |
| Question 传输格式规范（v0.1），设计/规范文档 | [questions.md](spec/questions.md) | [questions.zh-CN.md](spec/questions.zh-CN.md) |
| State 传输格式规范（v0.1），设计/规范文档 | [state.md](spec/state.md) | [state.zh-CN.md](spec/state.zh-CN.md) |
| 贡献指南 | [CONTRIBUTING.md](CONTRIBUTING.md) | [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md) |

v0.1 两份文档描述的是传输格式设计；当前内核只实现其中一个子集（映射表在 state 规范末尾）。
英文文档是 canonical 版本，中文文件与它信息对等。代码注释与运行时文案为英文。

## 贡献与许可

欢迎贡献；开发环境、必过检查与设计约束见 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。
测试不会调用付费 API。

本项目以 [MIT 许可证](LICENSE) 发布。
