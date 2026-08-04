# Agent2Canape 工程平台架构

## 需求

### 功能

- 覆盖 `CAPABILITIES.md` 中 1—140 项能力；
- 同时支持在线 CANape COM、离线工程数据、自动分析和任务编排；
- 核心包不绑定车型、ECU、信号名、DID、Seed/Key 或企业系统；
- 自动编排写操作默认拒绝，必须显式放行；车型级权限、范围、前置条件和审计由
  `SafetyPolicy`、`SafeCANape`、工作流配置与企业适配器共同提供。

### 非功能

- Python 3.10+，Windows 上启用 CANape COM，离线模块可跨平台运行；
- 10 万行信号数据的常规分析应在秒级完成；
- 任一工作流步骤失败后不得静默继续危险写操作；
- 资产和审计记录使用 SHA-256，可复现、可追踪；
- 可选解析器缺失时给出明确安装提示，不影响基础包导入。

## 高层架构

```text
Codex / Claude Code / CLI / Python API
       |
       v
MCP + Action Plan --- External Approval
       |
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
- `calibration.py`：标定对象、数据集、版本、变更事务、实验设计和优化；
- `calibration_design.py`：二维/物理模型约束、DOE 质量、实验报告和安全高斯过程；
- `calibration_formats.py`：A2L 语义目录和 CDFX/DCM/PAR 标定数据交换；
- `calibration_operations.py`：变更评审、存储层台账、DOE 恢复、多 ECU 事务和 Pareto；
- `calibration_targets.py`：目标适配协议、RAM/ROM 持久化、掉线协调和分阶段多 ECU 补偿；
- `measurement.py`：测量清单、DAQ/FIFO 预算、事务快照、重连恢复和 MDF/MF4 验收；
- `streaming.py`：调用线程在线订阅、有界时间窗、采样质量、背压统计和可恢复证据分卷；
- `topology.py`：网络/设备/通道/数据库清单、资产哈希、DBC/A2L 语义和实时漂移审计；
- `ai_tools.py`：AI 工具 Schema、自然语言计划、跨进程审批和安全调度；
- `mcp_server.py`：Codex、Claude Code 等客户端使用的本地 stdio MCP Server；
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
| 工作流中断 | 保存检查点并逆序执行显式补偿；失败动作分类后停止 |
| 工作流未授权写入 | 默认拒绝 `write=True` 动作，需显式 `allow_writes=True` |
| AI 参数被篡改或重复执行 | SHA-256 绑定工具与参数，原子认领，计划只使用一次 |
| 曲线/MAP 轴或维度错误 | 写入前校验轴递增、矩阵形状和上下限 |
| MAP 局部尖峰或物理模型越界 | X/Y/对角邻域及项目派生指标在候选阶段拒绝 |
| DOE 环境未稳态或样本异常 | 写前稳态门禁、指标验收和队列级稳健异常判定 |
| 代理优化探索到未知危险区域 | 安全指标置信界、外推距离和已观测候选去重 |
| 写操作完成但响应前掉线 | 重连后回读目标层，协调已生效动作，不盲目重复写入 |
| 多 ECU ROM 阶段失败 | RAM 屏障、ROM 分阶段提交和跨设备逆序补偿 |
| 测量配置部分应用或设备掉线 | 快照回滚；重连后重建清单和原测量运行态 |
| DAQ 或触发窗口超预算 | 规划阶段拒绝，并只列出非必需信号的降级候选 |
| 录制文件缺失或证据不完整 | 大小、SHA-256、可选信号和时长验收失败 |
| 长时间在线观察导致内存或文件无界增长 | 样本数/时间双上限和字节分卷；达到分卷上限后停止 |
| COM 对象跨线程失效 | 订阅在调用线程同步轮询；后台任务必须拥有专属 COM 工作线程 |
| 流式进程异常退出 | `fsync` 加原子检查点；恢复时校验精确字节数，拒绝静默截断 |
| 错误网络、通道或数据库上执行工程任务 | 拓扑清单、资产哈希、数据库语义和 CANape 实时快照门禁 |
| COM 无法读取总线类型或波特率 | 明确保留为清单语义，不伪造实时字段；由 Vector 硬件配置补充验收 |
| 报告生成失败 | 保留原始 JSON 审计和证据文件 |
| 企业系统不可用 | 外部适配器失败不影响本地证据包 |
