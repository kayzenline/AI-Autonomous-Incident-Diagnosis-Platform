"""Static checks that the rule baseline cannot see the answer key."""

import ast
import inspect
from pathlib import Path

from app.agent import baseline


def assert_baseline_is_isolated() -> None:
    source_path = Path(inspect.getsourcefile(baseline) or "")
    source = source_path.read_text(encoding="utf-8")
    if "ground_truth" in source or "user_severity" in source:
        raise RuntimeError("rule baseline references ground_truth or user_severity")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module.startswith("app.eval") or "simulator" in module:
            raise RuntimeError(f"rule baseline imports {module}")
