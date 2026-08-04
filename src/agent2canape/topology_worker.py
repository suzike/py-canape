"""拓扑数据库语义审计的隔离子进程入口。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .topology import DatabaseTopologySpec, NetworkTopologyManifest


def main() -> int:
    try:
        payload: dict[str, Any] = json.loads(
            sys.stdin.buffer.read().decode("utf-8")
        )
        path = Path(payload["path"])
        database = DatabaseTopologySpec.from_mapping(payload["database"])
        kind = database.resolved_kind()
        if kind == "dbc":
            semantic, errors, warnings = NetworkTopologyManifest._audit_dbc(
                path, database
            )
        elif kind == "a2l":
            semantic, errors = NetworkTopologyManifest._audit_a2l(path, database)
            warnings = []
        else:
            semantic = {}
            errors = [f"{database.name} 数据库类型不支持隔离语义审计：{kind}"]
            warnings = []
        output = json.dumps(
            {"semantic": semantic, "errors": errors, "warnings": warnings},
            ensure_ascii=False,
        )
        sys.stdout.buffer.write((output + "\n").encode("utf-8"))
        return 0 if not errors else 1
    except Exception as exc:
        output = json.dumps(
            {
                "semantic": {},
                "errors": [f"隔离语义审计失败：{type(exc).__name__}: {exc}"],
                "warnings": [],
            },
            ensure_ascii=False,
        )
        sys.stdout.buffer.write((output + "\n").encode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
