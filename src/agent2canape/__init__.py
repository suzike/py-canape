"""Vector CANape 的 Python COM 控制接口。"""

from .ai_tools import (
    AIActionPlan,
    AIToolRegistry,
    AIToolSpec,
    ApprovalStore,
    CANapeAIToolkit,
    EngineeringCommandPlanner,
    ToolRisk,
)
from .analysis import Finding, SignalAnalyzer
from .assets import AssetManager, AssetRecord, PreflightResult
from .calibration import (
    CalibrationChange,
    CalibrationDataset,
    CalibrationExperiment,
    CalibrationKind,
    CalibrationMath,
    CalibrationOptimizer,
    CalibrationParameter,
    CalibrationPlan,
    CalibrationRepository,
    CalibrationSession,
    SweepParameter,
)
from .canape import CANape
from .capabilities import Capability, CapabilityRegistry
from .errors import (
    AssetValidationError,
    CANapeCOMError,
    CANapeError,
    CANapeNotConnectedError,
    CANapeObjectNotFoundError,
    CANapeProjectError,
    OptionalDependencyError,
    SafetyViolationError,
    WorkflowError,
)
from .models import (
    CalibrationObjectInfo,
    CANapeVersion,
    DeviceInfo,
    DiagnosticResponse,
    FlashStateInfo,
    RecorderInfo,
    TaskInfo,
)
from .offline import OfflineData, SignalDefinition, SignalDictionary
from .platform import EngineeringPlatform
from .plugins import BasicDomainAdapter, PluginRegistry, built_in_domains
from .reporting import AuditEntry, AuditTrail, Reporter
from .safety import (
    EnvironmentSecretProvider,
    PermissionLevel,
    SafeCANape,
    SafetyPolicy,
    ValueRule,
)
from .workflow import StepResult, WorkflowEngine, WorkflowResult

__all__ = [
    "CANape",
    "ToolRisk",
    "AIToolSpec",
    "AIActionPlan",
    "ApprovalStore",
    "AIToolRegistry",
    "EngineeringCommandPlanner",
    "CANapeAIToolkit",
    "CalibrationKind",
    "CalibrationParameter",
    "CalibrationDataset",
    "CalibrationRepository",
    "CalibrationChange",
    "CalibrationPlan",
    "CalibrationSession",
    "CalibrationExperiment",
    "CalibrationOptimizer",
    "CalibrationMath",
    "SweepParameter",
    "AssetManager",
    "AssetRecord",
    "PreflightResult",
    "SignalAnalyzer",
    "Finding",
    "OfflineData",
    "SignalDefinition",
    "SignalDictionary",
    "EngineeringPlatform",
    "Capability",
    "CapabilityRegistry",
    "PermissionLevel",
    "SafetyPolicy",
    "ValueRule",
    "SafeCANape",
    "EnvironmentSecretProvider",
    "WorkflowEngine",
    "WorkflowResult",
    "StepResult",
    "AuditEntry",
    "AuditTrail",
    "Reporter",
    "PluginRegistry",
    "BasicDomainAdapter",
    "built_in_domains",
    "CANapeCOMError",
    "CANapeError",
    "CANapeNotConnectedError",
    "CANapeObjectNotFoundError",
    "CANapeProjectError",
    "OptionalDependencyError",
    "SafetyViolationError",
    "WorkflowError",
    "AssetValidationError",
    "CANapeVersion",
    "CalibrationObjectInfo",
    "DeviceInfo",
    "DiagnosticResponse",
    "FlashStateInfo",
    "RecorderInfo",
    "TaskInfo",
]

__version__ = "3.0.0"
