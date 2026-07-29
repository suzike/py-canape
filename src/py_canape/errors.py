"""py-canape 异常类型。"""


class CANapeError(RuntimeError):
    """py-canape 的基础异常。"""


class CANapeNotConnectedError(CANapeError):
    """尚未创建 CANape COM 连接。"""


class CANapeProjectError(CANapeError):
    """CANape 项目路径或项目加载失败。"""


class CANapeObjectNotFoundError(CANapeError):
    """未找到指定的 CANape 对象。"""


class CANapeCOMError(CANapeError):
    """CANape COM 调用失败。"""


class OptionalDependencyError(CANapeError):
    """当前文件格式或报告能力需要可选依赖。"""


class SafetyViolationError(CANapeError):
    """安全策略拒绝了危险操作。"""


class WorkflowError(CANapeError):
    """工程工作流定义或执行失败。"""


class AssetValidationError(CANapeError):
    """工程资产或预检验证失败。"""
