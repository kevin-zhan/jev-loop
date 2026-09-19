# jev-loop

显式状态驱动的决策运行时。代码拥有循环、状态、守卫和终止；**Jev（或任何策略）只在一帧之内做选择**。

它回答的问题是：`state + questions → 结构化答案` 之外的那部分——谁执行、谁记状态、谁保证不空转——应该长什么样。

```text
observe → offer → decide → validate → execute → reduce → guard
   ↑                                                       │
   └───────────────────────────────────────────────────────┘
```

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
uv sync
uv run pytest          # 全部离线，不调用模型
uv run ruff check .
uv run jev-loop-demo --scenario clean
uv run jev-loop-demo --scenario noop    # 无效果动作 → 收窄候选，继续推进
uv run jev-loop-demo --scenario cycle   # 可逆循环 → 命名失败，不烧步数预算
```

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
```

## 边界

- 第一版只支持**单环境、单写执行**；没有多环境并行写、没有自动规划、没有子 loop。
- `mock` 是测试环境，不是产品依赖；真实适配器（浏览器/手机）不在本仓。
- 候选覆盖问题（正确动作根本不在候选集里）由适配器和评测负责，内核只提供 `complete` 标记与
  `blocked` 出口，不能替它发现。