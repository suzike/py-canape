"""对外返回的数据模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CANapeVersion:
    """CANape COM 接口返回的版本信息。"""

    main: int
    sub: int
    description: str
    release: int = 0

    def __str__(self) -> str:
        suffix = f" ({self.description})" if self.description else ""
        version = f"{self.main}.{self.sub}"
        if self.release:
            version = f"{version}.{self.release}"
        return f"{version}{suffix}"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """CANape 设备摘要。"""

    name: str
    driver_type: str
    channel: int
    online: bool
    database_filename: str


@dataclass(frozen=True, slots=True)
class RecorderInfo:
    """CANape 记录器摘要。"""

    name: str
    state: int
    recorder_type: int
    mdf_filename: str


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """ECU 测量任务摘要。"""

    name: str
    task_id: int
    event: int
    sampling_time: float
    fifo_level: int
    signal_count: int


@dataclass(frozen=True, slots=True)
class CalibrationObjectInfo:
    """标定对象的类型和维度信息。"""

    name: str
    object_type: int
    calibration_type: int
    representation_type: int
    x_dimension: int
    y_dimension: int


@dataclass(frozen=True, slots=True)
class DiagnosticResponse:
    """诊断请求响应摘要。"""

    positive: bool
    response_code: int
    sender: str
    stream: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FlashStateInfo:
    """CANape FlashManager 当前状态。"""

    busy: bool
    progress: int
    info: str
    has_return_value: bool
    return_value: object
