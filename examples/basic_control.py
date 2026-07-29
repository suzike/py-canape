"""基础使用示例。

修改 PROJECT、DEVICE、TASK、CHANNEL 后运行。写标定值前请确认 ECU 状态安全。
"""

from py_canape import CANape

PROJECT = r"D:\path\to\canape_project"
DEVICE = "XCPsim"
TASK = 1
CHANNEL = 1


with CANape() as canape:
    canape.open(PROJECT)
    print(canape.get_canape_version_info())
    print(canape.list_devices())

    canape.start_measurement()
    value, timestamp = canape.read_measurement_channel(DEVICE, TASK, CHANNEL)
    print(f"value={value}, timestamp={timestamp}")
    canape.stop_measurement()
    canape.quit()

