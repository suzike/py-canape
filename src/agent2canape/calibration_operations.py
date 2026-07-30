"""生产标定作业、实验恢复、多 ECU 事务和 Pareto 分析。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .calibration import (
    CalibrationBackend,
    CalibrationChange,
    CalibrationDataset,
    CalibrationPlan,
    _json_value,
    _same_value,
    _utc_now,
)
from .errors import SafetyViolationError


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _dataset_content_digest(dataset: CalibrationDataset) -> str:
    payload = dataset.to_dict()
    payload.pop("created_utc", None)
    payload.pop("source", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ChangeSetStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    COMMENT = "comment"


@dataclass(frozen=True, slots=True)
class CalibrationChangeItem:
    name: str
    value: Any
    reason: str
    function_group: str
    page: str
    owner: str
    risk: str = "medium"
    expected_before: Any = None
    enforce_expected: bool = False
    evidence: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        errors = []
        for label, value in (
            ("name", self.name),
            ("reason", self.reason),
            ("function_group", self.function_group),
            ("page", self.page),
            ("owner", self.owner),
        ):
            if not str(value).strip():
                errors.append(f"变更项 {self.name or '<unnamed>'} 缺少 {label}")
        if self.risk not in {"low", "medium", "high", "critical"}:
            errors.append(f"{self.name} risk 必须是 low/medium/high/critical")
        return errors

    def to_change(self) -> CalibrationChange:
        return CalibrationChange(
            name=self.name,
            value=_json_value(self.value),
            reason=self.reason,
            expected_before=_json_value(self.expected_before),
            enforce_expected=self.enforce_expected,
        )


@dataclass(frozen=True, slots=True)
class CalibrationReview:
    reviewer: str
    decision: ReviewDecision
    comment: str = ""
    created_utc: str = field(default_factory=_utc_now)


@dataclass(slots=True)
class CalibrationChangeSet:
    name: str
    owner: str
    items: list[CalibrationChangeItem]
    ticket: str = ""
    required_approvals: int = 1
    status: ChangeSetStatus = ChangeSetStatus.DRAFT
    reviews: list[CalibrationReview] = field(default_factory=list)
    created_utc: str = field(default_factory=_utc_now)
    submitted_utc: str = ""
    applied_utc: str = ""
    application_result: dict[str, Any] = field(default_factory=dict)

    def validate(self, dataset: CalibrationDataset | None = None) -> list[str]:
        errors = []
        if not self.name.strip():
            errors.append("变更集名称不能为空")
        if not self.owner.strip():
            errors.append("变更集 owner 不能为空")
        if self.required_approvals <= 0:
            errors.append("required_approvals 必须大于 0")
        seen = set()
        for item in self.items:
            errors.extend(item.validate())
            if item.name in seen:
                errors.append(f"变更集包含重复标定量：{item.name}")
            seen.add(item.name)
        if dataset is not None:
            plan = CalibrationPlan(
                [item.to_change() for item in self.items],
                name=self.name,
                author=self.owner,
                ticket=self.ticket,
            )
            errors.extend(plan.validate(dataset))
        return errors

    def submit(self, dataset: CalibrationDataset | None = None) -> None:
        if self.status is not ChangeSetStatus.DRAFT:
            raise RuntimeError("只有 draft 变更集可以提交评审")
        errors = self.validate(dataset)
        if errors:
            raise ValueError("; ".join(errors))
        self.status = ChangeSetStatus.IN_REVIEW
        self.submitted_utc = _utc_now()

    def review(
        self,
        reviewer: str,
        decision: ReviewDecision | str,
        *,
        comment: str = "",
    ) -> CalibrationReview:
        if self.status is not ChangeSetStatus.IN_REVIEW:
            raise RuntimeError("变更集当前不处于 in_review")
        reviewer = reviewer.strip()
        if not reviewer:
            raise ValueError("reviewer 不能为空")
        if reviewer.casefold() == self.owner.casefold():
            raise SafetyViolationError("变更集 owner 不能审批自己的变更")
        decision = ReviewDecision(decision)
        if decision is ReviewDecision.REJECT and not comment.strip():
            raise ValueError("拒绝变更必须填写原因")
        if decision in {ReviewDecision.APPROVE, ReviewDecision.REJECT} and any(
            review.reviewer.casefold() == reviewer.casefold()
            and review.decision in {ReviewDecision.APPROVE, ReviewDecision.REJECT}
            for review in self.reviews
        ):
            raise ValueError(f"{reviewer} 已经提交过最终评审决定")
        review = CalibrationReview(reviewer, decision, comment.strip())
        self.reviews.append(review)
        if decision is ReviewDecision.REJECT:
            self.status = ChangeSetStatus.REJECTED
        elif (
            sum(item.decision is ReviewDecision.APPROVE for item in self.reviews)
            >= self.required_approvals
        ):
            self.status = ChangeSetStatus.APPROVED
        return review

    def build_plan(self) -> CalibrationPlan:
        if self.status is not ChangeSetStatus.APPROVED:
            raise SafetyViolationError("变更集尚未获得足够审批")
        approvers = [
            review.reviewer
            for review in self.reviews
            if review.decision is ReviewDecision.APPROVE
        ]
        return CalibrationPlan(
            [item.to_change() for item in self.items],
            name=self.name,
            author=self.owner,
            ticket=self.ticket,
            approved_by=", ".join(approvers),
            approved_utc=max(
                review.created_utc
                for review in self.reviews
                if review.decision is ReviewDecision.APPROVE
            ),
        )

    def mark_applied(self, result: Mapping[str, Any]) -> None:
        if self.status is not ChangeSetStatus.APPROVED:
            raise SafetyViolationError("只有 approved 变更集可以标记为 applied")
        self.status = ChangeSetStatus.APPLIED
        self.applied_utc = _utc_now()
        self.application_result = _json_value(dict(result))

    def summary(self, dataset: CalibrationDataset | None = None) -> dict[str, Any]:
        def counts(field_name: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for item in self.items:
                key = str(getattr(item, field_name))
                result[key] = result.get(key, 0) + 1
            return dict(sorted(result.items()))

        approvals = [
            review.reviewer
            for review in self.reviews
            if review.decision is ReviewDecision.APPROVE
        ]
        return {
            "name": self.name,
            "owner": self.owner,
            "ticket": self.ticket,
            "status": self.status.value,
            "change_count": len(self.items),
            "required_approvals": self.required_approvals,
            "approvals": approvals,
            "functions": counts("function_group"),
            "pages": counts("page"),
            "item_owners": counts("owner"),
            "risks": counts("risk"),
            "errors": self.validate(dataset),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner,
            "ticket": self.ticket,
            "required_approvals": self.required_approvals,
            "status": self.status.value,
            "created_utc": self.created_utc,
            "submitted_utc": self.submitted_utc,
            "applied_utc": self.applied_utc,
            "application_result": _json_value(self.application_result),
            "items": [_json_value(asdict(item)) for item in self.items],
            "reviews": [
                {**asdict(review), "decision": review.decision.value}
                for review in self.reviews
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationChangeSet:
        return cls(
            name=str(data["name"]),
            owner=str(data["owner"]),
            ticket=str(data.get("ticket", "")),
            required_approvals=int(data.get("required_approvals", 1)),
            status=ChangeSetStatus(data.get("status", "draft")),
            created_utc=str(data.get("created_utc", _utc_now())),
            submitted_utc=str(data.get("submitted_utc", "")),
            applied_utc=str(data.get("applied_utc", "")),
            application_result=dict(data.get("application_result", {})),
            items=[
                CalibrationChangeItem(
                    **{
                        **item,
                        "evidence": tuple(item.get("evidence", ())),
                    }
                )
                for item in data.get("items", [])
            ],
            reviews=[
                CalibrationReview(
                    reviewer=str(review["reviewer"]),
                    decision=ReviewDecision(review["decision"]),
                    comment=str(review.get("comment", "")),
                    created_utc=str(review.get("created_utc", _utc_now())),
                )
                for review in data.get("reviews", [])
            ],
        )

    def save(self, path: str | Path) -> Path:
        return _atomic_json(Path(path).expanduser().resolve(), self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> CalibrationChangeSet:
        source = Path(path).expanduser().resolve()
        return cls.from_dict(json.loads(source.read_text(encoding="utf-8")))


class CalibrationMemoryLayer(str, Enum):
    WORKING = "working"
    REFERENCE = "reference"
    RAM = "ram"
    ROM = "rom"


@dataclass(slots=True)
class CalibrationMemoryLedger:
    device: str
    snapshots: dict[CalibrationMemoryLayer, CalibrationDataset] = field(
        default_factory=dict
    )
    history: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        layer: CalibrationMemoryLayer | str,
        dataset: CalibrationDataset,
        *,
        actor: str,
        source: str = "",
        verified: bool = False,
    ) -> dict[str, Any]:
        layer = CalibrationMemoryLayer(layer)
        if not actor.strip():
            raise ValueError("记录存储层快照必须提供 actor")
        dataset.require_valid()
        snapshot = CalibrationDataset.from_dict(dataset.to_dict())
        self.snapshots[layer] = snapshot
        event = {
            "layer": layer.value,
            "digest": _dataset_content_digest(snapshot),
            "actor": actor.strip(),
            "source": source or snapshot.source,
            "verified": bool(verified),
            "created_utc": _utc_now(),
        }
        self.history.append(event)
        return event

    def transition_plan(
        self,
        source: CalibrationMemoryLayer | str,
        target: CalibrationMemoryLayer | str,
    ) -> dict[str, Any]:
        source = CalibrationMemoryLayer(source)
        target = CalibrationMemoryLayer(target)
        if source not in self.snapshots:
            raise KeyError(f"缺少 {source.value} 层快照")
        target_dataset = self.snapshots.get(target, CalibrationDataset())
        differences = target_dataset.diff(self.snapshots[source])
        return {
            "device": self.device,
            "source": source.value,
            "target": target.value,
            "direction": f"{source.value}->{target.value}",
            "change_count": len(differences),
            "differences": differences,
            "requires_hardware_action": source != target,
        }

    def status(self) -> dict[str, Any]:
        digests = {
            layer.value: _dataset_content_digest(dataset)
            for layer, dataset in self.snapshots.items()
        }

        def same(left: CalibrationMemoryLayer, right: CalibrationMemoryLayer) -> bool | None:
            if left not in self.snapshots or right not in self.snapshots:
                return None
            return _dataset_content_digest(
                self.snapshots[left]
            ) == _dataset_content_digest(self.snapshots[right])

        working_equals_ram = same(
            CalibrationMemoryLayer.WORKING, CalibrationMemoryLayer.RAM
        )
        ram_equals_rom = same(CalibrationMemoryLayer.RAM, CalibrationMemoryLayer.ROM)
        return {
            "device": self.device,
            "layers": digests,
            "working_equals_reference": same(
                CalibrationMemoryLayer.WORKING, CalibrationMemoryLayer.REFERENCE
            ),
            "working_equals_ram": working_equals_ram,
            "ram_equals_rom": ram_equals_rom,
            "ram_dirty": working_equals_ram is False,
            "persistent": ram_equals_rom is True,
            "history_count": len(self.history),
        }

    def require_persistent(self) -> None:
        status = self.status()
        if status["working_equals_ram"] is not True or status["ram_equals_rom"] is not True:
            raise SafetyViolationError("working、RAM 和 ROM 尚未形成一致持久化基线")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "snapshots": {
                layer.value: dataset.to_dict()
                for layer, dataset in self.snapshots.items()
            },
            "history": _json_value(self.history),
        }

    def save(self, path: str | Path) -> Path:
        return _atomic_json(Path(path).expanduser().resolve(), self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> CalibrationMemoryLedger:
        source = Path(path).expanduser().resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        return cls(
            device=str(data["device"]),
            snapshots={
                CalibrationMemoryLayer(name): CalibrationDataset.from_dict(dataset)
                for name, dataset in data.get("snapshots", {}).items()
            },
            history=list(data.get("history", [])),
        )


class ExperimentCaseStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExperimentEvidence:
    path: str
    sha256: str
    size: int

    @classmethod
    def from_file(cls, path: str | Path) -> ExperimentEvidence:
        source = Path(path).expanduser().resolve()
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return cls(str(source), digest.hexdigest(), source.stat().st_size)

    def verify(self) -> bool:
        source = Path(self.path)
        try:
            return (
                source.is_file()
                and source.stat().st_size == self.size
                and ExperimentEvidence.from_file(source).sha256 == self.sha256
            )
        except OSError:
            return False


@dataclass(slots=True)
class ExperimentCaseRecord:
    index: int
    parameters: dict[str, Any]
    status: ExperimentCaseStatus = ExperimentCaseStatus.PENDING
    metrics: dict[str, float] = field(default_factory=dict)
    evidence: list[ExperimentEvidence] = field(default_factory=list)
    error: str = ""
    attempts: int = 0
    started_utc: str = ""
    completed_utc: str = ""
    quality: dict[str, Any] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CalibrationExperimentStore:
    path: Path
    name: str
    device: str
    cases: list[ExperimentCaseRecord]
    identity: dict[str, str] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    lock_recoveries: list[dict[str, Any]] = field(default_factory=list)
    created_utc: str = field(default_factory=_utc_now)
    updated_utc: str = field(default_factory=_utc_now)
    quality_summary: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        name: str,
        device: str,
        cases: Sequence[Mapping[str, Any]],
        identity: Mapping[str, str] | None = None,
    ) -> CalibrationExperimentStore:
        target = Path(path).expanduser().resolve()
        if target.exists():
            raise FileExistsError(target)
        if not cases:
            raise ValueError("实验至少需要一个 case")
        store = cls(
            target,
            name,
            device,
            [
                ExperimentCaseRecord(index, _json_value(dict(parameters)))
                for index, parameters in enumerate(cases)
            ],
            identity=dict(identity or {}),
        )
        store.save()
        return store

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "name": self.name,
            "device": self.device,
            "identity": self.identity,
            "baseline": _json_value(self.baseline),
            "quality_summary": _json_value(self.quality_summary),
            "lock_recoveries": _json_value(self.lock_recoveries),
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
            "cases": [
                {
                    **asdict(case),
                    "status": case.status.value,
                    "evidence": [asdict(item) for item in case.evidence],
                }
                for case in self.cases
            ],
        }

    def save(self) -> Path:
        self.updated_utc = _utc_now()
        return _atomic_json(self.path, self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> CalibrationExperimentStore:
        source = Path(path).expanduser().resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        return cls(
            path=source,
            name=str(data["name"]),
            device=str(data["device"]),
            identity=dict(data.get("identity", {})),
            baseline=dict(data.get("baseline", {})),
            quality_summary=dict(data.get("quality_summary", {})),
            lock_recoveries=list(data.get("lock_recoveries", [])),
            created_utc=str(data.get("created_utc", _utc_now())),
            updated_utc=str(data.get("updated_utc", _utc_now())),
            cases=[
                ExperimentCaseRecord(
                    index=int(case["index"]),
                    parameters=dict(case["parameters"]),
                    status=ExperimentCaseStatus(case.get("status", "pending")),
                    metrics={
                        name: float(value)
                        for name, value in case.get("metrics", {}).items()
                    },
                    evidence=[
                        ExperimentEvidence(**evidence)
                        for evidence in case.get("evidence", [])
                    ],
                    quality=dict(case.get("quality", {})),
                    rejection_reasons=[
                        str(reason)
                        for reason in case.get("rejection_reasons", [])
                    ],
                    error=str(case.get("error", "")),
                    attempts=int(case.get("attempts", 0)),
                    started_utc=str(case.get("started_utc", "")),
                    completed_utc=str(case.get("completed_utc", "")),
                )
                for case in data.get("cases", [])
            ],
        )

    def recover_interrupted(self) -> int:
        recovered = 0
        for case in self.cases:
            if case.status is ExperimentCaseStatus.RUNNING:
                case.status = ExperimentCaseStatus.PENDING
                case.error = "上次运行中断，已恢复为 pending"
                recovered += 1
        if recovered:
            self.save()
        return recovered

    @property
    def run_lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".run.lock")

    @contextmanager
    def claim(self) -> Any:
        try:
            descriptor = os.open(
                self.run_lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except (FileExistsError, PermissionError) as exc:
            raise SafetyViolationError(
                f"实验已被其他运行器占用：{self.run_lock_path}"
            ) from exc
        try:
            os.write(
                descriptor,
                json.dumps(
                    {"pid": os.getpid(), "claimed_utc": _utc_now()}
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            self.run_lock_path.unlink(missing_ok=True)

    def recover_run_lock(self, *, actor: str, reason: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise ValueError("恢复实验锁必须提供 actor 和 reason")
        if not self.run_lock_path.exists():
            raise FileNotFoundError(self.run_lock_path)
        previous = self.run_lock_path.read_text(encoding="utf-8", errors="replace")
        event = {
            "actor": actor.strip(),
            "reason": reason.strip(),
            "previous_lock": previous,
            "recovered_utc": _utc_now(),
        }
        self.run_lock_path.unlink()
        self.lock_recoveries.append(event)
        self.save()
        return event

    def pending(self, *, retry_failed: bool = False) -> list[ExperimentCaseRecord]:
        statuses = {ExperimentCaseStatus.PENDING, ExperimentCaseStatus.RUNNING}
        if retry_failed:
            statuses.add(ExperimentCaseStatus.FAILED)
        return [case for case in self.cases if case.status in statuses]

    def summary(self) -> dict[str, Any]:
        counts = {status.value: 0 for status in ExperimentCaseStatus}
        invalid_evidence = []
        for case in self.cases:
            counts[case.status.value] += 1
            for evidence in case.evidence:
                if not evidence.verify():
                    invalid_evidence.append(
                        {"case": case.index, "path": evidence.path}
                    )
        return {
            "name": self.name,
            "device": self.device,
            "case_count": len(self.cases),
            "status_counts": counts,
            "completed": counts["passed"] + counts["rejected"],
            "invalid_evidence": invalid_evidence,
            "rejected_cases": [
                {
                    "case": case.index,
                    "reasons": list(case.rejection_reasons),
                }
                for case in self.cases
                if case.status is ExperimentCaseStatus.REJECTED
            ],
            "passed": (
                not invalid_evidence
                and counts["failed"] == 0
                and counts["pending"] == 0
                and counts["running"] == 0
            ),
        }


class CalibrationExperimentRunner:
    @staticmethod
    def run(
        backend: CalibrationBackend,
        store: CalibrationExperimentStore,
        evaluator: Callable[[int, Mapping[str, Any]], Mapping[str, float]],
        *,
        evidence_collector: (
            Callable[[int, Mapping[str, Any], Mapping[str, float]], Sequence[str | Path]]
            | None
        ) = None,
        retry_failed: bool = False,
        stop_on_error: bool = True,
        quality_policy: Any | None = None,
        stability_probe: (
            Callable[
                [int, Mapping[str, Any]],
                Sequence[Mapping[str, float]],
            ]
            | None
        ) = None,
    ) -> dict[str, Any]:
        if (
            quality_policy is not None
            and quality_policy.requires_environment_samples
            and stability_probe is None
        ):
            raise ValueError("质量策略包含稳态规则，必须提供 stability_probe")
        with store.claim():
            return CalibrationExperimentRunner._run_claimed(
                backend,
                store,
                evaluator,
                evidence_collector=evidence_collector,
                retry_failed=retry_failed,
                stop_on_error=stop_on_error,
                quality_policy=quality_policy,
                stability_probe=stability_probe,
            )

    @staticmethod
    def _run_claimed(
        backend: CalibrationBackend,
        store: CalibrationExperimentStore,
        evaluator: Callable[[int, Mapping[str, Any]], Mapping[str, float]],
        *,
        evidence_collector: (
            Callable[[int, Mapping[str, Any], Mapping[str, float]], Sequence[str | Path]]
            | None
        ),
        retry_failed: bool,
        stop_on_error: bool,
        quality_policy: Any | None,
        stability_probe: (
            Callable[
                [int, Mapping[str, Any]],
                Sequence[Mapping[str, float]],
            ]
            | None
        ),
    ) -> dict[str, Any]:
        names = sorted(
            {name for case in store.cases for name in case.parameters}
        )
        if not store.baseline:
            store.baseline = {
                name: _json_value(
                    backend.read_calibration_value(store.device, name)
                )
                for name in names
            }
            store.save()
        baseline = dict(store.baseline)
        completed_this_run = 0
        failures_this_run = 0
        rejected_this_run = 0
        outlier_results: list[dict[str, Any]] = []
        try:
            for case in store.pending(retry_failed=retry_failed):
                case.status = ExperimentCaseStatus.RUNNING
                case.attempts += 1
                case.started_utc = _utc_now()
                case.error = ""
                case.metrics = {}
                case.evidence = []
                case.quality = {}
                case.rejection_reasons = []
                store.save()
                try:
                    if quality_policy is not None and stability_probe is not None:
                        stability = quality_policy.evaluate_environment(
                            stability_probe(case.index, case.parameters)
                        )
                        case.quality["stability"] = stability
                        if not stability["passed"]:
                            case.status = ExperimentCaseStatus.REJECTED
                            case.rejection_reasons.extend(stability["issues"])
                            case.completed_utc = _utc_now()
                            completed_this_run += 1
                            rejected_this_run += 1
                            continue
                    for name, value in case.parameters.items():
                        backend.write_calibration_value(
                            store.device, name, value, verify=True
                        )
                    metrics = {
                        name: float(value)
                        for name, value in evaluator(
                            case.index, case.parameters
                        ).items()
                    }
                    evidence_paths = (
                        evidence_collector(case.index, case.parameters, metrics)
                        if evidence_collector
                        else ()
                    )
                    case.metrics = metrics
                    case.evidence = [
                        ExperimentEvidence.from_file(path)
                        for path in evidence_paths
                    ]
                    if quality_policy is not None:
                        acceptance = quality_policy.evaluate_metrics(metrics)
                        case.quality["metric_acceptance"] = acceptance
                        if not acceptance["passed"]:
                            case.status = ExperimentCaseStatus.REJECTED
                            case.rejection_reasons.extend(acceptance["issues"])
                            rejected_this_run += 1
                        else:
                            case.status = ExperimentCaseStatus.PASSED
                    else:
                        case.status = ExperimentCaseStatus.PASSED
                    case.completed_utc = _utc_now()
                    completed_this_run += 1
                except Exception as exc:
                    case.status = ExperimentCaseStatus.FAILED
                    case.error = str(exc)
                    case.completed_utc = _utc_now()
                    failures_this_run += 1
                    if stop_on_error:
                        raise
                finally:
                    for name, value in baseline.items():
                        backend.write_calibration_value(
                            store.device, name, value, verify=True
                        )
                    store.save()
            if quality_policy is not None:
                outlier_results = quality_policy.apply_outliers(store.cases)
                store.quality_summary["outliers"] = outlier_results
                store.quality_summary["updated_utc"] = _utc_now()
                rejected_this_run += sum(
                    len(result["outlier_indices"])
                    for result in outlier_results
                )
                store.save()
        finally:
            for name, value in baseline.items():
                backend.write_calibration_value(
                    store.device, name, value, verify=True
                )
        return {
            **store.summary(),
            "completed_this_run": completed_this_run,
            "failures_this_run": failures_this_run,
            "rejected_this_run": rejected_this_run,
            "outlier_results": outlier_results,
        }


@dataclass(frozen=True, slots=True)
class ECUCalibrationTask:
    device: str
    plan: CalibrationPlan


class MultiECUCalibrationCoordinator:
    @staticmethod
    def preview(tasks: Sequence[ECUCalibrationTask]) -> dict[str, Any]:
        devices = [task.device for task in tasks]
        duplicates = sorted(
            {device for device in devices if devices.count(device) > 1}
        )
        return {
            "device_count": len(devices),
            "devices": devices,
            "change_count": sum(len(task.plan.changes) for task in tasks),
            "approved": all(bool(task.plan.approved_by) for task in tasks),
            "duplicate_devices": duplicates,
            "passed": (
                bool(tasks)
                and not duplicates
                and all(task.plan.changes for task in tasks)
            ),
        }

    @classmethod
    def apply(
        cls,
        backend: CalibrationBackend,
        tasks: Sequence[ECUCalibrationTask],
        *,
        require_approval: bool = True,
        physical: bool = True,
    ) -> dict[str, Any]:
        preview = cls.preview(tasks)
        if not preview["passed"]:
            raise ValueError("多 ECU 任务为空或包含重复设备")
        if require_approval and not preview["approved"]:
            raise SafetyViolationError("至少一个 ECU 标定计划尚未审批")
        before: dict[str, dict[str, Any]] = {}
        for task in tasks:
            before[task.device] = {
                change.name: backend.read_calibration_value(
                    task.device, change.name, physical=physical
                )
                for change in task.plan.changes
            }
            for change in task.plan.changes:
                if change.enforce_expected and not _same_value(
                    before[task.device][change.name],
                    change.expected_before,
                ):
                    raise SafetyViolationError(
                        f"{task.device}/{change.name} 在线值与计划基线不一致"
                    )
        written: dict[str, dict[str, Any]] = {
            task.device: {} for task in tasks
        }
        attempted: list[tuple[str, str]] = []
        rollback_errors: list[str] = []
        try:
            for task in tasks:
                for change in task.plan.changes:
                    attempted.append((task.device, change.name))
                    written[task.device][change.name] = (
                        backend.write_calibration_value(
                            task.device,
                            change.name,
                            change.value,
                            physical=physical,
                            verify=True,
                        )
                    )
        except Exception:
            for device, name in reversed(attempted):
                try:
                    backend.write_calibration_value(
                        device,
                        name,
                        before[device][name],
                        physical=physical,
                        verify=True,
                    )
                except Exception as rollback_error:
                    rollback_errors.append(f"{device}/{name}: {rollback_error}")
            if rollback_errors:
                raise SafetyViolationError(
                    "多 ECU 标定失败且回滚不完整：" + "; ".join(rollback_errors)
                ) from None
            raise
        return {
            "status": "applied",
            "devices": [task.device for task in tasks],
            "before": before,
            "after": written,
            "plans": {task.device: task.plan.name for task in tasks},
            "applied_utc": _utc_now(),
        }


@dataclass(frozen=True, slots=True)
class CalibrationObjective:
    name: str
    direction: str = "minimize"

    def validate(self) -> None:
        if self.direction not in {"minimize", "maximize"}:
            raise ValueError(f"{self.name} direction 必须是 minimize/maximize")


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    identifier: str
    parameters: dict[str, float]
    metrics: dict[str, float]
    evidence: tuple[str, ...] = ()


class ParetoCalibrationAnalysis:
    @staticmethod
    def _dominates(
        left: CalibrationCandidate,
        right: CalibrationCandidate,
        objectives: Sequence[CalibrationObjective],
    ) -> bool:
        no_worse = True
        strictly_better = False
        for objective in objectives:
            left_value = left.metrics[objective.name]
            right_value = right.metrics[objective.name]
            if objective.direction == "minimize":
                no_worse &= left_value <= right_value
                strictly_better |= left_value < right_value
            else:
                no_worse &= left_value >= right_value
                strictly_better |= left_value > right_value
        return no_worse and strictly_better

    @classmethod
    def analyze(
        cls,
        candidates: Sequence[CalibrationCandidate],
        objectives: Sequence[CalibrationObjective],
        *,
        safety_limits: Mapping[str, tuple[float | None, float | None]] | None = None,
    ) -> dict[str, Any]:
        if not candidates or not objectives:
            raise ValueError("Pareto 分析需要候选和目标")
        for objective in objectives:
            objective.validate()
        required_metrics = {objective.name for objective in objectives}
        accepted = []
        rejected = []
        for candidate in candidates:
            missing = required_metrics - set(candidate.metrics)
            if missing:
                raise KeyError(
                    f"{candidate.identifier} 缺少指标：{', '.join(sorted(missing))}"
                )
            violations = []
            for name, (minimum, maximum) in (safety_limits or {}).items():
                if name not in candidate.metrics:
                    violations.append(f"缺少安全指标 {name}")
                    continue
                value = candidate.metrics[name]
                if minimum is not None and value < minimum:
                    violations.append(f"{name}={value} < {minimum}")
                if maximum is not None and value > maximum:
                    violations.append(f"{name}={value} > {maximum}")
            if violations:
                rejected.append(
                    {"identifier": candidate.identifier, "violations": violations}
                )
            else:
                accepted.append(candidate)
        front = [
            candidate
            for candidate in accepted
            if not any(
                cls._dominates(other, candidate, objectives)
                for other in accepted
                if other is not candidate
            )
        ]
        return {
            "passed": bool(front),
            "accepted_count": len(accepted),
            "rejected": rejected,
            "pareto_front": [
                {
                    "identifier": candidate.identifier,
                    "parameters": candidate.parameters,
                    "metrics": candidate.metrics,
                    "evidence": list(candidate.evidence),
                }
                for candidate in front
            ],
        }

    @staticmethod
    def select_balanced(
        pareto_front: Sequence[CalibrationCandidate],
        objectives: Sequence[CalibrationObjective],
    ) -> CalibrationCandidate:
        if not pareto_front:
            raise ValueError("Pareto 前沿为空")
        ranges = {}
        for objective in objectives:
            values = [item.metrics[objective.name] for item in pareto_front]
            ranges[objective.name] = (min(values), max(values))

        def distance(candidate: CalibrationCandidate) -> float:
            total = 0.0
            for objective in objectives:
                minimum, maximum = ranges[objective.name]
                span = maximum - minimum
                if span == 0:
                    continue
                value = candidate.metrics[objective.name]
                normalized = (
                    (value - minimum) / span
                    if objective.direction == "minimize"
                    else (maximum - value) / span
                )
                total += normalized * normalized
            return total

        return min(pareto_front, key=lambda candidate: (distance(candidate), candidate.identifier))
