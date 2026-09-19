# Jev Loop

先读 `/Users/mengxiao/workspace/AGENTS.md`。本仓是独立项目，自带 Git 与依赖。

## 这个项目是什么

显式状态驱动的决策运行时：**代码拥有循环、状态与终止；策略（Jev 或任何实现）只在一帧内做选择。**
它回答的是 `state + questions → 答案` 之外的那部分职责。设计依据见 `README.md`；第一版内核只做
单环境、单写执行，不做多环境、自动规划和子 loop。

## 边界

- 内核（`src/jev_loop/core/`）不得依赖 Jev、HTTP、浏览器或时钟之外的外部状态；所有模型相关代码在
  `src/jev_loop/policies/`。新增环境行为放适配器，不放核心。
- 状态的唯一真相是**事件**：任何状态变化都必须经由 `core/events.py` 的事件与 `core/reduce.py` 的
  纯 reducer。禁止直接改 `RuntimeState`，禁止在 reducer 里做 I/O 或调用模型。
- 策略返回的候选 id 只在本帧内有效；执行前必须重新校验观测与前置条件。`request_finish` 只是请求验证，
  只有 verifier 的 `satisfied` 才算成功；`unknown` 不得四舍五入成成功或失败。
- 不要移除守卫（无效果 / 重复 / 循环）来"让循环更顺畅"：它们是确定性策略下唯一的逃生阀。
  改动守卫语义必须同时改测试与 `README.md` 的守卫表。
- `pending` / `unknown` 的操作只许查询不许重发；`idempotency=NONE` 的动作永远不能盲目重试。
- 不把真实 API key、cookie、页面登录态写进代码或测试；联网代码只允许出现在
  `policies/jev.py` 的 `http_request_fn`。

## 验证

```sh
uv sync
uv run pytest          # 全部离线：mock 环境 + 确定性策略 + 假 requester
uv run ruff check .
uv run jev-loop-demo --scenario clean   # noop / cycle 三个场景
```

- 测试不得调用付费 API；需要真实形状验证时用显式脚本并说明成本，不放进 pytest。
- 覆盖重点：frame 绑定被拒、四态回执、未决操作不重发、无效果动作收窄候选后仍能推进、
  可逆循环以 `cycle_detected` 结束而不是烧满步数、验证 unknown 不等于成功、reducer 纯性。