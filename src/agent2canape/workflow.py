"""YAML/JSON 工程任务编排、重试、检查点、Dry-run 和批量回归。"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WorkflowError


@dataclass(frozen=True, slots=True)
class Handler:
    function: Callable[..., Any]
    write: bool = False
    thread_safe: bool = True


@dataclass(slots=True)
class StepResult:
    id: str
    action: str
    status: str
    started_utc: str
    duration: float
    attempts: int
    output: Any = None
    error: str | None = None
    error_category: str | None = None
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowResult:
    status: str
    run_id: str = ""
    operator: str = ""
    started_utc: str = ""
    steps: list[StepResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    compensation_steps: list[StepResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "passed" else 1

    def machine_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "operator": self.operator,
            "status": self.status,
            "exit_code": self.exit_code,
            "passed": sum(item.status == "passed" for item in self.steps),
            "failed": sum(item.status == "failed" for item in self.steps),
            "artifacts": [
                artifact for item in self.steps for artifact in item.artifacts
            ],
        }


class WorkflowEngine:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}

    def register(
        self,
        name: str,
        function: Callable[..., Any],
        *,
        write: bool = False,
        thread_safe: bool = True,
    ) -> None:
        if name in self.handlers:
            raise ValueError(f"工作流动作重复：{name}")
        self.handlers[name] = Handler(function, write, thread_safe)

    @staticmethod
    def load(path: str | os.PathLike[str]) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        if source.suffix.casefold() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise WorkflowError("YAML 工作流需要安装 PyYAML") from exc
            data = yaml.safe_load(source.read_text(encoding="utf-8"))
        else:
            data = json.loads(source.read_text(encoding="utf-8"))
        WorkflowEngine.validate_definition(data)
        return data

    @staticmethod
    def validate_definition(definition: Any) -> None:
        if not isinstance(definition, Mapping) or not isinstance(
            definition.get("steps"), list
        ):
            raise WorkflowError("工作流必须是包含 steps 数组的对象")
        identifiers: set[str] = set()
        for index, step in enumerate(definition["steps"], start=1):
            if not isinstance(step, Mapping):
                raise WorkflowError(f"第 {index} 个步骤必须是对象")
            if not str(step.get("action", "")).strip():
                raise WorkflowError(f"第 {index} 个步骤缺少 action")
            step_id = str(step.get("id", f"step_{index}"))
            if step_id in identifiers:
                raise WorkflowError(f"工作流步骤 id 重复：{step_id}")
            identifiers.add(step_id)
            if "arguments" in step:
                raise WorkflowError(
                    f"{step_id} 使用了未知字段 arguments；工作流参数字段应为 with"
                )
            if "with" in step and not isinstance(step["with"], Mapping):
                raise WorkflowError(f"{step_id}.with 必须是对象")
            if "timeout" in step and float(step["timeout"]) <= 0:
                raise WorkflowError(f"{step_id}.timeout 必须大于 0")
            if "retries" in step and int(step["retries"]) < 0:
                raise WorkflowError(f"{step_id}.retries 不能为负数")

    @staticmethod
    def merge_variables(
        definition: Mapping[str, Any],
        *,
        overrides: Mapping[str, Any] | None = None,
        environment_prefix: str = "AGENT2CANAPE_VAR_",
    ) -> dict[str, Any]:
        variables = dict(definition.get("variables", {}))
        for key, value in os.environ.items():
            if key.startswith(environment_prefix):
                variables[key[len(environment_prefix) :].casefold()] = value
        variables.update(overrides or {})
        return variables

    @classmethod
    def _resolve(cls, value: Any, context: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            if value.startswith("${") and value.endswith("}"):
                path = value[2:-1].split(".")
                current: Any = context
                for key in path:
                    current = current[key]
                return current
            return value
        if isinstance(value, list):
            return [cls._resolve(item, context) for item in value]
        if isinstance(value, dict):
            return {key: cls._resolve(item, context) for key, item in value.items()}
        return value

    @staticmethod
    def _condition(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
        path = str(condition["path"]).split(".")
        value: Any = context
        for key in path:
            value = value[key]
        operator = condition.get("operator", "eq")
        expected = condition.get("value")
        operations = {
            "eq": lambda: value == expected,
            "ne": lambda: value != expected,
            "lt": lambda: value < expected,
            "le": lambda: value <= expected,
            "gt": lambda: value > expected,
            "ge": lambda: value >= expected,
            "in": lambda: value in expected,
            "not_in": lambda: value not in expected,
            "truthy": lambda: bool(value),
        }
        if operator not in operations:
            raise WorkflowError(f"未知条件操作符：{operator}")
        return bool(operations[operator]())

    @staticmethod
    def _save_checkpoint(path: Path, result: WorkflowResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": result.status,
                    "run_id": result.run_id,
                    "operator": result.operator,
                    "started_utc": result.started_utc,
                    "steps": [asdict(item) for item in result.steps],
                    "compensation_steps": [
                        asdict(item) for item in result.compensation_steps
                    ],
                    "context": result.context,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def execute(
        self,
        definition: Mapping[str, Any],
        *,
        variables: Mapping[str, Any] | None = None,
        dry_run: bool = False,
        allow_writes: bool = False,
        checkpoint: str | os.PathLike[str] | None = None,
        resume: bool = False,
        operator: str = "",
    ) -> WorkflowResult:
        self.validate_definition(definition)
        run_id = str(uuid.uuid4())
        started_utc = datetime.now(timezone.utc).isoformat()
        context = {
            "variables": self.merge_variables(definition, overrides=variables),
            "steps": {},
            "run": {"id": run_id, "operator": operator, "started_utc": started_utc},
        }
        completed: set[str] = set()
        checkpoint_path = Path(checkpoint).resolve() if checkpoint else None
        if resume and checkpoint_path and checkpoint_path.is_file():
            previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            context.update(previous.get("context", {}))
            context["run"] = {
                "id": run_id,
                "operator": operator,
                "started_utc": started_utc,
                "resumed_from": previous.get("run_id", ""),
            }
            completed = {
                item["id"]
                for item in previous.get("steps", [])
                if item.get("status") == "passed"
            }
        result = WorkflowResult(
            status="running",
            run_id=run_id,
            operator=operator,
            started_utc=started_utc,
            context=context,
        )
        compensations: list[tuple[str, str, dict[str, Any]]] = []
        had_continued_failures = False
        for index, step in enumerate(definition["steps"], start=1):
            step_id = str(step.get("id", f"step_{index}"))
            action = str(step["action"])
            if step_id in completed:
                continue
            handler = self.handlers.get(action)
            if handler is None:
                raise WorkflowError(f"未注册工作流动作：{action}")
            if handler.write and not dry_run and not allow_writes:
                raise WorkflowError(
                    f"{step_id} 是写操作；必须显式传入 allow_writes=True"
                )
            for condition in step.get("preconditions", ()):
                if not self._condition(condition, context):
                    raise WorkflowError(f"{step_id} 前置条件不满足：{condition}")
            arguments = self._resolve(step.get("with", {}), context)
            start = time.monotonic()
            started_utc = datetime.now(timezone.utc).isoformat()
            if dry_run:
                step_result = StepResult(
                    step_id,
                    action,
                    "dry-run-write" if handler.write else "dry-run",
                    started_utc,
                    0.0,
                    0,
                    output={"arguments": arguments},
                    artifacts=list(step.get("artifacts", ())),
                )
                result.steps.append(step_result)
                context["steps"][step_id] = asdict(step_result)
                continue
            retries = max(0, int(step.get("retries", 0)))
            backoff = max(0.0, float(step.get("backoff", 0.0)))
            timeout = step.get("timeout")
            last_error: Exception | None = None
            output: Any = None
            attempts = 0
            for attempt in range(retries + 1):
                attempts = attempt + 1
                try:
                    if timeout is not None and handler.thread_safe:
                        executor = ThreadPoolExecutor(max_workers=1)
                        try:
                            future = executor.submit(handler.function, **arguments)
                            output = future.result(timeout=float(timeout))
                        finally:
                            executor.shutdown(wait=False, cancel_futures=True)
                    else:
                        output = handler.function(**arguments)
                        if timeout is not None and time.monotonic() - start > float(timeout):
                            raise WorkflowError(
                                f"{step_id} 超时；该动作不支持强制线程中断"
                            )
                    last_error = None
                    break
                except TimeoutError:
                    last_error = WorkflowError(f"{step_id} 超时")
                except Exception as exc:  # noqa: PERF203
                    last_error = exc
                if attempt < retries:
                    time.sleep(backoff * (2**attempt))
            duration = time.monotonic() - start
            if last_error is not None:
                step_result = StepResult(
                    step_id,
                    action,
                    "failed",
                    started_utc,
                    duration,
                    attempts,
                    error=str(last_error),
                    error_category=self._classify_error(last_error),
                    artifacts=list(step.get("artifacts", ())),
                )
                result.steps.append(step_result)
                context["steps"][step_id] = asdict(step_result)
                result.status = "failed"
                if step.get("continue_on_error", False):
                    had_continued_failures = True
                    if checkpoint_path:
                        self._save_checkpoint(checkpoint_path, result)
                    continue
                self._compensate(compensations, result)
                if checkpoint_path:
                    self._save_checkpoint(checkpoint_path, result)
                return result
            step_result = StepResult(
                step_id,
                action,
                "passed",
                started_utc,
                duration,
                attempts,
                output=output,
                artifacts=[
                    str(self._resolve(value, context))
                    for value in step.get("artifacts", ())
                ],
            )
            result.steps.append(step_result)
            context["steps"][step_id] = asdict(step_result)
            compensation = step.get("compensate")
            if compensation:
                compensations.append(
                    (
                        step_id,
                        str(compensation["action"]),
                        self._resolve(compensation.get("with", {}), context),
                    )
                )
            for condition in step.get("postconditions", ()):
                if not self._condition(condition, context):
                    step_result.status = "failed"
                    step_result.error = f"{step_id} 后置条件不满足：{condition}"
                    step_result.error_category = "validation"
                    context["steps"][step_id] = asdict(step_result)
                    result.status = "failed"
                    self._compensate(compensations, result)
                    if checkpoint_path:
                        self._save_checkpoint(checkpoint_path, result)
                    return result
            if checkpoint_path:
                self._save_checkpoint(checkpoint_path, result)
        result.status = "completed_with_errors" if had_continued_failures else "passed"
        if checkpoint_path:
            self._save_checkpoint(checkpoint_path, result)
        return result

    @staticmethod
    def _classify_error(error: Exception) -> str:
        if isinstance(error, TimeoutError) or "超时" in str(error):
            return "timeout"
        if isinstance(error, (PermissionError,)):
            return "permission"
        if isinstance(error, (FileNotFoundError, KeyError)):
            return "dependency"
        if isinstance(error, ValueError):
            return "validation"
        return "execution"

    def _compensate(
        self,
        compensations: Sequence[tuple[str, str, dict[str, Any]]],
        result: WorkflowResult,
    ) -> None:
        for source_step, action, arguments in reversed(compensations):
            started = datetime.now(timezone.utc).isoformat()
            start = time.monotonic()
            handler = self.handlers.get(action)
            if handler is None:
                result.compensation_steps.append(
                    StepResult(
                        f"compensate_{source_step}",
                        action,
                        "failed",
                        started,
                        0.0,
                        0,
                        error=f"未注册补偿动作：{action}",
                        error_category="dependency",
                    )
                )
                continue
            try:
                output = handler.function(**arguments)
                result.compensation_steps.append(
                    StepResult(
                        f"compensate_{source_step}",
                        action,
                        "passed",
                        started,
                        time.monotonic() - start,
                        1,
                        output=output,
                    )
                )
            except Exception as exc:
                result.compensation_steps.append(
                    StepResult(
                        f"compensate_{source_step}",
                        action,
                        "failed",
                        started,
                        time.monotonic() - start,
                        1,
                        error=str(exc),
                        error_category=self._classify_error(exc),
                    )
                )

    @staticmethod
    def multi_ecu_definition(
        ecu_names: Sequence[str],
        *,
        actions: Sequence[str] = ("device.online", "measurement.configure"),
    ) -> dict[str, Any]:
        """Create a deterministic multi-ECU orchestration skeleton."""
        steps = []
        for action in actions:
            for ecu in ecu_names:
                steps.append(
                    {
                        "id": f"{action.replace('.', '_')}_{ecu}",
                        "action": action,
                        "with": {"device": ecu},
                    }
                )
        return {"name": "multi-ecu-orchestration", "steps": steps}

    @staticmethod
    def ci_summary(result: WorkflowResult) -> dict[str, Any]:
        return result.machine_summary()

    def batch(
        self,
        definition: Mapping[str, Any],
        parameter_sets: Sequence[Mapping[str, Any]],
        *,
        dry_run: bool = False,
        allow_writes: bool = False,
    ) -> list[WorkflowResult]:
        return [
            self.execute(
                definition,
                variables=parameters,
                dry_run=dry_run,
                allow_writes=allow_writes,
            )
            for parameters in parameter_sets
        ]
