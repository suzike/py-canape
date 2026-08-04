"""Agent2Canape MCP stdio 服务器。"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any

from .ai_tools import ApprovalStore, CANapeAIToolkit
from .canape import CANape
from .errors import OptionalDependencyError


def create_server(
    *,
    canape: CANape | None = None,
    approval_file: str | Path | None = None,
    default_project: str | Path | None = None,
    tool_allowlist: str | list[str] | tuple[str, ...] | None = None,
) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise OptionalDependencyError(
            "MCP Server 需要安装 Agent2Canape[ai]"
        ) from exc

    approval_path = approval_file or os.getenv("AGENT2CANAPE_APPROVAL_STORE")
    project_path = default_project or os.getenv("AGENT2CANAPE_DEFAULT_PROJECT")
    toolkit = CANapeAIToolkit(
        canape or CANape(),
        approvals=ApprovalStore(approval_path),
        default_project=project_path,
    )
    manifest = toolkit.registry.manifest()
    configured_allowlist = (
        tool_allowlist
        if tool_allowlist is not None
        else os.getenv("AGENT2CANAPE_MCP_TOOL_ALLOWLIST")
    )
    allowed_names: set[str] | None = None
    if configured_allowlist:
        raw_names = (
            configured_allowlist.split(",")
            if isinstance(configured_allowlist, str)
            else configured_allowlist
        )
        allowed_names = {str(name).strip() for name in raw_names if str(name).strip()}
        registered_names = {item["name"] for item in manifest}
        unknown_names = sorted(allowed_names - registered_names)
        if unknown_names:
            raise ValueError(
                "AGENT2CANAPE_MCP_TOOL_ALLOWLIST 包含未知工具："
                + ", ".join(unknown_names)
            )
        manifest = [item for item in manifest if item["name"] in allowed_names]

    server = FastMCP(
        "Agent2Canape",
        instructions=(
            "面向 ECU 标定、测量、诊断和刷写的 CANape 工程工具。"
            "任何非只读工具必须先 dry_run 生成 Action Plan，"
            "由外部用户审批后使用同一参数和 action_plan_id 执行。"
            "标定写入计划包含实时当前值、目标值、差异、校验与恢复快照；"
            "执行前置状态变化时必须重新规划。"
        ),
    )

    @server.tool()
    def agent2canape_tool_manifest() -> list[dict[str, Any]]:
        """列出可用工程工具、输入 Schema、风险和审批要求。"""
        return manifest

    @server.tool()
    def agent2canape_plan_natural_language(
        request: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """结合对象别名、ECU 和单位上下文生成工具参数；不会执行操作。"""
        plan = toolkit.planner.plan(request, context=context)
        if (
            allowed_names is not None
            and plan.get("tool")
            and plan["tool"] not in allowed_names
        ):
            return {
                "status": "not_exposed",
                "text": request,
                "tool": plan["tool"],
                "message": "该工具未包含在当前 MCP 工具允许列表中",
            }
        return plan

    def expose(item: dict[str, Any]) -> None:
        name = item["name"]
        description = item["description"]
        schema = item["input_schema"]
        required = set(schema.get("required", ()))
        type_map = {
            "string": str,
            "number": float,
            "integer": int,
            "boolean": bool,
            "array": list[Any],
            "object": dict[str, Any],
            "any": Any,
        }

        def invoke(**kwargs: Any) -> dict[str, Any]:
            dry_run = bool(kwargs.pop("dry_run", True))
            action_plan_id = str(kwargs.pop("action_plan_id", ""))
            arguments = {
                key: value for key, value in kwargs.items() if value is not None
            }
            return toolkit.registry.invoke(
                name,
                arguments,
                dry_run=dry_run,
                action_plan_id=action_plan_id,
            )

        invoke.__name__ = f"agent2canape_{name}"
        invoke.__doc__ = (
            f"{description} 非只读操作默认只生成计划；"
            "执行时需传 dry_run=false 和已批准的 action_plan_id。"
            f" 输入 Schema：{json.dumps(schema, ensure_ascii=False)}"
        )
        parameters = []
        properties = schema.get("properties", {})
        for parameter_name in sorted(properties, key=lambda key: key not in required):
            property_schema = properties[parameter_name]
            parameters.append(
                inspect.Parameter(
                    parameter_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=(
                        inspect.Parameter.empty
                        if parameter_name in required
                        else None
                    ),
                    annotation=type_map.get(property_schema.get("type"), Any),
                )
            )
        parameters.extend(
            (
                inspect.Parameter(
                    "dry_run",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=True,
                    annotation=bool,
                ),
                inspect.Parameter(
                    "action_plan_id",
                    inspect.Parameter.KEYWORD_ONLY,
                    default="",
                    annotation=str,
                ),
            )
        )
        invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters,
            return_annotation=dict[str, Any],
        )
        server.tool(name=f"agent2canape_{name}", description=invoke.__doc__)(invoke)

    for item in manifest:
        expose(item)
    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
