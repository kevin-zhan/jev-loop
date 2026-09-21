# jev-loop

[![CI](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | **简体中文**

**由 Jev 驱动的高速智能循环。**

Jev 让百毫秒级 AI 判断成为可能。[^latency] `jev-loop` 把这种能力变成一个持续循环：观察任务状态，用
Jev 选出下一步，执行，用实际观测到的结果更新状态，然后继续决策。高频的局部决策不必再占用完整的慢速
agent 推理回合。

循环、执行与停止条件仍归代码所有——模型只在一个明确给出的候选集里选下一步，每个周期都有界、可回放、
可验证。可选的 [pi](https://github.com/earendil-works/pi) 集成（pi-jev）把合适的项目 bundle 放进
独立 worker，主 agent 推理时环境可以继续运动。

[^latency]: 延迟说明：公开示例项目
    [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/flights-measurement.json)
    在 commit `1231850a` 记录的 Jev 决策请求为 17 次、中位数 178 ms（见该示例自己的
    `docs/flights-measurement.json`）。这是那一个外部示例与负载下的决策请求计时，不是 TypeSafe 的
    SLA，不是通用延迟或硬实时保证，也不是整个任务的耗时；观测、执行、网络与输入规模都会影响循环实际
    跑多快。

本仓库目前是私有、仅源码分发：克隆后使用；没有发布到 PyPI 或 npm。

## 为什么需要它

交互环境不会等人。当一个推理模型在每一步之前都重新规划整个任务时，每个局部决策都要花掉一整个 agent
回合；等选择回来，页面、屏幕或进程早已变化。这是时效性问题：反复出现的局部决策，正是慢推理循环跟不
上的地方。

它同样也是效率问题：把一整回合的推理、重读上下文和文本生成，花在一次有界的单步判断上，延迟与 token
都很贵。Jev 正好补上这一端：System One 模型返回软件可直接消费的结构化判定与概率，于是判断路径可以放进
循环内部，而不是绕着循环转。

于是按成本分工：Jev 基于当前状态提供快速局部判断，循环执行选中的步骤，并把观测到的结果写回状态。复杂
规划、内容生成和异常仍可以回到慢速主 agent；能独立推进的 bundle 继续工作；依赖未满足的步骤等待，而不
是盲动。

## 它如何工作

```text
task state → Jev decision → action → observed state update → next decision
     ↑                                                              │
     └──────────────────────────────────────────────────────────────┘
```

1. **观察任务状态。** bundle 上报下一步决策所需的可观测状态：当前事实、进展、约束与最近动作。这里的
   "全局状态"以任务为界——不是全知世界模型，也不是把原始 DOM 塞进 prompt。
2. **请 Jev 选择下一步。** 策略拿到一帧不可变的 frame，里面有该状态和此刻有效的候选动作，返回候选 ID
   与决策元数据；循环在执行前校验这个选择。
3. **执行并观察。** bundle 执行动作；循环记录回执并重新读取环境，而不是假定动作生效了。
4. **更新状态、继续决策。** 新的观测进入 append-only 事件日志，状态被重新推导，下一帧被给出——直到
   独立 verifier 确认任务完成，或循环以一个明确原因结束。

执行器不隐藏策略：环境适配器归 bundle，判断保持显式。策略能决定什么（给出的动作、前置条件、预算、停止
条件）由代码持有，这正是快速模型可以安全持续运行的原因。

## 用例

本循环适合这样的工作：能报告**可靠的当前状态**、提供**明确的下一步动作**、并对"变了什么"给出
**快速反馈**：

- **动态浏览器交互。** 页面要逐步读取和操作，而每一步的选择不应该等一整个推理回合。
- **游戏、模拟器与设备。** 需要每隔几百毫秒给出新判断的长时间输入。这些是适用方向，不是现成集成：
  每个都需要专门实现并验收的 bundle（含自己的 adapter；真实设备还需要进程外 watchdog）。
- **慢速推理模型 + 长时间环境操作。** 持续操作环境由 bundle 负责；host 给它独立 worker 和一条向
  主 agent 异步提问的通道，循环不会阻塞在一次回复上。
- **必须能安全停止、能对账的循环。** 独占资源声明、租约、两阶段停止，以及 worker 消失且无法确认释放
  时的 quarantine。
- **需要审计轨迹的 run。** append-only 的 `events.jsonl`、bundle 自己拥有的 evidence，以及一个不是
  模型的 verifier。

本仓目前实际包含什么：

- `uv run jev-loop-demo` 三个场景（`clean`、`noop`、`cycle`）——用确定性 mock 环境演示内核语义，
  不联网、不调用模型。
- `uv run jev-loop-doctor`——本地 Jev 配置的离线预检：不发起网络请求，也不打印凭据值。
- [`examples/live-files`](examples/live-files)——可直接运行的真实参考用例：真实 Jev 决策请求、带
  合成内容的本机文件系统环境，以及独立 verifier。
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

准备接真实 Jev？见[接入真实 Jev 服务](#接入真实-jev-服务)，内含预检与可运行参考用例。

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
from jev_loop.policies.jev import JevPolicy, default_request_fn

loop = Loop(
    task=TaskSpec(
        goal="开启深色模式",
        success_criteria=("设置页显示深色模式已开启",),
    ),
    environment=my_adapter,                  # observe / offer / execute / query / validate
    policy=JevPolicy(request_fn=default_request_fn()),   # 从进程环境读 TYPESAFE_API_KEY
    verifier=my_verifier,                    # 独立证据；不接受模型的 DONE
)
result = loop.run()
print(result.status, result.stop_reason)
```

这段代码是集成示意，不是可直接运行的示例：`my_adapter` 与 `my_verifier` 需要你自己提供——一个
`Environment` 实现和一个独立的 `Verifier`。`default_request_fn()` 从进程环境读取凭据；缺失或不可用
时会在第一个动作之前给出具名错误。已经持有密钥的嵌入代码仍可用 `http_request_fn(api_key=...)` 显式
注入。完整可运行用例见下文[接入真实 Jev 服务](#接入真实-jev-服务)。

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

## 接入真实 Jev 服务

快速开始 demo 与默认测试套件均离线，不调用付费 API。真实 run 需要由**你自己提供** TypeSafe / Jev API
key；本仓提供的是受支持的配置入口、离线预检与可运行的 reference bundle。上文路径 A 的代码是你自己
搭建集成的示意，它发出的真实请求属于你的付费调用。

### 凭据

- 受支持的入口是环境变量 `TYPESAFE_API_KEY`，由 `jev_loop.policies.jev` 里的
  `default_request_fn()` 读取。已经持有密钥的代码仍可用 `http_request_fn(api_key=...)` 或
  `JevPolicy(request_fn=...)` 显式注入；`default_request_fn(env_name=...)` 可换用其他变量名。
- 库**只读进程环境**：不解析 `.env`、不查 keychain、不读其他项目的配置。key 缺失、空白或含无法
  放进 HTTP header 的字符时，会在第一个网络或环境动作之前抛出具名错误，绝不静默降级到 mock。
- 不要把 key 写进命令行 flag、run spec/task、`bundle_config`、事件、snapshot 或 cognition 消息：
  这些都会被持久化并回读给 agent。
- 把 key 放进进程环境，然后直接运行命令。两种受支持方式：
  - 在 shell 里 `export TYPESAFE_API_KEY=...`，再跑 `uv run jev-loop-doctor`；
  - 或保存在私有文件里，让 uv 显式加载该文件。只有在还没有私有文件时才复制空模板——绝不覆盖已有文件：

```sh
# 仅当你还没有把该 key 保存在本地文件时：
cp .env.example .env && chmod 600 .env
# 若 .env 已存在，请保留它并直接编辑该文件，不要复制覆盖
uv run --env-file .env jev-loop-doctor
```

  `--env-file` 接的是真实文件路径，由 uv 在命令运行前加载进进程环境；库自身不读、不解析该文件，
  也不会打印其值。

### 离线预检

```sh
uv run jev-loop-doctor            # 人类可读报告
uv run jev-loop-doctor --json     # 面向脚本/agent 的同一份报告
uv run jev-loop-doctor --offline  # 只查安装与运行时；不需要 key
```

doctor 校验 Python ≥3.12、host 依赖的 Unix `fcntl`、endpoint/model/timeout 取值与凭据是否存在。
它的 JSON 标注 `mode: "preflight"`，并把 `authentication`、`connectivity`、`model_availability`
保持为 `not_checked`：绿灯只是本地配置结论，不是真实 API 验收。退出码：`0` 本地配置通过，
`2` 凭据缺失/空白/不可用，`3` 非秘密配置无效或平台不支持，`1` 未知错误。可用
`--bundle examples/live-files/bundle.json` 顺带检查 run 将要加载的 manifest。

### 可直接运行的真实参考用例

[`examples/live-files`](examples/live-files) 自包含、可跨机器运行，不需要私有仓或浏览器 profile：
真实 Jev 决策、带固定**合成**内容的本机文件系统环境，以及独立 verifier。

```sh
# 变量已在当前进程环境中：
uv run python examples/live-files/run.py --max-steps 8 \
    --workspace "$(mktemp -d)/workspace"

# 或显式加载私有文件（真实路径；库自身不会读取该值）：
uv run --env-file .env python examples/live-files/run.py --max-steps 8 \
    --workspace "$(mktemp -d)/workspace"
```

发生的事情按顺序是：

1. 先校验配置与凭据；通过之后才创建工作区。
2. 工作区必须是新建或空目录——非空目录或 symlink 会被拒绝；本示例绝不覆盖、移动或删除不是它自己
   创建的内容。
3. 每个循环步骤就是一次真实的 Jev 决策请求，候选集来自该 frame；`--max-steps`（默认 8）限制
   请求次数。
4. 环境执行真实的文件操作（写入 `inbox/alpha.txt` 内容、把 `inbox/gamma.log` 归档到
   `archive/gamma.log`），循环在每一步之后重新读取目录。
5. `LiveFilesVerifier` 自己重读文件系统并作出判定；模型的回答无法让它通过。证据写入工作区里的
   `verification.json`，run 会打印该路径。
6. 不会自动删除任何东西：请先复核工作区，再自行删除。

它的退出码：成功 `0`；凭据缺失或被服务拒绝 `2`；循环本身失败或验证未满足 `1`；配置、工作区或命令行用法错误 `3`。
它是参考示例，不是生产适配器，也不是 benchmark。`examples/` 不会打进构建出的 wheel：请从源码克隆
运行（如上）；`jev-loop-doctor` 随包安装，在克隆目录之外也能用。同一个 adapter 与 verifier 也可作为
managed bundle 运行：

```sh
uv run jev-loop-host rpc <<'JSON'
{"action":"start","owner_id":"engineer","idempotency_key":"live-files-1",
 "project_root":"/path/to/jev-loop","bundle":"examples/live-files/bundle.json",
 "task":{"goal":"prepare the synthetic inbox"},"max_runtime_seconds":120}
JSON
```

key 来自显式文件时，给同一条命令加上前缀 `uv run --env-file .env` 即可。

之后用 `inspect` / `events` 读取状态与证据，需要时用 `stop` 停止。只有 terminal status、
`resources_released=true` 且 `controller.verification.verdict=satisfied` 才算完成。该 bundle 始终在
`run_dir/artifacts/workspace` 工作，两次 run 不可能共享目录；它的 `bundle_config` 只接受 `model`、
`api_url`、`timeout` 与 `max_steps`（1..64，默认 8）。`inspect` 会报告生效的 `max_steps` 与
实际发生的 `decision_requests`。

### pi、OpenRouter 与其他 provider

pi-jev 不会替 Jev 读取 pi 的凭据。worker 继承 pi 进程的环境，因此请从已加载该变量的进程启动 pi——
在 shell 里 export，或用 uv 显式加载一个文件：

```sh
uv run pi                     # 变量已在当前 shell 环境中
uv run --env-file .env pi     # 或显式加载私有文件
```

`/reload` 不会重新导入 key；请启动新的 pi 进程。

慢思考始终是 pi 自己的事，在 pi 里配置。如果你的 pi 会话使用其他 provider（例如 OpenRouter），请在
pi 侧配置：pi 支持 `/login openrouter` 与 `OPENROUTER_API_KEY` 变量（见
[pi 仓库](https://github.com/earendil-works/pi) 的 `docs/providers.md`、`docs/models.md`）。
`TYPESAFE_API_KEY` 只用于经 `policies/jev.py` 发出的 Jev 决策请求；jev-loop 不自带其他 provider
客户端。

## 可靠性从哪来

快速判断只有在循环本身可信时才有用，因此循环、状态与结论都由代码持有：

- **循环归代码所有。** 策略（Jev 或任何实现）拿到一帧不可变的 frame，只能返回该帧内的候选 id。
  观测什么、提供哪些动作、执行前的校验、停止条件和预算，全部是代码；守卫会限制无效果的重复。
- **事件就是状态。** 状态由纯 reducer 从 append-only 事件日志推导，因此一次 run 可以被回放和解释，
  而不是从对话历史里重新拼出来。
- **有回执不等于有推进。** 每次执行记录四态回执（`completed / rejected / pending / unknown`）；
  `pending` 与 `unknown` 只许查询、不许盲目重发，`idempotency=NONE` 永不重试。
- **验证是独立的。** `request_finish` 只是请求验证；只有 verifier 给出 `satisfied` 才算成功，
  `unknown` 保持 unknown——工具返回不等于动作生效，动作生效也不等于任务完成。
- **守卫让循环要么推进、要么诚实地停下。** 无效果写、同一观测下的重复、frame 重现，会依次收窄
  候选集、挂起、最后以一个明确原因结束。完整守卫表与背后的不变量见
  [docs/design.zh-CN.md](docs/design.zh-CN.md)。

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
