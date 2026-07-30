"""参考 py-canoe 风格实现的 CANape COM 控制入口。"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .errors import (
    CANapeCOMError,
    CANapeNotConnectedError,
    CANapeObjectNotFoundError,
    CANapeProjectError,
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

LOGGER = logging.getLogger("agent2canape")
_MISSING = object()


def _collection_item(collection: Any, key: str | int) -> Any:
    """兼容 COM 集合的 Item(key) 与 Item[key] 两种调用形式。"""
    try:
        return collection.Item(key)
    except TypeError:
        return collection.Item[key]


def _iter_collection(collection: Any) -> Iterator[Any]:
    """CANape COM 集合使用从 1 开始的索引。"""
    for index in range(1, int(collection.Count) + 1):
        yield _collection_item(collection, index)


def _as_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


class CANape:
    """Vector CANape COM 自动化封装。

    默认使用晚绑定 COM，避免 CANape 升级后遗留的 ``gen_py`` 缓存影响调用。
    类的方法名称尽量与 py-canoe 保持一致，便于在两套 Vector 工具之间切换。
    """

    PROG_ID = "CANape.Application"
    APPLICATION_TYPE_CANAPE = 1

    def __init__(self, *, com_factory: Any | None = None) -> None:
        self.application: Any | None = None
        self._com_factory = com_factory
        self._pythoncom: Any | None = None
        self._com_initialized = False
        self._project_open = False
        self._owns_application = False

    def __enter__(self) -> CANape:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        return self.application is not None

    def _initialize_com(self) -> None:
        if self._com_initialized:
            return
        if self._com_factory is not None:
            self._com_initialized = True
            return
        if os.name != "nt":
            raise CANapeCOMError("CANape COM 接口只能在 Windows 上使用")
        try:
            import pythoncom

            pythoncom.CoInitialize()
            self._pythoncom = pythoncom
            self._com_initialized = True
        except Exception as exc:  # pragma: no cover - 依赖 Windows COM 环境
            raise CANapeCOMError(f"初始化 Windows COM 失败：{exc}") from exc

    def connect(self) -> bool:
        """创建 CANape Application COM 对象，但不加载项目。"""
        if self.application is not None:
            return True
        self._initialize_com()
        try:
            if self._com_factory is not None:
                self.application = self._com_factory()
                self._project_open = bool(
                    str(getattr(self.application, "WorkingDirectory", ""))
                )
            else:
                import win32com.client

                self.application = win32com.client.Dispatch(self.PROG_ID)
            self._owns_application = True
            return True
        except Exception as exc:
            self.disconnect()
            raise CANapeCOMError(f"连接 {self.PROG_ID} 失败：{exc}") from exc

    def attach_to_active_application(self) -> bool:
        """连接 ROT 中已注册的 CANape 实例。

        CANape 17 在部分启动模式下不会注册到 Running Object Table。遇到这种
        情况应改用 :meth:`connect` 或 :meth:`open`。
        """
        self._initialize_com()
        if self._com_factory is not None:
            self.application = self._com_factory()
            self._project_open = bool(
                str(getattr(self.application, "WorkingDirectory", ""))
            )
            self._owns_application = False
            return True
        try:
            import win32com.client

            self.application = win32com.client.GetActiveObject(self.PROG_ID)
            self._project_open = bool(
                str(getattr(self.application, "WorkingDirectory", ""))
            )
            self._owns_application = False
            return True
        except Exception as exc:
            self.application = None
            raise CANapeCOMError(f"未找到可连接的 CANape 活动实例：{exc}") from exc

    def open(
        self,
        project: str | os.PathLike[str],
        *,
        visible: bool = True,
        timeout_ms: int = 30_000,
        clear_device_list: bool = False,
        non_modal: bool = True,
    ) -> bool:
        """打开 CANape 项目目录或指定的 ``.cna`` 文件。

        Python 参数 ``non_modal=True`` 会转换为 COM ``modalmode=0``。
        CANape COM 的第五个参数实际表示“模态模式”，传 ``1`` 会让自动化调用
        一直阻塞到 CANape 关闭。
        """
        project_path = _as_path(project)
        if not project_path.exists():
            raise CANapeProjectError(f"CANape 项目不存在：{project_path}")
        if project_path.is_file() and project_path.suffix.lower() != ".cna":
            raise CANapeProjectError("project 必须是项目目录或 .cna 文件")

        work_dir = project_path.parent if project_path.is_file() else project_path
        self.connect()
        try:
            self.application.Open2(
                str(work_dir),
                int(visible),
                int(timeout_ms),
                bool(clear_device_list),
                not bool(non_modal),
                self.APPLICATION_TYPE_CANAPE,
            )
            if project_path.is_file():
                loaded_name = Path(str(getattr(self.application, "CNAFilename", ""))).name
                if loaded_name.casefold() != project_path.name.casefold():
                    self.application.LoadCNAFile(str(project_path))
            self._project_open = True
            return True
        except Exception as exc:
            if self._owns_application and self.application is not None:
                try:
                    self.application.QuitNonModal()
                except Exception:
                    LOGGER.debug(
                        "打开项目失败后关闭 CANape 实例失败", exc_info=True
                    )
            self.disconnect()
            raise CANapeProjectError(
                f"打开 CANape 项目失败：{project_path}；COM 返回：{exc}"
            ) from exc

    def disconnect(self) -> None:
        """释放 Python 侧 COM 引用，不关闭 CANape。"""
        self.application = None
        self._project_open = False
        self._owns_application = False
        self._uninitialize_com()

    def _uninitialize_com(self) -> None:
        if self._pythoncom is not None and self._com_initialized:
            try:
                self._pythoncom.CoUninitialize()
            finally:
                self._pythoncom = None
                self._com_initialized = False

    def quit(self, *, non_modal: bool = False) -> bool:
        """关闭由当前对象控制的 CANape 实例。"""
        app = self._require_application()
        try:
            if non_modal and hasattr(app, "QuitNonModal"):
                app.QuitNonModal()
            else:
                app.Quit()
            return True
        except Exception as exc:
            raise CANapeCOMError(f"关闭 CANape 失败：{exc}") from exc
        finally:
            self.application = None
            self._project_open = False
            self._owns_application = False
            del app
            self._uninitialize_com()

    def _require_application(self) -> Any:
        if self.application is None:
            raise CANapeNotConnectedError("请先调用 connect() 或 open()")
        return self.application

    def get_com_api_version_info(self) -> CANapeVersion:
        """返回 CANape COM 自动化接口版本，例如 2.3。"""
        version = self._require_application().Version
        return CANapeVersion(
            main=int(version.Main),
            sub=int(version.Sub),
            description=str(version.Description),
        )

    def get_canape_version_info(self) -> CANapeVersion:
        """返回 CANape 产品版本；必须先打开项目。

        CANape 17 的 ``APPVersion`` 在应用尚未完成 Open2 时不安全，因此这里
        显式门禁，避免把 COM API 版本 2.3 误报为产品版本。
        """
        self._require_application()
        if not self._project_open:
            raise CANapeProjectError("获取 CANape 产品版本前必须先打开项目")
        version = self.application.APPVersion
        return CANapeVersion(
            main=int(version.Main),
            sub=int(version.Sub),
            release=int(version.Release),
            description=str(version.Description),
        )

    def get_project_info(self) -> dict[str, str]:
        app = self._require_application()
        return {
            "working_directory": str(app.WorkingDirectory),
            "cna_filename": str(app.CNAFilename),
            "application_name": str(app.Name),
        }

    def load_cna_file(self, cna_file: str | os.PathLike[str]) -> None:
        """在已打开的工作目录中加载指定 CNA。"""
        path = _as_path(cna_file)
        if not path.is_file() or path.suffix.lower() != ".cna":
            raise CANapeProjectError(f"CNA 文件不存在或扩展名错误：{path}")
        try:
            self._require_application().LoadCNAFile(str(path))
            self._project_open = True
        except Exception as exc:
            raise CANapeProjectError(f"加载 CNA 失败：{path}；{exc}") from exc

    def show_debug_window(self) -> None:
        self._require_application().DebugWindow()

    def save_debug_window(self, output_file: str | os.PathLike[str]) -> None:
        output = _as_path(output_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._require_application().SaveDebugWindow(str(output))

    def get_application_exception(self) -> Any:
        """返回 CANape 最近一次异常对象，保留原始 COM 信息。"""
        return self._require_application().Exception

    def convert_mdf_to_matlab(
        self,
        mdf_file: str | os.PathLike[str],
        matlab_file: str | os.PathLike[str],
        *,
        asynchronous: bool = False,
    ) -> None:
        source = _as_path(mdf_file)
        target = _as_path(matlab_file)
        if not source.is_file():
            raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._require_application().Convert2Matlab(
                str(source), str(target), bool(asynchronous)
            )
        except Exception as exc:
            raise CANapeCOMError(f"MDF 转 MATLAB 失败：{exc}") from exc

    def run_script(self, script: str | os.PathLike[str]) -> None:
        """运行 CANape ``.cns`` 脚本。"""
        script_path = _as_path(script)
        if not script_path.is_file():
            raise FileNotFoundError(script_path)
        try:
            self._require_application().RunScript(str(script_path))
        except Exception as exc:
            raise CANapeCOMError(f"运行 CANape 脚本失败：{script_path}；{exc}") from exc

    # 测量控制
    def start_measurement(self, *, wait_until_running: float = 5.0) -> bool:
        measurement = self._require_application().Measurement
        if bool(measurement.Running):
            return True
        try:
            measurement.Start()
            if not self._wait_for(
                lambda: bool(measurement.Running), wait_until_running
            ):
                raise CANapeCOMError(
                    f"CANape 测量未在 {wait_until_running:.3f}s 内进入运行态"
                )
            return True
        except CANapeCOMError:
            raise
        except Exception as exc:
            raise CANapeCOMError(f"启动 CANape 测量失败：{exc}") from exc

    def stop_measurement(self, *, wait_until_stopped: float = 5.0) -> bool:
        measurement = self._require_application().Measurement
        if not bool(measurement.Running):
            return True
        try:
            measurement.Stop()
            if not self._wait_for(
                lambda: not bool(measurement.Running), wait_until_stopped
            ):
                raise CANapeCOMError(
                    f"CANape 测量未在 {wait_until_stopped:.3f}s 内停止"
                )
            return True
        except CANapeCOMError:
            raise
        except Exception as exc:
            raise CANapeCOMError(f"停止 CANape 测量失败：{exc}") from exc

    def is_measurement_running(self) -> bool:
        return bool(self._require_application().Measurement.Running)

    def get_measurement_state(self) -> int:
        return int(self._require_application().Measurement.MeasurementState)

    def set_measurement_output_file(self, path: str | os.PathLike[str]) -> None:
        output = _as_path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._require_application().Measurement.MDFFilename = str(output)

    def get_measurement_output_file(self) -> str:
        return str(self._require_application().Measurement.MDFFilename)

    def configure_measurement(
        self,
        *,
        sample_size: int | None = None,
        fifo_size: int | None = None,
        sync_mode: bool | None = None,
        resume_mode: bool | None = None,
        use_nan: bool | None = None,
    ) -> None:
        """配置 CANape API 测量缓冲及同步策略。"""
        if sample_size is not None and sample_size <= 0:
            raise ValueError("sample_size 必须大于 0")
        if fifo_size is not None and fifo_size <= 0:
            raise ValueError("fifo_size 必须大于 0")
        measurement = self._require_application().Measurement
        values = {
            "SampleSize": sample_size,
            "FifoSize": fifo_size,
            "SyncMode": sync_mode,
            "ResumeMode": resume_mode,
            "UseNAN": use_nan,
        }
        try:
            for property_name, value in values.items():
                if value is not None:
                    setattr(measurement, property_name, value)
        except Exception as exc:
            raise CANapeCOMError(f"配置测量参数失败：{exc}") from exc

    def get_measurement_configuration(self) -> dict[str, Any]:
        measurement = self._require_application().Measurement
        return {
            "sample_size": int(measurement.SampleSize),
            "fifo_size": int(measurement.FifoSize),
            "sync_mode": bool(measurement.SyncMode),
            "resume_mode": bool(measurement.ResumeMode),
            "use_nan": bool(measurement.UseNAN),
        }

    # 设备控制
    def list_devices(self) -> list[DeviceInfo]:
        devices = _iter_collection(self._require_application().Devices)
        return [self._device_info(device) for device in devices]

    def get_device(self, device: str | int) -> Any:
        try:
            return _collection_item(self._require_application().Devices, device)
        except Exception as exc:
            raise CANapeObjectNotFoundError(f"未找到设备：{device}") from exc

    def add_device(
        self,
        name: str,
        database_file: str | os.PathLike[str],
        *,
        driver_type: str = "XCP",
        channel: int = 1,
        online: bool = False,
    ) -> Any:
        database = _as_path(database_file)
        if not database.is_file():
            raise FileNotFoundError(database)
        try:
            return self._require_application().Devices.Add2(
                name, str(database), driver_type, int(channel), bool(online)
            )
        except Exception as exc:
            raise CANapeCOMError(f"添加 CANape 设备 {name} 失败：{exc}") from exc

    def remove_device(self, device: str | int) -> None:
        try:
            self._require_application().Devices.Remove(device)
        except Exception as exc:
            raise CANapeCOMError(f"移除 CANape 设备 {device} 失败：{exc}") from exc

    def clear_devices(self) -> None:
        """释放 API 设备集合；项目中持久化设备不会因此被删除。"""
        self._require_application().Devices.Clear()

    def release_device(self, device: str | int) -> None:
        devices = self._require_application().Devices
        try:
            if isinstance(device, str):
                devices.ReleaseModuleByName(device)
            else:
                devices.ReleaseModuleByIndex(int(device))
        except Exception as exc:
            raise CANapeCOMError(f"释放设备 {device} 失败：{exc}") from exc

    def set_device_online(self, device: str | int, *, download: bool = False) -> None:
        target = self.get_device(device)
        try:
            target.GoOnline(bool(download))
        except Exception as exc:
            raise CANapeCOMError(f"设备 {device} 上线失败：{exc}") from exc

    def set_device_offline(self, device: str | int) -> None:
        target = self.get_device(device)
        try:
            target.GoOffline()
        except Exception as exc:
            raise CANapeCOMError(f"设备 {device} 离线失败：{exc}") from exc

    def is_device_online(self, device: str | int) -> bool:
        return bool(self.get_device(device).IsOnline)

    def reconnect_device(
        self,
        device: str | int,
        *,
        download: bool = False,
        restore_measurement: bool = True,
    ) -> bool:
        """重连设备，并在需要时恢复原测量运行态。"""
        was_running = self.is_measurement_running()
        if was_running:
            self.stop_measurement()
        self.set_device_offline(device)
        self.set_device_online(device, download=download)
        if restore_measurement and was_running:
            self.start_measurement()
        return self.is_device_online(device)

    def read_memory(
        self,
        device: str | int,
        address: int,
        size: int,
        *,
        address_extension: int | None = None,
    ) -> tuple[int, ...]:
        if address < 0 or size <= 0:
            raise ValueError("address 必须非负，size 必须大于 0")
        target = self.get_device(device)
        try:
            if address_extension is None:
                value = target.ReadMemory(int(address), int(size))
            else:
                value = target.ReadMemoryExt(
                    int(address), int(address_extension), int(size)
                )
            return tuple(int(item) for item in value)
        except Exception as exc:
            raise CANapeCOMError(f"读取设备 {device} 内存失败：{exc}") from exc

    def write_memory(
        self,
        device: str | int,
        address: int,
        data: Sequence[int],
        *,
        address_extension: int | None = None,
        verify: bool = True,
    ) -> tuple[int, ...]:
        """显式写内存，默认按相同地址回读验证。"""
        payload = tuple(int(item) for item in data)
        if address < 0 or not payload or any(item < 0 or item > 255 for item in payload):
            raise ValueError("address 必须非负，data 必须是非空 0~255 字节序列")
        target = self.get_device(device)
        try:
            if address_extension is None:
                target.WriteMemory(int(address), payload)
            else:
                target.WriteMemoryExt(int(address), int(address_extension), payload)
            if not verify:
                return payload
            actual = self.read_memory(
                device,
                address,
                len(payload),
                address_extension=address_extension,
            )
            if actual != payload:
                raise CANapeCOMError(
                    f"内存写入回读不一致：写入 {payload!r}，回读 {actual!r}"
                )
            return actual
        except CANapeCOMError:
            raise
        except Exception as exc:
            raise CANapeCOMError(f"写设备 {device} 内存失败：{exc}") from exc

    def upload_device_data(self, device: str | int, output_file: str | os.PathLike[str]) -> None:
        output = _as_path(output_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.get_device(device).Upload(str(output))

    def download_device_data(self, device: str | int, input_file: str | os.PathLike[str]) -> None:
        source = _as_path(input_file)
        if not source.is_file():
            raise FileNotFoundError(source)
        self.get_device(device).Download(str(source))

    def transfer_device_data(
        self,
        device: str | int,
        path: str | os.PathLike[str],
        *,
        direction: str,
    ) -> None:
        if direction == "upload":
            self.upload_device_data(device, path)
        elif direction == "download":
            self.download_device_data(device, path)
        else:
            raise ValueError("direction 必须是 upload 或 download")

    def send_device_telegram(
        self, device: str | int, request: Sequence[int], *, response_time_ms: int = 1000
    ) -> tuple[int, ...]:
        payload = tuple(int(item) for item in request)
        if not payload or any(item < 0 or item > 255 for item in payload):
            raise ValueError("request 必须是非空 0~255 字节序列")
        try:
            response = self.get_device(device).Telegramm(payload, int(response_time_ms))
            return tuple(int(item) for item in response)
        except Exception as exc:
            raise CANapeCOMError(f"设备 Telegram 发送失败：{exc}") from exc

    def run_device_script(self, device: str | int, script: str | os.PathLike[str]) -> None:
        path = _as_path(script)
        if not path.is_file():
            raise FileNotFoundError(path)
        self.get_device(device).RunScript(str(path))

    def list_device_databases(self, device: str | int) -> list[str]:
        databases = self.get_device(device).Databases
        return [str(database.Name) for database in _iter_collection(databases)]

    # 测量任务与信号
    def list_tasks(self, device: str | int) -> list[str]:
        tasks = self.get_device(device).Tasks
        return [str(task.Name) for task in _iter_collection(tasks)]

    def get_task_info(self, device: str | int, task: str | int) -> TaskInfo:
        target = _collection_item(self.get_device(device).Tasks, task)
        return TaskInfo(
            name=str(target.Name),
            task_id=int(target.ID),
            event=int(target.Event),
            sampling_time=float(target.SamplingTime),
            fifo_level=int(target.FifoLevel),
            signal_count=int(target.SignalCount),
        )

    def list_measurement_channels(self, device: str | int, task: str | int) -> list[str]:
        channels = _collection_item(self.get_device(device).Tasks, task).Channels
        return [str(channel.Name) for channel in _iter_collection(channels)]

    def add_measurement_channel(self, device: str | int, task: str | int, channel: str) -> Any:
        try:
            task_object = _collection_item(self.get_device(device).Tasks, task)
            return task_object.Channels.Add(channel)
        except Exception as exc:
            raise CANapeCOMError(f"添加测量信号 {channel} 失败：{exc}") from exc

    def configure_measurement_channels(
        self,
        device: str | int,
        task: str | int,
        channels: Sequence[str],
        *,
        clear: bool = True,
    ) -> list[str]:
        """批量配置任务信号并返回最终信号清单。"""
        if clear:
            self.clear_measurement_channels(device, task)
        for channel in channels:
            self.add_measurement_channel(device, task, channel)
        return self.list_measurement_channels(device, task)

    def read_measurement_channel(
        self, device: str | int, task: str | int, channel: str | int
    ) -> tuple[Any, int]:
        try:
            task_object = _collection_item(self.get_device(device).Tasks, task)
            channel_object = _collection_item(task_object.Channels, channel)
            return channel_object.Value, int(channel_object.TimeStamp)
        except Exception as exc:
            raise CANapeCOMError(f"读取测量信号 {channel} 失败：{exc}") from exc

    def clear_measurement_channels(self, device: str | int, task: str | int) -> None:
        _collection_item(self.get_device(device).Tasks, task).Channels.Clear()

    def read_task_current_values(
        self, device: str | int, task: str | int
    ) -> tuple[tuple[Any, ...], Any]:
        target = _collection_item(self.get_device(device).Tasks, task)
        try:
            result = target.CurrentValuesVariant()
            values, timestamp = self._split_sample_result(result)
            return tuple(values), timestamp
        except Exception as exc:
            raise CANapeCOMError(f"读取任务当前采样失败：{exc}") from exc

    def read_task_next_sample(
        self, device: str | int, task: str | int
    ) -> tuple[tuple[Any, ...], Any]:
        target = _collection_item(self.get_device(device).Tasks, task)
        try:
            result = target.NextSampleVariant()
            values, timestamp = self._split_sample_result(result)
            return tuple(values), timestamp
        except Exception as exc:
            raise CANapeCOMError(f"读取任务 FIFO 下一采样失败：{exc}") from exc

    # 标定控制
    def list_calibration_objects(
        self, device: str | int, *, limit: int | None = None
    ) -> list[str]:
        objects = self.get_device(device).CalibrationObjects
        count = int(objects.Count)
        if limit is not None:
            count = min(count, max(0, int(limit)))
        return [str(_collection_item(objects, index).Name) for index in range(1, count + 1)]

    def add_calibration_object(self, device: str | int, name: str) -> Any:
        objects = self.get_device(device).CalibrationObjects
        objects.Add(name)
        return _collection_item(objects, name)

    def remove_calibration_object(
        self, device: str | int, calibration_object: str | int
    ) -> None:
        self.get_device(device).CalibrationObjects.Remove(calibration_object)

    def clear_calibration_objects(self, device: str | int) -> None:
        self.get_device(device).CalibrationObjects.Clear()

    def get_calibration_object_info(
        self, device: str | int, calibration_object: str | int
    ) -> CalibrationObjectInfo:
        target = self._get_calibration_object(device, calibration_object)
        return CalibrationObjectInfo(
            name=str(target.Name),
            object_type=int(target.Type),
            calibration_type=int(target.Caltype),
            representation_type=int(target.RepresentationType),
            x_dimension=int(target.XDim),
            y_dimension=int(target.YDim),
        )

    def get_calibration_metadata(
        self, device: str | int, calibration_object: str | int
    ) -> dict[str, Any]:
        """读取标定对象常用范围、单位、地址和转换元数据。"""
        target = self._get_calibration_object(device, calibration_object)
        properties = {
            "name": "Name",
            "unit": "Unit",
            "minimum": "Min",
            "maximum": "Max",
            "address": "Address",
            "conversion": "Conversion",
            "comment": "Comment",
        }
        result: dict[str, Any] = {}
        for key, property_name in properties.items():
            try:
                result[key] = getattr(target, property_name)
            except Exception:
                result[key] = None
        result["info"] = self.get_calibration_object_info(
            device, calibration_object
        )
        return result

    def validate_calibration_value(
        self, device: str | int, calibration_object: str | int, value: Any
    ) -> bool:
        metadata = self.get_calibration_metadata(device, calibration_object)
        minimum = metadata.get("minimum")
        maximum = metadata.get("maximum")
        if minimum is not None and float(value) < float(minimum):
            raise ValueError(f"{calibration_object} 小于下限 {minimum}")
        if maximum is not None and float(value) > float(maximum):
            raise ValueError(f"{calibration_object} 大于上限 {maximum}")
        return True

    def read_calibration_value(
        self,
        device: str | int,
        calibration_object: str | int,
        *,
        physical: bool = True,
        force_read: bool = True,
    ) -> Any:
        target = self._get_calibration_object(device, calibration_object)
        target.RepresentationType = 1 if physical else 0
        try:
            if force_read:
                if hasattr(target, "ReadVariant"):
                    return target.ReadVariant()
                return target.Read()
            if hasattr(target, "ValueVariant"):
                return target.ValueVariant
            return target.Value
        except Exception as exc:
            raise CANapeCOMError(f"读取标定量 {calibration_object} 失败：{exc}") from exc

    def write_calibration_value(
        self,
        device: str | int,
        calibration_object: str | int,
        value: Any,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> Any:
        """写入标定量；默认回读验证，失败时抛出异常。"""
        target = self._get_calibration_object(device, calibration_object)
        target.RepresentationType = 1 if physical else 0
        try:
            target.Value = value
            target.Write()
            if not verify:
                return value
            actual = self.read_calibration_value(
                device, calibration_object, physical=physical, force_read=True
            )
            if not self._values_equal(actual, value):
                raise CANapeCOMError(
                    f"标定量 {calibration_object} 回读不一致：写入 {value!r}，回读 {actual!r}"
                )
            return actual
        except CANapeCOMError:
            raise
        except Exception as exc:
            raise CANapeCOMError(f"写入标定量 {calibration_object} 失败：{exc}") from exc

    def read_calibration_values(
        self,
        device: str | int,
        calibration_objects: Sequence[str],
        *,
        physical: bool = True,
    ) -> dict[str, Any]:
        return {
            name: self.read_calibration_value(device, name, physical=physical)
            for name in calibration_objects
        }

    def write_calibration_values(
        self,
        device: str | int,
        values: dict[str, Any],
        *,
        physical: bool = True,
        rollback_on_error: bool = True,
    ) -> dict[str, Any]:
        """批量写标定；默认写前快照，任一失败即尽力回滚。"""
        snapshot = self.read_calibration_values(
            device, list(values), physical=physical
        )
        written: dict[str, Any] = {}
        attempted: list[str] = []
        try:
            for name, value in values.items():
                attempted.append(name)
                written[name] = self.write_calibration_value(
                    device, name, value, physical=physical, verify=True
                )
            return written
        except Exception:
            if rollback_on_error:
                for name in reversed(attempted):
                    try:
                        self.write_calibration_value(
                            device,
                            name,
                            snapshot[name],
                            physical=physical,
                            verify=True,
                        )
                    except Exception:
                        LOGGER.exception("回滚标定量 %s 失败", name)
            raise

    def create_calibration_snapshot(
        self,
        device: str | int,
        calibration_objects: Sequence[str],
        *,
        physical: bool = True,
    ) -> dict[str, Any]:
        return self.read_calibration_values(
            device, calibration_objects, physical=physical
        )

    @staticmethod
    def diff_calibration_snapshots(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        differences: dict[str, dict[str, Any]] = {}
        for name in sorted(set(before) | set(after)):
            old = before.get(name, _MISSING)
            new = after.get(name, _MISSING)
            if not CANape._values_equal(old, new):
                difference = {
                    "before": None if old is _MISSING else old,
                    "after": None if new is _MISSING else new,
                }
                if old is _MISSING or new is _MISSING:
                    difference["before_present"] = old is not _MISSING
                    difference["after_present"] = new is not _MISSING
                differences[name] = difference
        return differences

    def restore_calibration_snapshot(
        self,
        device: str | int,
        snapshot: dict[str, Any],
        *,
        physical: bool = True,
    ) -> dict[str, Any]:
        return self.write_calibration_values(
            device,
            snapshot,
            physical=physical,
            rollback_on_error=False,
        )

    def list_calibration_axes(
        self, device: str | int, calibration_object: str | int
    ) -> list[dict[str, Any]]:
        """枚举曲线或 MAP 的轴对象；不同 CANape 版本兼容 AxisObjects/Axes。"""
        target = self._get_calibration_object(device, calibration_object)
        axes = getattr(target, "AxisObjects", None)
        if axes is None:
            axes = getattr(target, "Axes", None)
        if axes is None:
            return []
        result = []
        for index, axis in enumerate(_iter_collection(axes), start=1):
            result.append(
                {
                    "index": index,
                    "name": str(getattr(axis, "Name", f"Axis{index}")),
                    "unit": str(getattr(axis, "Unit", "") or ""),
                    "dimension": int(getattr(axis, "Dimension", getattr(axis, "XDim", 0))),
                    "type": int(getattr(axis, "Type", 0)),
                }
            )
        return result

    def _get_calibration_axis(
        self,
        device: str | int,
        calibration_object: str | int,
        axis: str | int,
    ) -> Any:
        target = self._get_calibration_object(device, calibration_object)
        axes = getattr(target, "AxisObjects", None)
        if axes is None:
            axes = getattr(target, "Axes", None)
        if axes is None:
            raise CANapeObjectNotFoundError(
                f"标定对象 {calibration_object} 不提供轴对象"
            )
        return _collection_item(axes, axis)

    def read_calibration_axis(
        self,
        device: str | int,
        calibration_object: str | int,
        axis: str | int,
        *,
        physical: bool = True,
        force_read: bool = True,
    ) -> Any:
        target = self._get_calibration_axis(device, calibration_object, axis)
        if hasattr(target, "RepresentationType"):
            target.RepresentationType = 1 if physical else 0
        try:
            if force_read:
                if hasattr(target, "ReadVariant"):
                    return target.ReadVariant()
                if hasattr(target, "Read"):
                    return target.Read()
            if hasattr(target, "ValueVariant"):
                return target.ValueVariant
            return target.Value
        except Exception as exc:
            raise CANapeCOMError(
                f"读取标定轴 {calibration_object}/{axis} 失败：{exc}"
            ) from exc

    def write_calibration_axis(
        self,
        device: str | int,
        calibration_object: str | int,
        axis: str | int,
        value: Any,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> Any:
        target = self._get_calibration_axis(device, calibration_object, axis)
        if hasattr(target, "RepresentationType"):
            target.RepresentationType = 1 if physical else 0
        try:
            target.Value = value
            target.Write()
            if not verify:
                return value
            actual = self.read_calibration_axis(
                device,
                calibration_object,
                axis,
                physical=physical,
                force_read=True,
            )
            if not self._values_equal(actual, value):
                raise CANapeCOMError(
                    f"标定轴 {calibration_object}/{axis} 回读不一致"
                )
            return actual
        except CANapeCOMError:
            raise
        except Exception as exc:
            raise CANapeCOMError(
                f"写入标定轴 {calibration_object}/{axis} 失败：{exc}"
            ) from exc

    def read_calibration_parameter(
        self,
        device: str | int,
        calibration_object: str | int,
        *,
        physical: bool = True,
    ) -> Any:
        """读取包含类型、范围、单位、轴和值的完整标定参数。"""
        from .calibration import CalibrationKind, CalibrationParameter

        metadata = self.get_calibration_metadata(device, calibration_object)
        info = metadata["info"]
        kind = (
            CalibrationKind.MAP if info.y_dimension > 1
            else CalibrationKind.CURVE if info.x_dimension > 1
            else CalibrationKind.SCALAR
        )
        axes = self.list_calibration_axes(device, calibration_object)
        axis_values = [
            list(
                self.read_calibration_axis(
                    device, calibration_object, item["index"], physical=physical
                )
            )
            for item in axes[:2]
        ]
        return CalibrationParameter(
            name=info.name,
            value=self.read_calibration_value(
                device, calibration_object, physical=physical
            ),
            kind=kind,
            unit=str(metadata.get("unit") or ""),
            minimum=metadata.get("minimum"),
            maximum=metadata.get("maximum"),
            x_axis=axis_values[0] if axis_values else [],
            y_axis=axis_values[1] if len(axis_values) > 1 else [],
            address=metadata.get("address"),
            conversion=str(metadata.get("conversion") or ""),
            comment=str(metadata.get("comment") or ""),
        )

    def write_calibration_parameter(
        self,
        device: str | int,
        parameter: Any,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> Any:
        """事务式写入完整标定参数，包括曲线/MAP 轴和值。"""
        errors = parameter.validate()
        if errors:
            raise ValueError("; ".join(errors))
        axes = self.list_calibration_axes(device, parameter.name)
        before_value = self.read_calibration_value(
            device, parameter.name, physical=physical
        )
        before_axes = {
            item["index"]: self.read_calibration_axis(
                device, parameter.name, item["index"], physical=physical
            )
            for item in axes[:2]
        }
        try:
            if parameter.x_axis and axes:
                self.write_calibration_axis(
                    device,
                    parameter.name,
                    axes[0]["index"],
                    parameter.x_axis,
                    physical=physical,
                    verify=verify,
                )
            if parameter.y_axis and len(axes) > 1:
                self.write_calibration_axis(
                    device,
                    parameter.name,
                    axes[1]["index"],
                    parameter.y_axis,
                    physical=physical,
                    verify=verify,
                )
            self.write_calibration_value(
                device,
                parameter.name,
                parameter.value,
                physical=physical,
                verify=verify,
            )
            return self.read_calibration_parameter(
                device, parameter.name, physical=physical
            )
        except Exception:
            for axis, value in before_axes.items():
                try:
                    self.write_calibration_axis(
                        device,
                        parameter.name,
                        axis,
                        value,
                        physical=physical,
                        verify=True,
                    )
                except Exception:
                    LOGGER.exception("回滚标定轴 %s/%s 失败", parameter.name, axis)
            self.write_calibration_value(
                device,
                parameter.name,
                before_value,
                physical=physical,
                verify=True,
            )
            raise

    def export_calibration_dataset(
        self,
        device: str | int,
        calibration_objects: Sequence[str],
        output_file: str | os.PathLike[str],
        *,
        identity: dict[str, str] | None = None,
        physical: bool = True,
    ) -> Any:
        from .calibration import CalibrationDataset

        dataset = CalibrationDataset(
            parameters={
                name: self.read_calibration_parameter(
                    device, name, physical=physical
                )
                for name in calibration_objects
            },
            identity=dict(identity or {}),
            source=f"CANape:{device}",
        )
        dataset.save(output_file)
        return dataset

    def import_calibration_dataset(
        self,
        device: str | int,
        input_file: str | os.PathLike[str],
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> dict[str, Any]:
        from .calibration import CalibrationDataset

        dataset = CalibrationDataset.load(input_file)
        dataset.require_valid()
        baseline = {
            name: self.read_calibration_parameter(device, name, physical=physical)
            for name in dataset.parameters
        }
        written = {}
        try:
            for name, parameter in dataset.parameters.items():
                written[name] = self.write_calibration_parameter(
                    device, parameter, physical=physical, verify=verify
                )
            return written
        except Exception:
            for parameter in reversed(list(baseline.values())):
                try:
                    self.write_calibration_parameter(
                        device, parameter, physical=physical, verify=True
                    )
                except Exception:
                    LOGGER.exception("回滚数据集标定量 %s 失败", parameter.name)
            raise

    # 记录器
    def list_recorders(self) -> list[RecorderInfo]:
        result: list[RecorderInfo] = []
        for recorder in _iter_collection(self._require_application().Recorders):
            result.append(
                RecorderInfo(
                    name=str(recorder.Name),
                    state=int(recorder.State),
                    recorder_type=int(recorder.Type),
                    mdf_filename=str(recorder.MDFFilename),
                )
            )
        return result

    def add_recorder(self, name: str, *, recorder_type: int | None = None) -> Any:
        recorders = self._require_application().Recorders
        if recorder_type is None:
            return recorders.Add(name)
        return recorders.Add2(name, int(recorder_type))

    def remove_recorder(self, recorder: str | int) -> None:
        self._require_application().Recorders.Remove(recorder)

    def select_recorder(self, recorder: str | int) -> None:
        recorders = self._require_application().Recorders
        recorders.SelectedRecorder = _collection_item(recorders, recorder)

    def get_selected_recorder(self) -> RecorderInfo:
        recorder = self._require_application().Recorders.SelectedRecorder
        return RecorderInfo(
            name=str(recorder.Name),
            state=int(recorder.State),
            recorder_type=int(recorder.Type),
            mdf_filename=str(recorder.MDFFilename),
        )

    def set_recorder_output_file(
        self, recorder: str | int, path: str | os.PathLike[str]
    ) -> None:
        output = _as_path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        _collection_item(self._require_application().Recorders, recorder).MDFFilename = str(
            output
        )

    def pause_recorder(self, recorder: str | int, paused: bool = True) -> None:
        _collection_item(self._require_application().Recorders, recorder).Pause(bool(paused))

    def set_recorder_data_reduction(self, recorder: str | int, value: int) -> None:
        _collection_item(
            self._require_application().Recorders, recorder
        ).DataReduction = int(value)

    # 网络、脚本和日志
    def list_networks(self) -> list[dict[str, Any]]:
        return [
            {"name": str(network.Name), "active": bool(network.IsActivate)}
            for network in _iter_collection(self._require_application().NetWorks)
        ]

    def activate_network(self, network: str | int, active: bool = True) -> None:
        _collection_item(self._require_application().NetWorks, network).Activate(
            bool(active)
        )

    def configure_network(self, network: str | int, *, active: bool = True) -> dict[str, Any]:
        self.activate_network(network, active)
        target = _collection_item(self._require_application().NetWorks, network)
        return {"name": str(target.Name), "active": bool(target.IsActivate)}

    def configure_api_logging(
        self,
        *,
        enabled: bool,
        output_file: str | os.PathLike[str] | None = None,
        overwrite: bool = True,
    ) -> None:
        logging_object = self._require_application().Logging
        if output_file is not None:
            output = _as_path(output_file)
            output.parent.mkdir(parents=True, exist_ok=True)
            logging_object.File = str(output)
        logging_object.OverWrite = bool(overwrite)
        logging_object.Enable = bool(enabled)

    def list_scripts(self) -> list[dict[str, Any]]:
        return [
            {
                "name": str(script.Name),
                "state": int(script.State),
                "state_text": str(script.StateText),
                "result_value": script.ResultValue,
                "result_string": str(script.ResultString),
            }
            for script in _iter_collection(self._require_application().Scripts)
        ]

    def add_script(
        self, script: str | os.PathLike[str], *, script_file: bool = True
    ) -> Any:
        value = str(_as_path(script)) if script_file else str(script)
        if script_file and not Path(value).is_file():
            raise FileNotFoundError(value)
        return self._require_application().Scripts.Add(value, bool(script_file))

    def start_script(self, script: str | int, *, command_line: str = "") -> None:
        target = _collection_item(self._require_application().Scripts, script)
        if command_line:
            target.Commandline = command_line
        target.Start()

    def stop_script(self, script: str | int) -> None:
        _collection_item(self._require_application().Scripts, script).Stop()

    # 诊断与刷写
    def send_diagnostic_request(
        self,
        device: str | int,
        service_and_path: str,
        *,
        parameters: dict[str, Any] | None = None,
        timeout: float = 5.0,
        suppress_positive_response: bool = False,
    ) -> list[DiagnosticResponse]:
        diagnostic = self.get_device(device).Diagnostic
        request = diagnostic.CreateRequest(service_and_path)
        for name, value in (parameters or {}).items():
            request.SetParameter(name, value)
        request.SuppressPositiveResponse = bool(suppress_positive_response)
        request.Send()
        if not self._wait_for(lambda: not bool(request.Pending), timeout):
            raise CANapeCOMError(f"诊断请求超时：{service_and_path}")
        return self._diagnostic_responses(request.Responses)

    def send_raw_diagnostic_request(
        self,
        device: str | int,
        payload: Sequence[int],
        *,
        timeout: float = 5.0,
    ) -> list[DiagnosticResponse]:
        data = tuple(int(item) for item in payload)
        if not data or any(item < 0 or item > 255 for item in data):
            raise ValueError("payload 必须是非空 0~255 字节序列")
        request = self.get_device(device).Diagnostic.CreateRequestFromStream(data)
        request.Send()
        if not self._wait_for(lambda: not bool(request.Pending), timeout):
            raise CANapeCOMError("原始诊断请求超时")
        return self._diagnostic_responses(request.Responses)

    def start_tester_present(self, device: str | int) -> None:
        self.get_device(device).Diagnostic.DiagStartTesterPresent()

    def stop_tester_present(self, device: str | int) -> None:
        self.get_device(device).Diagnostic.DiagStopTesterPresent()

    def get_tester_present_status(self, device: str | int) -> Any:
        return self.get_device(device).Diagnostic.TesterPresentStatus

    def set_tester_present(self, device: str | int, *, enabled: bool) -> Any:
        if enabled:
            self.start_tester_present(device)
        else:
            self.stop_tester_present(device)
        return self.get_tester_present_status(device)

    def execute_diagnostic_job(
        self, device: str | int, job: Any, *, command_line: Any = ""
    ) -> Any:
        return self.get_device(device).Diagnostic.DiagExecuteJob(job, command_line)

    def list_security_profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "name": str(profile.Name),
                "profile_id": profile.ProfileID,
                "description": str(profile.Description),
            }
            for profile in _iter_collection(self._require_application().SecProfiles)
        ]

    def start_flash(
        self,
        device: str | int,
        job: Any,
        session: Any,
        *,
        config_file: str | os.PathLike[str] | None = None,
    ) -> None:
        config = "" if config_file is None else str(_as_path(config_file))
        self.get_device(device).FlashManager.Start(job, session, config)

    def stop_flash(self, device: str | int) -> None:
        self.get_device(device).FlashManager.Stop()

    def get_flash_state(self, device: str | int) -> FlashStateInfo:
        state = self.get_device(device).FlashManager.State
        return FlashStateInfo(
            busy=bool(state.Busy),
            progress=int(state.Progress),
            info=str(state.Info),
            has_return_value=bool(state.HasReturnValue),
            return_value=state.ReturnValue if bool(state.HasReturnValue) else None,
        )

    def control_flash(
        self,
        device: str | int,
        *,
        action: str,
        job: Any = None,
        session: Any = None,
        config_file: str | os.PathLike[str] | None = None,
    ) -> FlashStateInfo:
        if action == "start":
            if job is None or session is None:
                raise ValueError("启动刷写必须提供 job 和 session")
            self.start_flash(device, job, session, config_file=config_file)
        elif action == "stop":
            self.stop_flash(device)
        else:
            raise ValueError("action 必须是 start 或 stop")
        return self.get_flash_state(device)

    @staticmethod
    def _diagnostic_responses(responses: Any) -> list[DiagnosticResponse]:
        result: list[DiagnosticResponse] = []
        for response in _iter_collection(responses):
            result.append(
                DiagnosticResponse(
                    positive=bool(response.Positive),
                    response_code=int(response.ResponseCode),
                    sender=str(response.Sender),
                    stream=tuple(int(item) for item in response.Stream),
                )
            )
        return result

    @staticmethod
    def _wait_for(predicate: Any, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() <= deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return bool(predicate())

    @staticmethod
    def _split_sample_result(result: Any) -> tuple[Sequence[Any], Any]:
        """兼容 pywin32 对 COM 返回值和 out 参数的两种排列。"""
        if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
            return (result,), None
        if len(result) != 2:
            return result, None
        first, second = result
        first_is_values = isinstance(first, Sequence) and not isinstance(
            first, (str, bytes)
        )
        second_is_values = isinstance(second, Sequence) and not isinstance(
            second, (str, bytes)
        )
        if first_is_values and not second_is_values:
            return first, second
        if second_is_values and not first_is_values:
            return second, first
        return result, None

    @staticmethod
    def _device_info(device: Any) -> DeviceInfo:
        return DeviceInfo(
            name=str(device.Name),
            driver_type=str(device.DriverType),
            channel=int(device.Channel),
            online=bool(device.IsOnline),
            database_filename=str(device.DatabaseFilename),
        )

    def _get_calibration_object(
        self, device: str | int, calibration_object: str | int
    ) -> Any:
        try:
            return _collection_item(
                self.get_device(device).CalibrationObjects, calibration_object
            )
        except Exception as exc:
            raise CANapeObjectNotFoundError(
                f"设备 {device} 中未找到标定量：{calibration_object}"
            ) from exc

    @staticmethod
    def _values_equal(actual: Any, expected: Any) -> bool:
        if isinstance(actual, Sequence) and not isinstance(actual, (str, bytes)):
            if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
                return False
            return list(actual) == list(expected)
        if isinstance(actual, float) or isinstance(expected, float):
            try:
                return abs(float(actual) - float(expected)) <= 1e-9
            except (TypeError, ValueError):
                return False
        return actual == expected
