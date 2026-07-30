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

## Codex

推荐用当前 Codex CLI 直接添加项目的本地 MCP Server：

```powershell
codex mcp add Agent2Canape `
  --env AGENT2CANAPE_APPROVAL_STORE=E:\secure\Agent2Canape-approvals.json `
  --env AGENT2CANAPE_DEFAULT_PROJECT=D:\CANapeProjects\Vehicle `
  -- E:\path\to\Agent2Canape\.venv\Scripts\agent2canape-mcp.exe

codex mcp get Agent2Canape
```

也可将以下配置放在受信任项目的 `.codex/config.toml`，不要把机器私有路径写入公共模板：

```toml
[mcp_servers.Agent2Canape]
command = "E:/path/to/Agent2Canape/.venv/Scripts/agent2canape-mcp.exe"

[mcp_servers.Agent2Canape.env]
AGENT2CANAPE_APPROVAL_STORE = "E:/secure/Agent2Canape-approvals.json"
AGENT2CANAPE_DEFAULT_PROJECT = "D:/CANapeProjects/Vehicle"
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
        "AGENT2CANAPE_DEFAULT_PROJECT": "D:/CANapeProjects/Vehicle"
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
  -e AGENT2CANAPE_DEFAULT_PROJECT=D:\CANapeProjects\Vehicle

claude mcp get Agent2Canape
```

Claude Code 的项目级 MCP 配置格式参见
[Anthropic 官方 MCP 文档](https://docs.anthropic.com/en/docs/claude-code/mcp)。

`AGENT2CANAPE_DEFAULT_PROJECT` 是可选的受信任启动配置。设置后，MCP 只在第一个需要
CANape COM 的工具调用到来时懒加载该工程；单纯的工具发现和自然语言规划不会启动
CANape。CANape 17 不应在尚未加载工程时读取 `WorkingDirectory`，因此需要直接调用
`project_info`、设备或标定工具的客户端应配置默认工程，或者先完成 `project_open`
的外部审批流程。

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

## 自然语言到工程动作

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
