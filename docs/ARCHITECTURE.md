# py-canape 工程平台架构

## 需求

### 功能

- 覆盖 `CAPABILITIES.md` 中 1—140 项能力；
- 同时支持在线 CANape COM、离线工程数据、自动分析和任务编排；
- 核心包不绑定车型、ECU、信号名、DID、Seed/Key 或企业系统；
- 所有写操作具备权限、范围、前置条件、审计和 Dry-run。

### 非功能

- Python 3.10+，Windows 上启用 CANape COM，离线模块可跨平台运行；
- 10 万行信号数据的常规分析应在秒级完成；
- 任一工作流步骤失败后不得静默继续危险写操作；
- 资产和审计记录使用 SHA-256，可复现、可追踪；
- 可选解析器缺失时给出明确安装提示，不影响基础包导入。

## 高层架构

```text
CLI / Python API
       |
       v
Workflow Engine ---- Safety Policy ---- Audit Trail
       |                    |
       +----------+---------+
                  |
        +---------+----------+----------------+
        |                    |                |
 CANape COM Core      Offline Data       Analysis Core
        |                    |                |
 ECU / CANape       MDF BLF A2L DBC   Evidence / Reports
```

## 模块职责

- `canape.py`：在线会话、设备、测量、标定、诊断、刷写和网络；
- `assets.py`：环境清单、工程资产、版本哈希、预检、快照和恢复；
- `offline.py`：数据格式适配、信号字典、重采样、时间对齐和导出；
- `analysis.py`：质量检查、状态机、因果链、控制指标和对比；
- `safety.py`：权限级别、对象/地址/数值白名单及车辆前置条件；
- `workflow.py`：YAML/JSON 场景、变量覆盖、重试、检查点和批量回归；
- `reporting.py`：不可变审计、证据包、Word/Excel/HTML 报告；
- `plugins.py`：领域包、外部问题单和企业安全实现的扩展协议；
- `capabilities.py`：140 项能力的机器可读注册与验收状态。

## 主要失效模式

| 失效 | 处理 |
|---|---|
| CANape/许可证/硬件不可用 | 预检失败并阻止在线步骤 |
| 可选解析器缺失 | 抛出带 extra 名称的依赖错误 |
| ECU 写入越界 | SafetyPolicy 在 COM 调用前拒绝 |
| 工作流中断 | 保存检查点；仅允许幂等或显式恢复步骤重试 |
| 报告生成失败 | 保留原始 JSON 审计和证据文件 |
| 企业系统不可用 | 外部适配器失败不影响本地证据包 |

