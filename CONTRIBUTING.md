# 贡献指南

Contributions are welcome. Issues and pull requests are handled on GitHub; this file is the
short version of what a change has to satisfy.

## 开发环境

```sh
git clone https://github.com/kevin-zhan/jev-loop.git
cd jev-loop
uv sync
```

需要 Python 3.12+；运行时零第三方依赖。`npm test`（pi RPC 真实加载扩展）额外需要 Node 与 pi CLI。

## 提交前必须通过

```sh
uv run pytest          # 全部离线，不调用付费 API
uv run ruff check .
npm test               # 改到 extensions/ 或 package.json 时
```

- 测试不得调用付费模型；需要验证真实形状时写显式脚本，并在 PR 里说明成本。
- 新增行为要有对应测试：帧绑定被拒、四态回执、未决操作不重发、无效果动作收窄候选后仍能推进、
  可逆循环以 `cycle_detected` 结束、verification unknown 不等于成功、reducer 纯性。

## 设计约束（改动前先读 `AGENTS.md` 与 `docs/design.md`）

- 内核（`src/jev_loop/core/`）不依赖 Jev、HTTP、浏览器或时钟之外的外部状态；模型相关代码只放
  `src/jev_loop/policies/`，环境行为放适配器。
- 状态的唯一真相是事件：变化必须经由 `core/events.py` + `core/reduce.py` 的纯 reducer，
  禁止直接改 `RuntimeState`，禁止在 reducer 里做 I/O 或调用模型。
- 不要为了"让循环更顺畅"移除守卫（`NO_PROGRESS` / `REPEAT` / `CYCLE`）；改守卫语义必须同时改
  测试与 `README.md` 的守卫表。
- `pending` / `unknown` 的操作只许查询不许重发；`idempotency=NONE` 永不盲目重试。
- 不把真实 API key、cookie、登录态写进代码、测试、事件或 snapshot。
- 改到 pi package 或 bundle 契约时，同步更新 `docs/pi-integration.md`。

## 提交信息

`type(scope): summary`，例如 `fix(host): preserve unconfirmed resource claims`。
一次提交只做一件事；不要把无关格式化混进功能改动。

## 许可证

提交即表示你的贡献以本仓的 [MIT 许可证](LICENSE) 授权。
