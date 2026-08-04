# AI Agent 与 CANape 联动

`Agent2Canape` 提供本地 stdio MCP Server，使 Codex、Claude Code 和其他 MCP 客户端能够用
自然语言规划 CANape 工程动作。核心原则是：AI 可以读取、分析和生成动作计划，但任何
项目控制、测量控制、标定写入、内存写入、诊断或刷写都不能绕过外部审批。

![AI 驱动 ECU 标定架构](./assets/ai-calibration-loop.svg)

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
.\.venv\Scripts\agent2canape-mcp.exe
```

MCP Server 使用 stdio，正常启动后不会向标准输出打印日志；MCP 协议消息由客户端管理。

安装后可先运行分层诊断：

```powershell
agent2canape mcp-doctor --project D:\CANapeProjects\Vehicle
agent2canape mcp-doctor --project D:\CANapeProjects\Vehicle --live-canape
```

第一条只检查依赖、环境、工程与客户端注册；第二条还会实际启动 CANape、读取工程信息
和设备数量后退出，不执行标定写入、诊断或刷写。输出为不包含令牌值的 JSON，适合
Codex、Claude Code 和 CI 收集。

## Codex

推荐用当前 Codex CLI 直接添加项目的本地 MCP Server：

```powershell
codex mcp add Agent2Canape `
  --env AGENT2CANAPE_APPROVAL_STORE=E:\secure\Agent2Canape-approvals.json `
  --env AGENT2CANAPE_DEFAULT_PROJECT=D:\CANapeProjects\Vehicle `
  --env AGENT2CANAPE_MCP_SESSION_ID=codex-vehicle-project `
  --env AGENT2CANAPE_MCP_LOCK_DIR=E:\secure\Agent2Canape-mcp-locks `
  --env AGENT2CANAPE_MCP_AUDIT_LOG=E:\secure\Agent2Canape-mcp-audit.jsonl `
  -- E:\path\to\Agent2Canape\.venv\Scripts\agent2canape-mcp.exe

codex mcp get Agent2Canape
```

也可将以下配置放在受信任项目的 `.codex/config.toml`，不要把机器私有路径写入公共模板：

```toml
[mcp_servers.Agent2Canape]
command = "E:/path/to/Agent2Canape/.venv/Scripts/agent2canape-mcp.exe"
env_vars = ["WINDIR", "SYSTEMROOT"]
startup_timeout_sec = 120.0
tool_timeout_sec = 120.0

[mcp_servers.Agent2Canape.env]
AGENT2CANAPE_APPROVAL_STORE = "E:/secure/Agent2Canape-approvals.json"
AGENT2CANAPE_DEFAULT_PROJECT = "D:/CANapeProjects/Vehicle"
AGENT2CANAPE_MCP_SESSION_ID = "codex-vehicle-project"
AGENT2CANAPE_MCP_LOCK_DIR = "E:/secure/Agent2Canape-mcp-locks"
AGENT2CANAPE_MCP_AUDIT_LOG = "E:/secure/Agent2Canape-mcp-audit.jsonl"
AGENT2CANAPE_MCP_RATE_LIMIT = "120"
```

CLI 格式以本机 `codex mcp add --help` 为准；项目内示例见
[`examples/codex.config.toml`](../examples/codex.config.toml)。

## Claude Code

Claude Code 支持项目级 `.mcp.json`。复制示例并替换路径：

```json
{
  "mcpServers": {
    "Agent2Canape": {
      "type": "stdio",
      "command": "E:/path/to/Agent2Canape/.venv/Scripts/agent2canape-mcp.exe",
      "args": [],
      "env": {
        "AGENT2CANAPE_APPROVAL_STORE": "E:/secure/Agent2Canape-approvals.json",
        "AGENT2CANAPE_DEFAULT_PROJECT": "D:/CANapeProjects/Vehicle",
        "AGENT2CANAPE_MCP_SESSION_ID": "claude-vehicle-project",
        "AGENT2CANAPE_MCP_LOCK_DIR": "E:/secure/Agent2Canape-mcp-locks",
        "AGENT2CANAPE_MCP_AUDIT_LOG": "E:/secure/Agent2Canape-mcp-audit.jsonl",
        "AGENT2CANAPE_MCP_RATE_LIMIT": "120",
        "SYSTEMROOT": "C:/Windows",
        "WINDIR": "C:/Windows"
      }
    }
  }
}
```

也可以使用 CLI：

```powershell
claude mcp add Agent2Canape `
  E:\path\to\Agent2Canape\.venv\Scripts\agent2canape-mcp.exe `
  --scope project `
  -e AGENT2CANAPE_APPROVAL_STORE=E:\secure\Agent2Canape-approvals.json `
  -e AGENT2CANAPE_DEFAULT_PROJECT=D:\CANapeProjects\Vehicle `
  -e AGENT2CANAPE_MCP_SESSION_ID=claude-vehicle-project `
  -e AGENT2CANAPE_MCP_LOCK_DIR=E:\secure\Agent2Canape-mcp-locks `
  -e AGENT2CANAPE_MCP_AUDIT_LOG=E:\secure\Agent2Canape-mcp-audit.jsonl `
  -e SYSTEMROOT=C:\Windows `
  -e WINDIR=C:\Windows

claude mcp get Agent2Canape
```

Claude Code 的项目级 MCP 配置格式参见
[Anthropic 官方 MCP 文档](https://docs.anthropic.com/en/docs/claude-code/mcp)。

`AGENT2CANAPE_DEFAULT_PROJECT` 是可选的受信任启动配置。设置后，MCP 只在第一个需要
CANape COM 的工具调用到来时懒加载该工程；单纯的工具发现和自然语言规划不会启动
CANape。CANape 17 不应在尚未加载工程时读取 `WorkingDirectory`，因此需要直接调用
`project_info`、设备或标定工具的客户端应配置默认工程，或者先完成 `project_open`
的外部审批流程。

Windows 上应确保 MCP 子进程继承 `WINDIR` 和 `SYSTEMROOT`。部分 AI 客户端会裁剪
stdio Server 环境；服务可以正常握手，但 CANape COM 冷启动会失败。CANape 冷启动也
可能超过客户端默认的30秒工具超时，因此 Codex 示例显式配置120秒。

对于使用 MCP 延迟工具发现不完整的第三方模型代理，或只希望向某个项目暴露最小权限
工具集，可配置逗号分隔的注册表工具名：

```text
AGENT2CANAPE_MCP_TOOL_ALLOWLIST=project_info,device_list,calibration_read
```

允许列表会同时限制 MCP 工具发现、工具清单和自然语言规划结果。未知工具名会让 Server
启动失败，避免拼写错误造成错误的权限预期。未设置时仍暴露完整工具集。

### 客户端显示 Connected 但模型不调用工具

`codex mcp get` 或 `claude mcp get` 成功只证明客户端完成了 MCP 握手，不等于当前模型
供应商已经支持工具调用。使用第三方 Claude Code API 代理时，还必须确认代理完整支持
`tool_use`；启用延迟发现时还需支持 `tool_reference`。若模型请求长时间无工具事件：

1. 先让同一 Claude Code 会话调用一次内置的安全工具，排除 Agent2Canape；
2. 使用 `claude --debug-file <path>` 检查 `ToolSearch` 和 `tool_use` 事件；
3. 若内置工具同样无法调用，应切换到支持 Claude Code 工具协议的模型端点；
4. 若只有延迟发现失败，可使用 `AGENT2CANAPE_MCP_TOOL_ALLOWLIST` 暴露较小工具集。

Codex 的 `PermissionRequest` Hook 和 Agent2Canape 自身的两阶段审批是两层独立门禁。
前者负责 AI 客户端工具授权，后者负责 CANape 工程动作授权；不应为验证连通性而全局
关闭任一门禁。

## 多客户端运行治理

Codex 与 Claude Code 会分别启动 stdio MCP Server 进程。两个客户端必须使用不同的
`AGENT2CANAPE_MCP_SESSION_ID`，但应指向相同的锁目录和审计文件。这样可以防止两个
AI 会话同时占用 CANape COM，并把调用轨迹汇入同一个本机 JSONL 日志。

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `AGENT2CANAPE_MCP_SESSION_ID` | 自动 UUID | 会话标识；仅允许字母、数字、点、下划线、冒号和连字符 |
| `AGENT2CANAPE_MCP_LOCK_DIR` | `~/.agent2canape/mcp-locks` | 多 MCP 进程共享的 CANape 资源租约目录 |
| `AGENT2CANAPE_MCP_AUDIT_LOG` | `~/.agent2canape/mcp-audit.jsonl` | 多进程安全追加的摘要审计文件 |
| `AGENT2CANAPE_MCP_RATE_LIMIT` | `120` | 每个 Server 进程每 60 秒允许的调用数；`0` 表示不限制 |
| `AGENT2CANAPE_MCP_LOCK_TIMEOUT` | `10` | 等待 CANape 资源租约的秒数 |
| `AGENT2CANAPE_MCP_LEASE_SECONDS` | `3600` | 无有效 PID 的异常租约兜底过期秒数 |

在线 CANape 工具进入全局 `canape-com` 租约后才会执行。持有进程退出后，下一调用可以
恢复陈旧锁；若租约缺少有效 PID，则按到期时间兜底恢复。存活进程即使执行超过租期也
不会被抢占。租约文件包含 PID、会话和到期时间，不包含工程参数或凭据。
自然语言规划及离线工具不占用 CANape 租约，但同样受会话限流和摘要审计约束。

审计记录只保存工具名、状态、耗时、错误类型，以及参数和结果的 SHA-256 摘要；不会
写入参数原文、工具结果或异常消息。审计目录仍应使用操作系统权限保护，不应提交到 Git。
调用 `agent2canape_runtime_status` 可读取当前会话、PID、活动/完成/失败/拒绝计数、
速率窗口、锁目录和审计文件位置。该状态查询自身不消耗限流配额。

## 自然语言到工程动作

### 工程对象上下文与单位换算

AI 不应仅凭相似名称猜测 ECU 标定对象。项目可维护 JSON 工程上下文，定义默认 ECU、
对象正式名称、别名、物理单位、范围和车型元数据。仓库示例：
[`examples/engineering_context.json`](../examples/engineering_context.json)。

```powershell
agent2canape context-validate examples\engineering_context.json
agent2canape ai-plan "把冷却液目标温度修改为 313.15 K" `
  --context-file examples\engineering_context.json `
  --reason "暖机响应优化"
```

规划器只执行白名单物理单位换算。未知单位、对象未声明目标单位、跨维度换算和上下文
范围越界都会直接返回错误；同一别名对应多个 ECU 时，必须在请求或上下文中明确 ECU。
上下文用于生成候选参数，不会自行执行 CANape 动作。
变更原因属于本次任务而不是车型知识，应通过 `--reason` 或 MCP `context.reason` 显式
提供并进入 Action Plan 审计，不会由规划器凭空生成。

MCP 调用可把同一 JSON 对象作为 `agent2canape_plan_natural_language` 的 `context` 参数。
例如 `313.15 K` 会确定性换算为对象单位中的 `40 °C`，并在结果中记录源单位、目标单位
和是否发生换算。

### 实时标定写入预览

`calibration_write` 的 Dry-run 不再只展示用户提交参数，而是实际执行只读操作获取当前
标定对象，并返回：

- 当前值、轴、单位、范围和对象类型；
- 目标值与标量百分比差异，或曲线/MAP 的变化点数和最大/平均绝对差；
- 写入前范围、维度和轴校验；
- 用于失败恢复的完整当前对象快照和回读要求；
- 绑定当前对象状态的 SHA-256 前置摘要。

该摘要属于审批计划的一部分。工程师批准后，如果对象被 CANape UI、另一 AI 会话或
其他标定终端修改，执行调用会在写入前失败，旧计划进入 `stale` 且不可再次使用，必须
重新读取、重新预览和重新审批。
升级前生成但尚未执行的标定写入计划不具备前置摘要，应重新生成。

示例请求：

```text
列出 VCU 中的标定量
读取 VCU 的 TorqueLimit，并告诉我范围、单位和当前值
将 TorqueCurve 的 X 轴改为 [0, 1000, 2000]，值改为 [80, 160, 220]
导出 BMS 的 SOC_Limit 与 ChargePowerLimit 形成标定基线
查看 P301 标定变更集的功能组、责任人、风险和审批状态
检查 VCU 的 Working、RAM、ROM 是否已经形成持久化基线
查看 VCU CAL-42 持久化作业的掉线协调和补偿状态
检查暖机 DOE 的失败 case 和证据完整性
按温度误差与能耗分析 Pareto 标定候选
生成暖机 DOE 的稳态、异常剔除和统计报告
根据历史实验和安全温度边界推荐下一组标定
启动测量
向 GW 发送诊断请求 22 F1 90
```

服务首先调用 `agent2canape_plan_natural_language`，返回候选工具、结构化参数、风险等级和
缺失参数。读取动作可以直接执行；非只读动作默认只返回 `Action Plan`。

## 两阶段审批协议

1. AI 使用完全确定的参数调用工具，保持 `dry_run=true`。
2. Server 返回 `action_plan.id`、风险等级、参数和 SHA-256 摘要。
3. 工程师在 MCP 之外审批：

   ```powershell
   agent2canape ai-approve <plan-id> --approver "calibration-engineer"
   ```

4. AI 使用完全相同的参数、`dry_run=false` 和 `action_plan_id` 再次调用。
5. Server 原子认领计划并执行；参数变化、过期、重复使用或并发重复认领都会被拒绝。
6. 执行失败的计划进入 `failed`，不会被自动重试，避免部分写入后重复操作。

审批文件不包含 API Key，但包含工程动作和参数，应放在受控目录，不要提交到 Git。

## 标定对象支持

`calibration_read` 和 `calibration_write` 支持：

- 标量、布尔、枚举、ASCII；
- 一维曲线及 X 轴；
- 二维 MAP 及 X/Y 轴；
- 单位、上下限、地址、转换规则和注释；
- 轴严格递增、维度匹配和范围校验；
- 值与轴的事务写入、回读验证和失败回滚。

多参数修改可先建立 `CalibrationDataset` 和 `CalibrationPlan`，完成差异预览、三方合并、
审批、事务提交和版本归档。AI/MCP 还可只读检查 `CalibrationChangeSet`、
`CalibrationMemoryLedger`、`CalibrationPersistenceJob` 和
`CalibrationExperimentStore`，并执行带安全边界的 Pareto 候选分析。DOE、OFAT、
Latin Hypercube 和坐标搜索可用于台架标定，每次实验后都会恢复基线。
`calibration_experiment_report` 汇总稳态、指标验收、异常剔除和证据；
`calibration_safe_suggest` 使用高斯过程置信边界推荐候选，但不会执行写入。

## 风险边界

| 风险等级 | 典型工具 | 执行条件 |
|---|---|---|
| `READ_ONLY` | 标定读取、评审/存储/DOE 报告、Pareto、安全候选、刷写状态 | 可直接执行 |
| `PROJECT_CONTROL` | 打开项目、设备上下线、网络配置 | 外部审批 |
| `MEASUREMENT_CONTROL` | 启停测量 | 外部审批 |
| `CALIBRATION_WRITE` | 标定量/曲线/MAP/数据集写入 | 外部审批、范围校验、回读 |
| `MEMORY_WRITE` | ECU 内存写入 | 外部审批、回读 |
| `DIAGNOSTIC` | 命名或原始诊断、Tester Present | 外部审批 |
| `FLASH` | 启停刷写 | 最高风险、外部审批 |

本工具包不会为 AI 暴露“自我审批”工具，也不会自动绕过车辆、台架、企业安全策略或
CANape 许可证限制。

RAM→ROM 动作不通过通用 MCP 工具猜测执行。项目应使用
`CANapeCalibrationTarget` 注入经过台架验收的 ROM 读取、持久化和恢复回调，再将该项目
动作注册为受 `CALIBRATION_WRITE` 或更高风险级别保护的工作流。
