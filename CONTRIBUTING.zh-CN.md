# 贡献指南

[English](CONTRIBUTING.md) | **简体中文**

欢迎贡献。Issue 与 PR 都在 GitHub 上处理；本文件是一份改动必须满足什么的简版说明。

## 开发环境

```sh
git clone https://github.com/kevin-zhan/jev-loop.git
cd jev-loop
uv sync
```

`jsonschema` 是**开发/测试**依赖：它支撑用真实 Draft 2020-12 校验器判定 manifest 语料的漂移测试。
运行时与构建出的 wheel 保持零依赖（`pyproject.toml` 没有 `dependencies`）；不要把仅测试用的包挪进
运行时依赖列表。

需要 Python 3.12+，运行时零第三方依赖。`npm test`（用 RPC 真实加载 pi package）额外需要 Node 与
pi CLI。

## 提交前必须通过

```sh
uv run pytest          # 全部离线；不调用付费 API
uv run ruff check .
uv run jev-loop-doctor --offline   # 本地配置预检；不发起网络请求
npm test               # 只在改到 extensions/ 或 package.json 时需要
node --experimental-strip-types --check extensions/pi-jev.ts
```

改到 bundle 契约、CLI、模板或 skill 时，还要跑一遍可复用的一致性检查与打包 smoke：

```sh
uv run jev-loop bundle validate project:offline-switchboard
uv run jev-loop bundle conformance project:offline-switchboard --run-tests
scripts/wheel-smoke.sh            # 构建、在检出外安装、init、validate、真实运行
```

两者都离线。`wheel-smoke.sh` 使用临时目录下的一次性 venv，绝不安装进开发者自己的环境。

- 测试不得调用付费模型。需要验证真实线上形状时，写显式脚本，并在 PR 里说明成本。离线套件用 loopback
  HTTP fixture 覆盖传输层，包括认证失败、超时、redirect 与错误 body 不回显。
- 真实 Jev run 永远不进测试套件。真实脚本必须写明请求预算，只用合成输入与专用临时目录，并报告实际发生
  了多少次决策请求；失败与超时都计入预算，绝不自动重试。
- 凭据来自进程环境（默认 `TYPESAFE_API_KEY`）或显式的 `request_fn` 注入——绝不来自命令行 flag、
  task/spec、bundle config、事件或 cognition 消息，也不去扫 `.env`、keychain 或其他项目。doctor
  是离线预检，不得输出凭据的值、长度或 hash。
- 新增行为要有对应测试。bundle 标准方面包括：发现与校验不导入任何 bundle 代码（用 audit hook 子进程
  证明）、歧义名称被拒绝而不是悄悄改变旧路径语义、不支持的 `schema_version` 在执行前失败、脚手架默认
  被拒绝、声明的 inputs/config 在 run 存在之前就被强制、legacy v1 manifest 与 `jev-loop-host rpc`
  仍然可用。
- 内核方面的预期覆盖范围包括：frame 绑定被拒、四态回执、未决操作不重发、无效果动作在候选集收窄后仍能
  推进、可逆循环默认挂起（`awaiting_evidence`）且只在升级到上限后才以 `cycle_detected` 失败、
  verification `unknown` 不算成功、reducer 纯性。

## 设计约束（先读 `AGENTS.md` 与 `docs/design.zh-CN.md`）

- 内核（`src/jev_loop/core/`）不得依赖 Jev、HTTP、浏览器或时钟之外的外部状态。模型相关代码在
  `src/jev_loop/policies/`；环境行为放适配器。
- 状态的唯一真相是事件：任何状态变化都必须经由 `core/events.py` 与 `core/reduce.py` 的纯 reducer。
  禁止直接改 `RuntimeState`，禁止在 reducer 里做 I/O 或调用模型。
- 不要为了"让循环更顺畅"移除守卫（`NO_PROGRESS` / `REPEAT` / `CYCLE`）。改动守卫语义必须同时更新
  测试与 `docs/design.zh-CN.md` 的守卫表。
- `pending` / `unknown` 的操作只许查询不许重发；`idempotency=NONE` 永不盲目重试。
- 不把真实 API key、cookie 或页面登录态写进代码、测试、事件或 snapshot。
- bundle 契约的正文在 `spec/bundle-standard.zh-CN.md`，唯一实现在 `src/jev_loop/bundles/`——CLI、
  doctor 与 host 共用它。不要再加第二份 parser、resolver 或信任根检查；
  `spec/bundle-manifest-v2.schema.json` 必须与运行时校验器保持一致（有测试守着）。
- pi 是可选客户端：`docs/pi-integration.zh-CN.md` 是适配器文档，扩展不能变成定义 bundle 语义的地方。
- skill（`skills/jev-loop`、`skills/jev-bundle-creator`）必须可复制：链接保持在 skill 目录内部，
  复制到 `.agents/skills/` 后仍能解析。
- 文档以英文为 canonical：先改英文文件，再保持对应的 `*.zh-CN.*` 副本信息对等（反过来做改变含义的
  措辞修订时同样如此）。

## 提交信息

`type(scope): summary`，例如 `fix(host): preserve unconfirmed resource claims`。一次提交只做一件事；
不要把无关格式化混进功能改动。

## 许可证

提交即表示你的贡献以本仓的 [MIT 许可证](LICENSE) 授权。
