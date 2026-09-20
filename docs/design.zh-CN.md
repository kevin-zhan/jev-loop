# 设计：核不变量

[English](design.md) | **简体中文**

内核只保证三件事：**只有通过校验的选择才会执行**、**状态只由真实事件推导**、**只有证据满足才宣布
完成**。为了让"循环"真的有活性（而不是停下），还需要一组更强的不变量。

## 为什么需要守卫

对于确定性策略——同一份观测与候选集永远给出同一个答案，也没有采样随机性可以逃出去——有：

> 只要 (观测, 候选集) 再次出现，答案必然再次出现：要么是固定点，要么是极限环。
> 模型永远不会自己发现自己在打转。

因此循环的推进力只能来自代码：**每一步要么改变 frame，要么停下来**。内核实现了三种逃生方式：

1. **收窄候选集**：把无效果或已尝试的动作从候选集移除（`dead_actions`）。frame 因此改变，确定性策略
   被迫做出不同选择。
2. **挂起**：无事可收窄时（frame 重现但没有可排除的动作），带唤醒条件挂起，而不是重问模型。
3. **命名失败**：反复升级（`max_guard_escalations`）后，以 `cycle_detected` / `no_progress` 结束。

| 守卫 | 触发条件 | 处理 |
|---|---|---|
| `NO_PROGRESS` | 写操作 `completed`，但观测 revision 没变 | 把该动作标为该观测下"无效"，从候选集移除 |
| `REPEAT` | 同一观测下同一动作已尝试过 | 拦截执行，加入 dead keys |
| `CYCLE` | frame 内容（观测 + 候选 + 排除集）重现 | 无事可收窄 → 挂起等待新信息；持续升级 → 以 `cycle_detected` 失败 |

dead keys 的作用域是**当前观测**：观测一变，排除集清空——既保证确定性策略能逃出重复，又不会因为
一次无效就永久失明。

## 不变量清单

| 编号 | 不变量 | 违反时的表现 | 由谁保证 |
|---|---|---|---|
| I1 | 每一步都从新观测开始；读失败保留旧 revision 并标记 stale | 在旧世界上做决定 | `OBSERVED` + `observation_stale` |
| I2 | 执行前重新校验：frame 归属、观测 revision、候选是否已被排除、授权 | 执行越权或不存在的动作 | `_validate_candidate` / `default_validate` |
| I3 | 有副作用的动作先写意图再执行；回执四态入事件流 | 崩溃后无法判断是否发生过 | `EXECUTION_INTENT` → `EXECUTION_RECEIPT` |
| I4 | pending/unknown 只许查询，不许重发；`idempotency=NONE` 永不盲目重试 | 重复副作用 | `unresolved` + `build_frame` 过滤 |
| I5 | `dead_actions` 的作用域是当前观测；观测一变即清空 | 一次无效导致永久失明 | `reduce.apply` 的 `OBSERVED` 分支 |
| I6 | frame 指纹只覆盖能带来推进的内容（观测、候选、排除集、目标），不含历史 | 因历史增长而永远检测不到环 | `frame.build_frame` |
| I7 | reducer 是纯函数；状态只能由事件推导 | 状态不可回放/不可解释 | `reduce.apply` |
| I8 | 只有 verifier 的 `satisfied` 才算成功；`unknown` 保持 unknown | 把模型的 DONE 当完成 | `_verify` |
| I9 | 目标文本只由显式 `TASK_UPDATED` 改变 | 网页/工具输出改写任务 | `reduce.apply` |
| I10 | 步数、墙钟、候选数、frame 体积、验证次数都有硬上限 | 失控成本 | `LoopConfig` |

## 四态回执的语义

| 回执 | 含义 | 运行时行为 |
|---|---|---|
| `completed` | 结果明确（可能成功也可能失败） | 记录；做效果检测（revision 是否变化） |
| `rejected` | 明确没有开始执行 | 记录；条件修复后可再考虑 |
| `pending` | 已受理，结果未定 | 进入未决；下一步只查询 |
| `unknown` | 无法确定是否发生 | 进入未决；只查询，**不重试** |

"工具调用返回了" ≠ "动作达到效果" ≠ "任务完成"。内核把这三件事分别记录、分别判断。

## 已知边界

- 单环境、单写执行。多环境并行写、子 loop、自动规划都不在第一版。
- 候选覆盖（正确动作根本不在候选集里）无法由内核发现，只能靠适配器 + 评测；内核提供 `complete`
  标记与 `blocked` 出口，并在 `blocked` 时把"候选集声称完整/不完整"记入事件。
- 观测延迟（动作效果晚于观测）会被判成一次 no-effect；这是有意的保守策略：宁可标死一次，
  也不重复提交。效果随后出现时观测 revision 改变，该动作自动回到候选集。
- 循环本身不产生正确性：每个候选仍可能被语义上选错，最终结论仍要靠 verifier 独立核对。

## 相关文档

- [`spec/questions.zh-CN.md`](../spec/questions.zh-CN.md) 与 [`spec/state.zh-CN.md`](../spec/state.zh-CN.md)：
  发给模型的 question 与 state 的传输格式规范/设计（v0.1），含每条规则的来源与尺寸上限；
  [`spec/fixture-dark-mode.json`](../spec/fixture-dark-mode.json) 是同一份规范的标准 fixture。
  内核当前实现是该规范的一个子集，映射表在 `state` 规范末尾。
- [`pi-integration.zh-CN.md`](pi-integration.zh-CN.md)：managed host / bundle 契约 / cognition 与生命
  周期语义。
- [`getting-started.zh-CN.html`](getting-started.zh-CN.html)：外部工程师上手说明。
