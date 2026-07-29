"""YAML/JSON 工程任务编排、重试、检查点、Dry-run 和批量回归。"""

from __future__ import annotations

import json
import os
import time
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


@dataclass(slots=True)
class WorkflowResult:
    status: str
    steps: list[StepResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


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
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            raise WorkflowError("工作流必须是包含 steps 数组的对象")
        return data

    @staticmethod
    def merge_variables(
        definition: Mapping[str, Any],
        *,
        overrides: Mapping[str, Any] | None = None,
        environment_prefix: str = "PY_CANAPE_VAR_",
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
                    "steps": [asdict(item) for item in result.steps],
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
        checkpoint: str | os.PathLike[str] | None = None,
        resume: bool = False,
    ) -> WorkflowResult:
        context = {
            "variables": self.merge_variables(definition, overrides=variables),
            "steps": {},
        }
        completed: set[str] = set()
        checkpoint_path = Path(checkpoint).resolve() if checkpoint else None
        if resume and checkpoint_path and checkpoint_path.is_file():
            previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            context.update(previous.get("context", {}))
            completed = {
                item["id"]
                for item in previous.get("steps", [])
                if item.get("status") == "passed"
            }
        result = WorkflowResult(status="running", context=context)
        for index, step in enumerate(definition["steps"], start=1):
            step_id = str(step.get("id", f"step_{index}"))
            action = str(step["action"])
            if step_id in completed:
                continue
            handler = self.handlers.get(action)
            if handler is None:
                raise WorkflowError(f"未注册工作流动作：{action}")
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
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(handler.function, **arguments)
                            output = future.result(timeout=float(timeout))
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
                )
                result.steps.append(step_result)
                context["steps"][step_id] = asdict(step_result)
                result.status = "failed"
                if checkpoint_path:
                    self._save_checkpoint(checkpoint_path, result)
                if step.get("continue_on_error", False):
                    continue
                return result
            step_result = StepResult(
                step_id,
                action,
                "passed",
                started_utc,
                duration,
                attempts,
                output=output,
            )
            result.steps.append(step_result)
            context["steps"][step_id] = asdict(step_result)
            for condition in step.get("postconditions", ()):
                if not self._condition(condition, context):
                    raise WorkflowError(f"{step_id} 后置条件不满足：{condition}")
            if checkpoint_path:
                self._save_checkpoint(checkpoint_path, result)
        result.status = "passed"
        if checkpoint_path:
            self._save_checkpoint(checkpoint_path, result)
        return result

    def batch(
        self,
        definition: Mapping[str, Any],
        parameter_sets: Sequence[Mapping[str, Any]],
        *,
        dry_run: bool = False,
    ) -> list[WorkflowResult]:
        return [
            self.execute(definition, variables=parameters, dry_run=dry_run)
            for parameters in parameter_sets
        ]
