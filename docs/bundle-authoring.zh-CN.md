# 编写 Jev Loop bundle

[English](bundle-authoring.md) | **简体中文**

这是编写 bundle 的实践指南：creator skill、模板、离线测试与一致性检查路径。规范性契约见
[spec/bundle-standard.zh-CN.md](../spec/bundle-standard.zh-CN.md)，本文不重复其内容。

## 1. 先判断 bundle 是否是正确形态

当环境必须在 agent 推理期间**持续运转**时才值得写 bundle：重复的“观察→选择下一步动作”、可靠可读的
当前状态、明确的候选动作、快速反馈，以及安全停机的需求。读一个文件、抓一个页面、跑一次测试或改代码
是一次工具调用，不是 bundle。

写代码前先回答：哪些状态可以被可靠读取？哪个动作能推动它？目标成立的**独立**证据是什么？哪个外部输入
必须被独占持有？如果 verifier 无法独立于模型，这个 bundle 还没准备好。

## 2. 使用 creator skill

本仓提供两个 Agent Skill：

- `skills/jev-bundle-creator/` —— 编写流程、manifest 契约、检查清单与离线测试指导。
- `skills/jev-loop/` —— 运行时/消费方：发现、校验、请求协议、心跳、cognition 与安全停机。

任何能读取 Agent Skills 的 harness 都可以使用；把 skill 目录复制到 `.agents/skills/`（项目级）或
`~/.agents/skills/`（用户级）即可。本仓不会改动你的全局 agent 配置。

```sh
# 任意已安装包的机器上生成脚手架
jev-loop bundle init review-notes --description "整理待审阅收件箱"
```

它会在 `<project_root>/.agents/jev-bundle/review-notes/` 写入：

| 文件 | 用途 |
|---|---|
| `bundle.json` | manifest v2 契约，`scaffold: true` |
| `BUNDLE.md` | agent 启动前阅读的说明 |
| `bundle_controller.py` | controller 骨架；实现前 `run` 会抛错 |
| `tests/test_bundle_offline.py` | 用标准库 `unittest` 检查 factory 形状与 manifest 契约 |

`init` 绝不覆盖任何东西：目标路径已存在就整体拒绝，所有文件独占创建，中途失败只清理本次创建的内容。
模板随包分发，因此在检出之外的 wheel 安装中同样可用。

请使用唯一的 controller 模块名（模板用 `bundle_controller`）——host 会把 bundle 自身目录加入
`sys.path`，两个都叫 `controller` 的 bundle 会在同一进程里冲突。

## 3. 填好契约

- 给输入与配置声明类型、边界，以及在写错就危险的地方用 `additionalProperties: false`。它们会在 run
  创建之前被强制校验。`config` 是**默认值**，会与调用方的 `bundle_config` 做浅合并；静态检查会校验你
  声明的每个默认值，但不要求默认值自身满足 `required`，而合并后的对象会在 start 时严格校验。
- 声明 bundle 真正持有的资源键，并在启动时把同样的键放进 `resource_keys`。声明是建议性的；run 的
  claim 才是真的。
- 只声明凭据的**变量名**。绝不写值，也不写别人私有文件的路径。
- 保持 `python_path` 位于 bundle 目录内。
- 说明停机与验证意图，并让 `max_steps`/截止时间保持诚实。
- 在下面工作完成前保持 `scaffold: true`。

## 4. 实现 controller

四个方法，以及不会变的规则：

```python
def run(self, stop_event: threading.Event) -> ControllerResult: ...
def snapshot(self) -> Mapping[str, Json]: ...
def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None: ...
def request_stop(self, reason: str) -> None: ...
```

- `run` 在 engine 线程执行；其余三个可能由协调线程调用，必须线程安全。
- `request_stop` **同步**释放已持有的外部输入，绝不等待模型或网络响应。只有在释放得到确认时才返回
  `resources_released=True`；否则 host 会隔离该 claim，这是诚实的结果。
- `snapshot` 有界且可 JSON 序列化；完整证据放在 run 的 `artifacts/`。
- 长问题走 `services.cognition.request(...)`；等待答复期间循环继续推进，且只有在重新核对世界前提之后
  才采用结果。
- 托管既有 `jev_loop.Loop` 是最短路径：使用 `jev_loop.host.LoopController`（见下方参考 bundle）。

## 5. 离线测试

写下不需要模型、网络和设备的测试：factory 形状、manifest 契约、adapter 的 observe/offer/execute 行为
（包括一次无效果动作与一次前提失败）、verifier 依据真实世界分别给出 `satisfied` **和** `unsatisfied`，
以及循环在环境停止推进时以具名原因结束而不是空转。

```sh
cd .agents/jev-bundle/<name>
python3 -m unittest discover -s tests -v
```

用于发现半渲染模板的 token 扫描是刻意有界的：它绝不跟随 symlink（因此读不到 bundle 之外），会剪枝隐藏
目录与缓存目录，并把遍历锚定在固定的目录描述符上，因此被替换的目录或文件不会被跟随。达到预算、读不到某个文件、或读取过程中文件发生变化时，
它会报告 `token_scan_incomplete`，并且不产生指纹（而不是产生一个部分指纹）。bundle 内请使用常规文件
（FIFO 或链接会被跳过并报告），不要在扫描期间改动文件，并让 `BUNDLE.md` 留在 bundle 内（指向外部的
链接是 error）。

然后运行可复用的一致性检查路径：

```sh
jev-loop bundle validate project:<name>
jev-loop bundle conformance project:<name> --run-tests
```

`conformance` 合并三部分：静态契约、证明发现过程未导入也未改动任何东西的子进程探针，以及带超时执行的
你自己的测试。它的运行时语义部分始终是 `not_checked`——它也从不声称静态验证了 factory 形状、依赖或
凭据。conformance 自身会运行那一个隔离探针子进程（以及你要求时的作者测试）；在探针内部，发现与校验不会再
启动任何进程。

`--run-tests` 是**你自己的**代码在子进程里带超时执行：可信代码，不是网络沙箱，也不证明你的测试没有副作用。
请让测试在构造上保持离线。

`bundle validate` 与 `start` 是**同一个门**：error 级结果会在任何 run 目录、journal 或资源声明存在之前
拒绝 start，因此校验通过是对 start 的可靠预检。warning 与建议声明永远不会阻断 start。

`bundle init` 打印的后续命令与 bundle 实际落点一致：位于发现根内的按名称引用，位于项目内其他位置的按
显式路径引用，而创建在项目根之外的只给出作者测试命令加一条说明——因为信任根会拒绝在那里校验或启动它。

## 6. 收尾并真实运行一次

1. 只有在 controller 实现完成且测试通过后才把 `"scaffold": false`。在此之前 host 会拒绝按名称或路径
   启动（存在仅用于管路检查的显式 `allow_scaffold: true`，而那种运行不构成任何用户任务的证据）。
2. 通过协议真实运行一次并阅读证据：

```sh
printf '%s' '{"action":"start","owner_id":"author-check","idempotency_key":"first-run",
  "project_root":"'"$PWD"'","bundle":"project:<name>",
  "task":{"goal":"<目标>"},"max_runtime_seconds":120}' | jev-loop rpc
```

3. 轮询 `inspect`，应答所有 `pending_cognition`，然后 `stop`，并要求终态加上
   `resources_released: true`。verifier 的判定才是结果。

这次验收运行请先针对**隔离的合成/夹具环境 + fake requester**。触及 bundle 的真实环境——真实模型、付费
API、网站、设备——是另一个步骤，需要用户为**该次运行**给出明确的授权、范围与预算。凭据缺失或不可用时必须
在第一个动作前失败；绝不因此降级为 mock 或合成成功，也绝不把离线验收运行说成真实任务已完成。

## 7. 本仓中的完整示例

`.agents/jev-bundle/offline-switchboard/` 是一个完整、可发现、离线的参考实现：带完整元数据的 manifest
v2、`BUNDLE.md`、通过 `LoopController` 托管真实内核的 controller，以及在进程内运行内核的作者测试。

```sh
jev-loop bundle list
jev-loop bundle show project:offline-switchboard
jev-loop bundle conformance project:offline-switchboard --run-tests
```

它刻意不演练运行时 cognition：那是内建 `diagnostic` bundle 的职责，由 host 测试套件启动。把两类证据
分开是有意为之——简单的参考 bundle 应当保持简单。

## 8. 在检出之外工作

CLI、模板与两个 skill 都能从已安装包工作：

```sh
uv build                                  # 或 python -m build
python3 -m venv /tmp/jev-loop-venv
/tmp/jev-loop-venv/bin/pip install dist/jev_loop-*.whl
/tmp/jev-loop-venv/bin/jev-loop bundle init my-bundle --project-root /tmp/my-project
```

`scripts/wheel-smoke.sh` 完整跑一遍这条链路（构建、在检出外安装、init、validate、作者测试、
conformance），作为打包验收检查。

## 9. 常见错误

| 现象 | 原因 |
|---|---|
| `no bundle named '<x>'` | 目录不在 `<project_root>/.agents/jev-bundle/` 下，或其名称与 manifest 不一致 |
| `is not usable: missing BUNDLE.md` | `bundle.json` 旁缺少说明文件 |
| `schema_version 1 is the legacy manifest format` | 标准位置放了 v1 manifest；请按路径运行或迁移 |
| `is ambiguous` | 裸名称同时是相对文件；用 `project:<name>` 或 `./path/bundle.json` |
| `python_path ... outside the bundle directory` | bundle 从自身之外导入代码 |
| `declares scaffold=true and is refused by default` | 完成 bundle 后设置 `scaffold: false` |
| `task.inputs does not match the bundle's inputs_schema` | 调用方输入与声明契约不一致 |
| `declared schema ... not a supported schema` | 不支持的 keyword 或无法强制的形状；见规范中的封闭子集 |
| `unrendered_template_token` | 模板占位符未渲染；请重新运行 `bundle init`，不要就地编辑模板 |
