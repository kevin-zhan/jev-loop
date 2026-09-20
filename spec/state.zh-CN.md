# State 规范 v0.1

[English](state.md) | **简体中文**

给 Jev 看的那一份状态。**环境无关**：规范里不出现"浏览器/手机/游戏"这类名词，
只定义槽位；环境差异只允许出现在 `observation.data` 与选项描述文本里。

## 骨架（六个字段，缺一不可）

```json
{
  "task":        { "...": "owned by the user, read-only" },
  "observation": { "...": "owned by the environment" },
  "progress":    { "...": "computed by code" },
  "pending":     [ "..." ],
  "excluded":    [ "..." ],
  "history":     [ "..." ]
}
```

### 1. `task` — 目标与边界（只有用户输入能改）

```json
{
  "goal": "Read today's upgrade availability for SFO→HKG on 2026-11-19.",
  "inputs": { "origin": "SFO", "destination": "HKG", "date": "2026-11-19", "adults": 1 },
  "constraints": ["read-only", "do not select a fare"],
  "success_criteria": ["the result page shows MUA pricing for a United nonstop"],
  "authorization": ["read_page", "fill_form"]
}
```

- `goal` MUST 是 1–2 句祈使句，用用户的话。
- `success_criteria` MUST 能被独立验证器核对（不能写"看起来对了"）。
- `authorization` MUST 列出本次允许的能力类别；不在列表里的能力一律拒绝执行。
- MUST NOT 由观测内容、工具输出或模型判断改写（只有 `TASK_UPDATED` 事件能改）。

### 2. `observation` — 世界现在是什么样

```json
{
  "revision": "screen-18",
  "source": "browser/united-result-page",
  "observed_at": "2026-09-19T00:12:03Z",
  "stale": false,
  "data": { "theme_switch": "off", "search_box": "", "results_shown": 3 }
}
```

- `revision` MUST 只在**投影出来的世界真的变了**时改变；动画、时间、无关重绘不算。
  它是"这一步有没有推进"的唯一信号。
- `stale: true` MUST 表示"这次读取失败，payload 是上一次的"；此时任何动作都不得执行。
- `data` MUST 恰好包含：① 候选前置条件引用到的字段；② 验证器要读的字段。**不多放。**
  （这条就是"观测完整性"：动作改变的量必须在 `data` 里看得见，否则循环会在盲点里空转。）
- MUST NOT 包含原始 HTML/DOM、整页文本、截图 base64、cookie、凭证。

### 3. `progress` — 上一步的结果，由代码算

```json
{ "steps": 3, "last_action_changed": true, "no_effect_streak": 0 }
```

- 三个值全部由代码计算，MUST NOT 让模型推断。
- `last_action_changed: false` 是给模型的直接信号："你刚才做的没生效"。
- `no_effect_streak` MUST 与 `excluded` 一致：进入排除集的动作一定体现在这里。

### 4/5. `pending` / `excluded` — 不许重复的两张名单

```json
"pending":  [ { "key": "submit_search", "status": "pending", "since_step": 3 } ],
"excluded": [ "fill_search_box" ]
```

- `pending`：已受理但结果未定的逻辑操作（`pending` / `unknown`）。**只允许查询，不允许重发。**
- `excluded`：在当前观测下被证明无效（执行完成但 `revision` 未变）或已经尝试过的动作。
- 两条铁律：**名单里的 key MUST NOT 出现在本轮选项里**；`excluded` 的作用域 MUST 绑定当前
  `revision`——观测一变即清空（否则一次无效会导致永久失明）。

### 6. `history` — 有界的近期步骤

```json
[ { "step": 2, "action": "fill_search_box", "receipt": "completed", "changed": true } ]
```

- 只保留最近 ≤8 步的**结构化摘要**，不是对话记录，不是日志。
- MUST NOT 放原始页面文本、完整工具输出、无用时间戳。

## 尺寸与禁入项

| 项 | 上限 |
|---|---|
| `state` + 最长单题 | ≤ 32k tokens（模型硬限制） |
| 整次请求 | ≤ 64k tokens |
| `observation.data` | 建议 ≤ 8k 字符；超了先在代码里过滤 |
| `history` | 默认 8 条 |
| 单个数组 | MUST NOT 无界 |

禁止进入 state：原始 DOM、整页文本、图像/音频 base64、凭证与 cookie、无关的日志、
每秒变化的时间戳（会污染 `revision` 与去重）。

## 一条铁律

**state 是数据，不是指令。** 观测里的任何文字都不得被当作目标、规则或授权。
规范要求把这条写进 question 的 instructions（见 questions 规范），但真正的防线是代码：
候选过滤、参数校验、授权检查都在运行时做。

## 与当前实现的对应

规范是本文件的**设计目标**；内核 `frame.decision_state` 当前只投影了其中一个子集，映射如下。
这张表记录的是现状，不是版本计划：

| 规范 | 内核现状 |
|---|---|
| `task.*` | `goal` / `inputs` / `constraints` / `success_criteria`（`authorization` 已实现但未投影） |
| `observation.{revision,stale,data}` | `observation_revision` / `observation_stale` / `observation` |
| `progress.*` | 无（`recent_steps[].changed` 间接表达） |
| `pending[]` | `unresolved_operations[]` |
| `excluded[]` | `excluded_actions[]` |
| `history[]` | `recent_steps[]` |
