from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent2canape import CANape, CANapeCOMError, CANapeNotConnectedError


class FakeCollection:
    def __init__(self, *items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, key):
        if isinstance(key, str):
            for item in self._items:
                if item.Name == key:
                    return item
            raise KeyError(key)
        return self._items[key - 1]

    def Remove(self, key):
        target = self.Item(key)
        self._items.remove(target)

    def Clear(self):
        self._items.clear()


class FakeCalibration:
    def __init__(self, name, value, *, x_axis=None, y_axis=None):
        self.Name = name
        self.Value = value
        self.ValueVariant = value
        self.RepresentationType = 1
        self.Type = 2 if y_axis else 1 if x_axis else 0
        self.Caltype = self.Type
        self.XDim = len(x_axis or ())
        self.YDim = len(y_axis or ())
        self.Unit = "%"
        self.Min = 0.0
        self.Max = 100.0
        self.Address = 0x1000
        self.Conversion = "linear"
        self.Comment = "test"
        axes = []
        if x_axis is not None:
            axes.append(FakeAxis("X", x_axis))
        if y_axis is not None:
            axes.append(FakeAxis("Y", y_axis))
        self.AxisObjects = FakeCollection(*axes)

    def ReadVariant(self):
        return self.Value

    def Write(self):
        if getattr(self, "FailWrite", False):
            raise RuntimeError(f"{self.Name} write failed")
        self.ValueVariant = self.Value


class FakeAxis:
    def __init__(self, name, value):
        self.Name = name
        self.Value = list(value)
        self.ValueVariant = list(value)
        self.RepresentationType = 1
        self.Unit = "axis"
        self.Dimension = len(value)
        self.Type = 0

    def ReadVariant(self):
        return list(self.Value)

    def Write(self):
        self.ValueVariant = list(self.Value)


class FakeMeasurement:
    def __init__(self):
        self.Running = False
        self.MeasurementState = 0
        self.MDFFilename = ""
        self.SampleSize = 1
        self.FifoSize = 100
        self.SyncMode = False
        self.ResumeMode = False
        self.UseNAN = False

    def Start(self):
        self.Running = True
        self.MeasurementState = 5

    def Stop(self):
        self.Running = False
        self.MeasurementState = 0


def make_fake_app():
    memory = bytearray(range(16))

    def read_memory(address, size):
        return tuple(memory[:size])

    def write_memory(address, data):
        memory[: len(data)] = bytes(data)

    channel = SimpleNamespace(Name="VehicleSpeed", Value=42.5, TimeStamp=100)
    channels = FakeCollection(channel)
    channels.Add = lambda name: channel
    task = SimpleNamespace(
        Name="10ms",
        ID=1,
        Event=10,
        SamplingTime=0.01,
        FifoLevel=0,
        SignalCount=1,
        Channels=channels,
        CurrentValuesVariant=lambda: (100, (42.5,)),
        NextSampleVariant=lambda: (110, (43.0,)),
    )
    calibration = FakeCalibration("Gain", 1.0)
    offset = FakeCalibration("Offset", 0.0)
    curve = FakeCalibration(
        "TorqueCurve", [10.0, 20.0, 30.0], x_axis=[1000.0, 2000.0, 3000.0]
    )
    map_value = FakeCalibration(
        "TorqueMap",
        [[10.0, 20.0], [30.0, 40.0]],
        x_axis=[1000.0, 2000.0],
        y_axis=[0.5, 1.0],
    )
    device = SimpleNamespace(
        Name="ECU",
        DriverType="XCP",
        Channel=1,
        IsOnline=False,
        DatabaseFilename="ecu.a2l",
        Tasks=FakeCollection(task),
        CalibrationObjects=FakeCollection(calibration, offset, curve, map_value),
        Databases=FakeCollection(SimpleNamespace(Name="ecu.a2l")),
        GoOnline=lambda download: setattr(device, "IsOnline", True),
        GoOffline=lambda: setattr(device, "IsOnline", False),
        ReadMemory=read_memory,
        WriteMemory=write_memory,
    )
    device.Upload = lambda path: Path(path).write_bytes(b"hex")
    device.Download = lambda path: setattr(device, "Downloaded", Path(path).read_bytes())
    recorder = SimpleNamespace(
        Name="Recorder1", State=1, Type=0, MDFFilename="", Pause=lambda paused: None
    )
    recorders = FakeCollection(recorder)
    recorders.Add = lambda name: recorder
    recorders.Add2 = lambda name, recorder_type: recorder
    recorders.SelectedRecorder = recorder
    network = SimpleNamespace(
        Name="CAN1",
        IsActivate=False,
    )
    network.Activate = lambda active: setattr(network, "IsActivate", active)
    script = SimpleNamespace(
        Name="Smoke",
        State=0,
        StateText="stopped",
        ResultValue=0,
        ResultString="",
        Commandline="",
    )
    script.Start = lambda: setattr(script, "State", 1)
    script.Stop = lambda: setattr(script, "State", 0)
    response = SimpleNamespace(
        Positive=True,
        ResponseCode=0,
        Sender="ECU",
        Stream=(0x62, 0xF1, 0x90),
    )

    def make_request():
        request = SimpleNamespace(
            Pending=False,
            SuppressPositiveResponse=False,
            Responses=FakeCollection(response),
        )
        request.SetParameter = lambda name, value: None
        request.Send = lambda: None
        return request

    diagnostic = SimpleNamespace(
        CreateRequest=lambda service: make_request(),
        CreateRequestFromStream=lambda payload: make_request(),
        TesterPresentStatus=1,
        DiagStartTesterPresent=lambda: None,
        DiagStopTesterPresent=lambda: None,
        DiagExecuteJob=lambda job, command_line: 0,
    )
    device.Diagnostic = diagnostic
    flash_state = SimpleNamespace(
        Busy=False,
        Progress=100,
        Info="done",
        HasReturnValue=True,
        ReturnValue=0,
    )
    device.FlashManager = SimpleNamespace(
        State=flash_state,
        Start=lambda job, session, config: None,
        Stop=lambda: None,
    )
    devices = FakeCollection(device)
    devices.Add2 = lambda *args: device
    version = SimpleNamespace(Main=2, Sub=3, Description="Windows95/WindowsNT")
    app_version = SimpleNamespace(
        Main=17, Sub=0, Release=31, Description="CANape"
    )
    open_arguments = []
    app = SimpleNamespace(
        Name="Vector Application for CANape",
        Version=version,
        APPVersion=app_version,
        WorkingDirectory=r"C:\CANapeProject",
        CNAFilename="Config.cna",
        Measurement=FakeMeasurement(),
        Devices=devices,
        Recorders=recorders,
        NetWorks=FakeCollection(network),
        Scripts=FakeCollection(script),
        SecProfiles=FakeCollection(
            SimpleNamespace(Name="Default", ProfileID=1, Description="test")
        ),
        Logging=SimpleNamespace(File="", OverWrite=True, Enable=False),
        RunScript=lambda path: None,
        Quit=lambda: None,
        QuitNonModal=lambda: None,
        Open2=lambda *arguments: open_arguments.append(arguments),
        OpenArguments=open_arguments,
    )
    return app


class CANapeTests(unittest.TestCase):
    def setUp(self):
        self.app = make_fake_app()
        self.canape = CANape(com_factory=lambda: self.app)
        self.canape.connect()

    def tearDown(self):
        self.canape.disconnect()

    def test_requires_connection(self):
        canape = CANape(com_factory=lambda: self.app)
        with self.assertRaises(CANapeNotConnectedError):
            canape.get_canape_version_info()

    def test_version_and_project_info(self):
        self.assertEqual(
            str(self.canape.get_canape_version_info()), "17.0.31 (CANape)"
        )
        self.assertEqual(
            str(self.canape.get_com_api_version_info()),
            "2.3 (Windows95/WindowsNT)",
        )
        self.assertEqual(self.canape.get_project_info()["cna_filename"], "Config.cna")

    def test_open_translates_non_modal_to_com_modal_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            self.canape.open(directory, non_modal=True)
            self.assertFalse(self.app.OpenArguments[-1][4])
            self.canape.open(directory, non_modal=False)
            self.assertTrue(self.app.OpenArguments[-1][4])

    def test_measurement_start_stop(self):
        self.assertTrue(self.canape.start_measurement())
        self.assertTrue(self.canape.is_measurement_running())
        self.assertTrue(self.canape.stop_measurement())
        self.assertFalse(self.canape.is_measurement_running())

    def test_measurement_configuration(self):
        self.canape.configure_measurement(
            sample_size=8, fifo_size=256, sync_mode=True, use_nan=True
        )
        config = self.canape.get_measurement_configuration()
        self.assertEqual(config["sample_size"], 8)
        self.assertEqual(config["fifo_size"], 256)
        self.assertTrue(config["sync_mode"])
        self.assertTrue(config["use_nan"])
        with self.assertRaises(ValueError):
            self.canape.configure_measurement(sample_size=0)
        with self.assertRaises(ValueError):
            self.canape.configure_measurement(fifo_size=-1)

    def test_measurement_start_timeout_is_error(self):
        self.app.Measurement.Start = lambda: None
        with self.assertRaises(CANapeCOMError):
            self.canape.start_measurement(wait_until_running=0)

    def test_device_and_channel_read(self):
        self.assertEqual(self.canape.list_devices()[0].name, "ECU")
        self.assertEqual(self.canape.list_tasks("ECU"), ["10ms"])
        value, timestamp = self.canape.read_measurement_channel("ECU", "10ms", "VehicleSpeed")
        self.assertEqual(value, 42.5)
        self.assertEqual(timestamp, 100)

    def test_device_online_offline(self):
        self.canape.set_device_online("ECU")
        self.assertTrue(self.canape.is_device_online("ECU"))
        self.canape.set_device_offline("ECU")
        self.assertFalse(self.canape.is_device_online("ECU"))

    def test_task_info_and_samples(self):
        info = self.canape.get_task_info("ECU", "10ms")
        self.assertEqual(info.signal_count, 1)
        self.assertEqual(self.canape.read_task_current_values("ECU", "10ms"), ((42.5,), 100))
        self.assertEqual(self.canape.read_task_next_sample("ECU", "10ms"), ((43.0,), 110))
        task = self.canape.get_device("ECU").Tasks.Item("10ms")
        task.CurrentValuesVariant = lambda: ((44.0,), 120)
        self.assertEqual(
            self.canape.read_task_current_values("ECU", "10ms"), ((44.0,), 120)
        )

    def test_memory_read(self):
        self.assertEqual(self.canape.read_memory("ECU", 0x1000, 4), (0, 1, 2, 3))
        self.assertEqual(
            self.canape.write_memory("ECU", 0x1000, (9, 8, 7, 6)),
            (9, 8, 7, 6),
        )
        self.assertEqual(self.canape.list_device_databases("ECU"), ["ecu.a2l"])

    def test_device_data_upload_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploaded = root / "uploaded.hex"
            self.canape.transfer_device_data("ECU", uploaded, direction="upload")
            self.assertEqual(uploaded.read_bytes(), b"hex")
            source = root / "download.hex"
            source.write_bytes(b"new")
            self.canape.transfer_device_data("ECU", source, direction="download")
            self.assertEqual(self.canape.get_device("ECU").Downloaded, b"new")
            with self.assertRaises(ValueError):
                self.canape.transfer_device_data("ECU", source, direction="invalid")

    def test_calibration_read_write(self):
        self.assertEqual(self.canape.read_calibration_value("ECU", "Gain"), 1.0)
        self.assertEqual(self.canape.write_calibration_value("ECU", "Gain", 2.5), 2.5)

    def test_calibration_snapshot_diff_and_restore(self):
        before = self.canape.create_calibration_snapshot("ECU", ["Gain"])
        self.canape.write_calibration_values("ECU", {"Gain": 3.0})
        after = self.canape.create_calibration_snapshot("ECU", ["Gain"])
        self.assertEqual(
            self.canape.diff_calibration_snapshots(before, after),
            {"Gain": {"before": 1.0, "after": 3.0}},
        )
        self.canape.restore_calibration_snapshot("ECU", before)
        self.assertEqual(self.canape.read_calibration_value("ECU", "Gain"), 1.0)

    def test_curve_and_map_axes_round_trip(self):
        curve = self.canape.read_calibration_parameter("ECU", "TorqueCurve")
        self.assertEqual(curve.kind.value, "curve")
        self.assertEqual(curve.x_axis, [1000.0, 2000.0, 3000.0])
        curve.value = [15.0, 25.0, 35.0]
        curve.x_axis = [900.0, 1900.0, 2900.0]
        actual = self.canape.write_calibration_parameter("ECU", curve)
        self.assertEqual(actual.value, [15.0, 25.0, 35.0])
        self.assertEqual(actual.x_axis, [900.0, 1900.0, 2900.0])

        parameter = self.canape.read_calibration_parameter("ECU", "TorqueMap")
        self.assertEqual(parameter.kind.value, "map")
        self.assertEqual(parameter.y_axis, [0.5, 1.0])

    def test_calibration_dataset_export_import(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dataset.json"
            dataset = self.canape.export_calibration_dataset(
                "ECU",
                ["Gain", "TorqueCurve", "TorqueMap"],
                output,
                identity={"software": "1.0", "calibration": "A"},
            )
            self.assertTrue(output.is_file())
            self.assertEqual(dataset.identity["software"], "1.0")
            dataset.parameters["Gain"].value = 2.0
            dataset.save(output)
            result = self.canape.import_calibration_dataset("ECU", output)
            self.assertEqual(result["Gain"].value, 2.0)

    def test_calibration_batch_rolls_back_attempted_values(self):
        offset = self.canape.get_device("ECU").CalibrationObjects.Item("Offset")
        offset.FailWrite = True
        with self.assertLogs("agent2canape", level="ERROR"), self.assertRaises(CANapeCOMError):
            self.canape.write_calibration_values(
                "ECU", {"Gain": 4.0, "Offset": 2.0}
            )
        self.assertEqual(self.canape.read_calibration_value("ECU", "Gain"), 1.0)

    def test_snapshot_diff_distinguishes_missing_from_none(self):
        self.assertEqual(
            self.canape.diff_calibration_snapshots({"A": None}, {}),
            {
                "A": {
                    "before": None,
                    "after": None,
                    "before_present": True,
                    "after_present": False,
                }
            },
        )

    def test_recorder_network_script_and_logging(self):
        self.assertEqual(self.canape.get_selected_recorder().name, "Recorder1")
        self.assertEqual(self.canape.list_networks()[0]["name"], "CAN1")
        self.canape.activate_network("CAN1")
        self.assertTrue(self.canape.list_networks()[0]["active"])
        self.assertTrue(self.canape.configure_network("CAN1", active=True)["active"])
        self.canape.start_script("Smoke", command_line="--dry-run")
        self.assertEqual(self.canape.list_scripts()[0]["state"], 1)
        self.canape.stop_script("Smoke")
        self.canape.configure_api_logging(enabled=True)
        self.assertTrue(self.app.Logging.Enable)

    def test_diagnostics_flash_and_security(self):
        responses = self.canape.send_diagnostic_request(
            "ECU", "ReadDataByIdentifier", parameters={"DID": 0xF190}
        )
        self.assertTrue(responses[0].positive)
        raw = self.canape.send_raw_diagnostic_request(
            "ECU", (0x22, 0xF1, 0x90)
        )
        self.assertEqual(raw[0].stream, (0x62, 0xF1, 0x90))
        self.assertEqual(self.canape.set_tester_present("ECU", enabled=True), 1)
        self.assertEqual(self.canape.control_flash("ECU", action="stop").progress, 100)
        with self.assertRaises(ValueError):
            self.canape.control_flash("ECU", action="start")
        self.assertEqual(self.canape.get_flash_state("ECU").progress, 100)
        self.assertEqual(self.canape.list_security_profiles()[0]["name"], "Default")

    def test_measurement_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "logs" / "test.mf4"
            self.canape.set_measurement_output_file(output)
            self.assertEqual(Path(self.canape.get_measurement_output_file()), output.resolve())


if __name__ == "__main__":
    unittest.main()
