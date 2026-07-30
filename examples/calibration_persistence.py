"""Working→RAM→ROM 持久化、掉线恢复和多 ECU 分阶段提交示例。"""

from pathlib import Path

from agent2canape import (
    CalibrationDataset,
    CalibrationMemoryLedger,
    CalibrationParameter,
    CalibrationPersistenceCoordinator,
    InMemoryCalibrationTarget,
    StagedECUPersistenceTask,
    StagedMultiECUPersistenceCoordinator,
)


def make_dataset(device: str, gain: float) -> CalibrationDataset:
    return CalibrationDataset(
        parameters={
            "Gain": CalibrationParameter(
                "Gain",
                gain,
                unit="%",
                minimum=0.0,
                maximum=10.0,
            )
        },
        identity={"ecu": device, "software": "SW_42"},
    )


baseline = {
    "VCU": make_dataset("VCU", 1.0),
    "BMS": make_dataset("BMS", 2.0),
}
target = InMemoryCalibrationTarget(baseline)

# 模拟“写 RAM 已成功，但链路在响应前断开”。协调器重连后先回读，
# 若目标值已经生效，则记录不确定动作的协调结果，而不是盲目重复写入。
target.inject("apply_ram_after_disconnect")
ledger = CalibrationMemoryLedger("VCU")
coordinator = CalibrationPersistenceCoordinator(
    target,
    ledger,
    journal_path=Path("build") / "vcu-persistence.json",
)
result = coordinator.execute(
    make_dataset("VCU", 3.0),
    job_id="CAL-DEMO-001",
    actor="calibrator",
    approved_by="reviewer",
)
print(result)

# 多 ECU 先完成全部 RAM 回读，再进入 ROM 阶段，避免某个 ECU 尚未
# 验证 RAM 时其他 ECU 已经开始持久化。
multi_result = StagedMultiECUPersistenceCoordinator.apply(
    target,
    [
        StagedECUPersistenceTask(
            "VCU", make_dataset("VCU", 4.0), "vehicle-reviewer"
        ),
        StagedECUPersistenceTask(
            "BMS", make_dataset("BMS", 5.0), "vehicle-reviewer"
        ),
    ],
    actor="calibrator",
)
print(multi_result)
