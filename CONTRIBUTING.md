# 参与贡献

感谢参与 `py-canape`。本项目面向真实整车工程场景，提交内容应保持可追踪、可验证和
安全优先。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
```

## 提交前检查

```powershell
.\.venv\Scripts\ruff.exe check src tests scripts examples
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\py-canape.exe capabilities
```

## 贡献要求

- 新功能必须包含测试和用户可理解的文档；
- 不在核心包中写死车型、ECU、信号、DID、Routine 或 Seed/Key；
- 涉及标定、内存、下载、诊断和刷写的操作必须通过显式安全门禁；
- 可选第三方依赖缺失时应给出清晰提示，不得破坏基础包导入；
- 不提交真实车辆身份、企业凭据、许可证文件或受限数据库；
- 能力注册变更需要同步更新 `CAPABILITIES.md` 与自动化测试。

## Pull Request

请在 PR 中说明：

1. 解决的工程问题；
2. 实现方式和安全影响；
3. 自动化验证命令及结果；
4. 尚未完成的硬件或外部系统验证。
