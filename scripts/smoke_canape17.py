"""使用 CANape 安装自带 OfflineAnalysis 项目执行只读冒烟测试。"""

from __future__ import annotations

import argparse
import json

from py_canape import CANape


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        default=r"D:\Software\Canape17\Examples\OfflineAnalysis",
        help="CANape 示例项目目录",
    )
    args = parser.parse_args()

    result: dict[str, object] = {"project": args.project}
    with CANape() as canape:
        try:
            canape.open(args.project, non_modal=True)
            result["canape_version"] = str(canape.get_canape_version_info())
            result["com_api_version"] = str(canape.get_com_api_version_info())
            result["project_info"] = canape.get_project_info()
            result["devices"] = [device.name for device in canape.list_devices()]
            result["measurement_running"] = canape.is_measurement_running()
            result["status"] = "passed"
            return_code = 0
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            return_code = 1
        finally:
            if canape.connected:
                try:
                    canape.quit()
                except Exception as exc:
                    result["quit_error"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
