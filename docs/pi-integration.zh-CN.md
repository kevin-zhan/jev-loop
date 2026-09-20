# pi-jev：受管理的 Jev Loop 宿主

[English](pi-integration.md) | **简体中文**

`pi-jev` 是本仓随附的 pi package，不是 pi fork。它把项目内行为 bundle 作为独立进程运行，让 pi 管理
生命周期并处理运行时认知请求；控制循环不等待主 agent 的一次回复。

```text
pi session
  ├─ jev_loop tool / slash commands
  ├─ heartbeat + cognition delivery
  └─ bounded cognition result
                ↕ JSON over stdin for each management request
managed host (one worker process per run)
  ├─ append-only events.jsonl
  ├─ lease / max runtime / exclusive resource claims
  ├─ cognition broker
  └─ trusted project bundle → controller → Loop/environment/driver
```

## 安装与试用

要求 Python 3.12+ 和 pi（pi 不由本仓安装；请按 pi 的官方安装说明安装，见
[pi 仓库](https://github.com/earendil-works/pi)）。扩展默认调用 `python3`，可用 `JEV_LOOP_PYTHON` 指向另一个解释器。
Python 源码通过受控的 `PYTHONPATH` 传给子进程，不需要把本包安装到全局环境。

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # 私有仓库：需要访问权限
cd jev-loop
pi install .            # 把本地 package 注册进 pi（就地引用这个克隆目录）
# 已打开的 pi 会话执行 /reload；或重启 pi
```

可选的远端非钉版本安装（仓库私有，仍需 GitHub 访问权限）：

```sh
pi install git:github.com/kevin-zhan/jev-loop
pi -e https://github.com/kevin-zhan/jev-loop   # 临时试用，不写设置
```

安装后提供：

- `jev_loop` 工具：`start / list / inspect / events / update / respond / stop / release_resources`；
  `events` 每页最多 500 条并返回 `next_seq`；
- `/jev-runs`：显示当前 session 拥有的 run；
- `/jev-self-test`：离线跑一遍真实进程验收（认知等待时继续推进、结束后释放输入）；
- `/jev-stop-all`：交互确认后停止当前 session 的所有活动 run；
- `pi-jev` skill：教 agent 何时使用、怎样处理 cognition 和怎样验收。

## Bundle manifest

扩展只接受内建 `diagnostic` 或当前可信项目目录内的 manifest。manifest 会执行 Python 代码，因此它是
明确的**项目代码信任边界**，不是远端提示词。

```json
{
  "schema_version": 1,
  "entrypoint": "controller:build",
  "python_path": ".",
  "config": {"adapter": "project-specific settings"}
}
```

`entrypoint` 的 factory 签名是：

```python
def build(spec: RunSpec, services: RuntimeServices, config: dict) -> ManagedController:
    ...
```

controller 的四个方法：

```python
class ManagedController(Protocol):
    def run(self, stop_event: threading.Event) -> ControllerResult: ...
    def snapshot(self) -> Mapping[str, Json]: ...
    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None: ...
    def request_stop(self, reason: str) -> None: ...
```

- `run` 在 engine thread 执行行为循环；返回的 `ControllerResult.resources_released` 只有在 controller
  已确认所有外部输入释放时才能为 `true`；强制杀死子进程等不确定路径必须返回 `false`；
- 另外三个方法可能由 coordinator thread 调用，必须线程安全；
- `request_stop` 必须同步释放 held inputs，不得等待模型或网络；
- 外部设备还应有独立 watchdog，因为 Python 进程被 `SIGKILL` 时任何 cleanup 都不保证执行；
- `snapshot` 只放有界、可 JSON 序列化的状态摘要，完整证据写入 run 的 `artifacts/`。

已有 `Loop` 可直接用 `LoopController` 托管，参考
[`examples/bundles/switchboard`](../examples/bundles/switchboard)。同步 `Loop.step()` 仍然是一次一个动作；
如果环境需要在慢模型调用期间持续推进，设备 driver/watchdog 必须在独立线程或进程运行，controller 只
协调它。

## 运行中认知

bundle 通过 broker 建立 job，调用立即返回：

```python
job_id = services.cognition.request(
    "已有候选偏向吃喝，下一轮应补哪些搜索方向？",
    context={"candidate_summary": summary},
    output_schema={"type": "object", "required": ["search_terms"]},
    resource_keys=("research-plan",),
    deadline_seconds=90,
    dedupe_key="diversify-search",
)
```

controller 可继续运行，并通过 `services.cognition.get(job_id)` 检查状态。pi 扩展把 pending job 作为明确
标注的 custom message 交给 owner session；主 agent 用 `jev_loop respond` 提交匹配 run/job/version 的
结果。迟到、重复、版本不匹配、结构不符或 run 已停止的结果会被拒绝。

当前 dependency-free validator 只实现下列关键字的**有限语义**，bundle 不应假设其他关键字已执行，也
不应把这些当作完整的 Draft 2020-12：

| 关键字 | 实际行为与限制 |
|---|---|
| `type` | `null / boolean / integer / number / string / array / object`；`integer`、`number` 都不接受 `bool` |
| `enum` / `const` | 用 Python 相等比较，因此 `1` 与 `true` 不区分；`const` 缺省时不检查 |
| `required` / `properties` | 只在值是对象时生效；`properties` 里的子 schema 递归校验 |
| `additionalProperties` | **只识别字面 `false`**（多余字段即拒绝）；写成子 schema 对象形式**不会被执行** |
| `items` / `minItems` / `maxItems` | 只在值是数组时生效 |
| `minLength` / `maxLength` | 只在值是字符串时生效 |
| `minimum` / `maximum` | 只对非 `bool` 数字生效；空 schema `{}` 直接放行 |

每个 run 最多同时有 8 个 pending job，question/context/schema/result/evidence 也分别有硬大小上限。
结果只进入 broker；bundle 仍需校验其世界前提与资源版本后才能采用。

内建 `diagnostic` 是对此语义的真实探针：它在 cognition pending 时维护 `forward` 输入，同时独立增加
world tick 和 decision 计数；收到结果或任何停止信号后释放输入。它不访问网页，也不能作为用户任务完成
证据。

## 生命周期与安全语义

- **Start 幂等**：`owner session + project root + idempotency key` 唯一映射到一个 run；重复调用不会再
  启动控制器。
- **独占资源**：`resourceKeys` 在 host 内原子声明，例如 `browser:research-profile`；活动 run 间不能重复
  占有。
- **Session owner**：管理命令和 cognition reply 必须匹配创建 run 的 pi session ID。切换到另一 session
  不会继承控制权。
- **租约**：非 detached run 默认 30 秒；扩展约每 3 秒 heartbeat。pi 消失或切换 session 后，worker 请求
  停止并释放输入。
- **Detached**：只有 TUI 中直接确认才能创建，且仍受 `maxRuntimeSeconds` 约束。
- **Stop 两阶段**：`accepted` 仅表示 worker 收到停止意图；terminal status 加 `resources_released=true`
  才能证明 host 收到了 controller 的同步释放确认。`confirmed` 只表示已等到 terminal。
- **超时**：run 有硬最大运行时间，cognition job 有各自 deadline；二者都不会无限等待。
- **单写所有权**：agent 不应在 run 活动时直接操作同一个 `resourceKeys`。pi-jev 只能约束经 host 注册的
  run，不能阻止外部程序绕过它。
- **崩溃隔离**：worker 消失且无法确认 release 时，资源 claim 保持 quarantined，不会被下一个 run 自动
  抢走。只有用户在 TUI 中独立核实设备输入已释放后，才能用 `release_resources` 清除 claim；进程外
  watchdog 仍是第一道保护。
- **会话历史不是运行真相**：事件日志是 host 状态的来源，pi session 只保存 delivery cursor。压缩、
  reload 不重放动作。

默认数据目录为 `~/.local/state/jev-loop/`，可通过 `JEV_LOOP_HOME` 改写。每个 run 包含：

```text
runs/<run-id>/
  spec.json          # 0600
  events.jsonl       # append-only source of truth
  status.json        # atomic projection/cache
  worker.log
  inbox/ acks/       # atomic command exchange
  artifacts/         # bundle-owned evidence
```

任务内容通过 management process 的 stdin 发送，不放进 shell 参数。目录和状态文件按用户私有权限创建；
bundle 自己仍须避免把 cookie、token 或原始隐私资料写入事件和 snapshot。

## 当前明确不包含

- 没有小红书、浏览器、手机或游戏站点适配器；它们必须是单独审查和验收的 project bundle。
- 没有把主 agent 变成按钮决策者；按钮候选仍由 bundle 的 Jev policy 决定。
- 没有 MCP server；需要跨宿主时可在相同 host API 外包一层适配器。
- 没有保证任意第三方 driver 能安全停车；bundle 必须实现同步 release 和设备侧 watchdog。
- 没有在离线测试中调用 TypeSafe 或其他付费模型。

## 验证

```sh
uv sync
uv run pytest
uv run ruff check .
npm test
node --experimental-strip-types --check extensions/pi-jev.ts
```

测试覆盖：认知等待期间继续推进、回复后完成、停止释放输入、owner lease 到期、start 幂等、版本化更新、
迟到回复拒绝、资源独占与崩溃 quarantine、项目 bundle 加载，以及用 pi RPC 实际加载 package 并执行
`/jev-runs` 和 `/jev-self-test`（不调用模型）。
