# jev-loop

[![CI](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **English summary** — `jev-loop` is an explicit-state decision runtime. Code owns the loop, the state,
> the guards and the termination; a model (Jev, or any policy) only picks one candidate inside a single frame.
> For a deterministic policy, a repeated (observation, candidate set) yields the same answer forever,
> so the runtime, not the model, has to guarantee progress: every step must either change the frame or stop
> (`NO_PROGRESS` / `REPEAT` / `CYCLE`). The repository also ships **pi-jev**, an installable
> [pi](https://github.com/earendil-works/pi) package that runs project-local behavior *bundles* in their own worker
> with leases, an append-only event journal, exclusive resource claims and asynchronous cognition.
> Zero runtime dependencies, Python ≥ 3.12 on Linux/macOS/WSL (the host uses `fcntl`). Documentation is written in Chinese; start with
> [docs/getting-started.html](docs/getting-started.html) for an external-engineer walkthrough.

显式状态驱动的决策运行时。代码拥有循环、状态、守卫和终止；**Jev（或任何策略）只在一帧之内做选择**。

它回答的问题是：`state + questions → 结构化答案` 之外的那部分——谁执行、谁记状态、谁保证不空转——应该长什么样。

```text
observe → offer → decide → validate → execute → reduce → guard
   ↑                                                       │
   └───────────────────────────────────────────────────────┘
```

仓库同时提供 **pi-jev**：一个可安装的 pi package，把项目内行为 bundle 放进有租约、持久事件日志、独占资源
声明和异步 cognition 的独立 worker 中。主 agent 可以慢思考，而 bundle 的 driver 继续运行。它不是 pi fork，
也不包含任何特定网站/设备适配器。见 [`docs/pi-integration.md`](docs/pi-integration.md)。

## 为什么值得单独做一层

Jev 是**自我一致**的：同一个 frame 会得到同一个答案，而且没有采样随机性可以逃出重复。（见
`docs/` 中记录的本机实测：同 state 复跑逐字一致。）这意味着

> 只要 (观测, 候选集) 再次出现，答案就必然再次出现：要么是固定点，要么是极限环。
> 模型永远不会自己发现自己在打转。

所以"能不能真的 loop 起来"不由模型决定，而由代码决定：**每步必须改变 frame，或者必须停下来。**
本内核把这个约束做成了三条守卫：

| 守卫 | 触发条件 | 处理 |
|---|---|---|
| `NO_PROGRESS` | 写操作 `completed`，但观测 revision 没变 | 把该动作标为该观测下"无效"，从候选集移除 |
| `REPEAT` | 同一观测下同一动作已尝试过 | 拦截执行，加入 dead keys |
| `CYCLE` | frame 内容（观测+候选+排除集）重现 | 无事可收窄 → 挂起等待新信息；持续升级 → `cycle_detected` 失败 |

dead keys 的作用域是**当前观测**：观测一变，排除集清空——既保证确定性策略能逃出重复，
又不会因为一次无效就永久失明。

## 内核约定

- **Frame 绑定**：`build_frame` 把候选重编号为 frame 内 `a1..an`；策略只能返回这些 id。
  循环只在本帧的候选表里解析答案，并在执行前重新校验观测 revision。
- **四态回执**：`completed / rejected / pending / unknown`。"工具返回了"≠"动作生效了"≠"任务完成了"。
  `pending`/`unknown` 进入未决操作，之后只允许查询（`query`），不允许重发；`idempotency=NONE`
  的动作永远不会被盲目重试。
- **纯 reducer**：`apply(state, event)` 是纯函数，状态只能由事件推导；事件是唯一真相，可回放。
- **验证独立**：策略的 `request_finish` 只是"请求验证"。只有 verifier 给出 `satisfied` 才算成功，
  `unknown` 保持 unknown 并挂起。
- **任务不可自改**：只有显式的 `TASK_UPDATED`（用户输入）能改目标文本。
- **预算**：步数、墙钟、候选数量（默认 255）、frame 字符预算、等待次数、验证次数。
- **不依赖 Jev SDK**：内核只依赖 `Policy` 协议；`policies/jev.py` 是唯一知道 TypeSafe 形状的地方。

## 安装与验证

```sh
# 需要 Python 3.12+；运行时零第三方依赖
# 平台：Linux / macOS / WSL（host 的资源声明用 Unix 的 fcntl）
git clone https://github.com/kevin-zhan/jev-loop.git
cd jev-loop
uv sync

uv run pytest          # 全部离线，不调用模型
uv run ruff check .
npm test              # pi RPC 真实加载扩展，不调用模型（需要 pi CLI 与 Node，已验证 pi 0.85.1 / Node ≥ 22）
uv run jev-loop-demo --scenario clean
uv run jev-loop-demo --scenario noop    # 无效果动作 → 收窄候选，继续推进
uv run jev-loop-demo --scenario cycle   # 可逆循环 → 挂起等待新信息（suspended / awaiting_evidence），不烧步数预算
```

不用 uv 也可以：`python3 -m venv .venv && ./.venv/bin/pip install -e .`。

### 在 pi 里用（pi-jev）

```sh
pi install git:github.com/kevin-zhan/jev-loop@v0.2.0   # 钉版本，便于复现
pi -e https://github.com/kevin-zhan/jev-loop           # 或临时试用，不写设置
```

扩展默认调用 `python3`（用 `JEV_LOOP_PYTHON` 指定其他解释器），并把本包 `src/` 加进子进程
`PYTHONPATH`，因此不需要单独安装 Python 包。装好后提供 `jev_loop` 工具、`/jev-runs`、`/jev-self-test`、
`/jev-stop-all` 与 `pi-jev` skill。

外部工程师的完整上手说明（三条使用路径、bundle 契约、安全语义、常见坑）见
**[docs/getting-started.html](docs/getting-started.html)**。

### 不依赖 pi 的宿主

`jev-loop-host rpc` 从 stdin 读一个 JSON 请求、往 stdout 写一个 JSON 回应，动作包括
`start / list / inspect / events / heartbeat / update / stop / respond / release_resources`，
宿主 API 从 `jev_loop.host` 导出。

## 接真实环境与真实 Jev

```python
from jev_loop import Loop, TaskSpec
from jev_loop.policies.jev import JevPolicy, http_request_fn

loop = Loop(
    task=TaskSpec(goal="开启深色模式", success_criteria=("设置页显示深色模式已开启",)),
    environment=my_adapter,                    # 实现 observe / offer / execute / query / validate
    policy=JevPolicy(request_fn=http_request_fn(api_key=key)),
    verifier=my_verifier,                      # 独立证据，不接受模型的 DONE
)
result = loop.run()
```

`Adaptors`（浏览器、手机、工作区）只需要承诺三件事：能给什么观测、能执行哪些具体动作、
未决操作怎么查询。内核不假设任何环境细节。

## 目录

```text
src/jev_loop/core/     state、events、reduce、frame、guards、loop
src/jev_loop/policies/ scripted（离线/确定性）、jev（问题构建与答案解析）
src/jev_loop/adapters/ mock（可注入故障的确定性环境）
src/jev_loop/verifiers/predicate（确定性验证器）
src/jev_loop/stores/   内存与 JSONL 事件存储
src/jev_loop/host/     managed host、租约、认知 broker、bundle contract
extensions/            pi 工具、后台事件投递与 session heartbeat
skills/pi-jev/         agent 使用手册
examples/bundles/      project bundle 示例
docs/                  设计不变量、pi 集成与 bundle 契约、外部上手指南（HTML）
spec/                  问题与 state 的线上规范（v0.1）及标准 fixture
tests/                 全离线测试；tests/pi-extension/ 用 pi RPC 真实加载扩展
```

## 边界

- 内核第一版只支持**单环境、单写执行**；没有多环境并行写、没有自动规划、没有子 loop。
- managed host 可以同时管理不同资源的 run，但每个 `resourceKeys` 只允许一个活动 owner；它不会把一个内核变成多写。
- `mock` 和内建 `diagnostic` 都是测试环境，不是产品适配器；真实适配器（浏览器/手机）不在本仓。
- 候选覆盖问题（正确动作根本不在候选集里）由适配器和评测负责，内核只提供 `complete` 标记与
  `blocked` 出口，不能替它发现。

## 贡献与许可

- 贡献流程、开发约束与验证命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 本项目以 [MIT 许可证](LICENSE) 发布；Issue 与 PR 都在 GitHub 上处理。
