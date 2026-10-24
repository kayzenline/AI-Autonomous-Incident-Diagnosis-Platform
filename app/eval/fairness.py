"""Static checks that diagnosis agents cannot see the answer key."""

import ast
import inspect
from pathlib import Path
from types import ModuleType

from app.agent import baseline, investigator, ledger, single_shot, verifier
from app.llm.adapters import fake, investigator_fake, openai


def assert_baseline_is_isolated() -> None:
    for mod in (
        baseline,
        single_shot,
        investigator,
        ledger,
        verifier,
        fake,
        investigator_fake,
        openai,
    ):
        _assert_module_is_isolated(mod)


def _assert_module_is_isolated(mod: ModuleType) -> None:
    source_path = Path(inspect.getsourcefile(mod) or "")
    source = source_path.read_text(encoding="utf-8")
    if "ground_truth" in source or "user_severity" in source:
        raise RuntimeError(f"{mod.__name__} references ground_truth or user_severity")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module.startswith("app.eval") or "simulator" in module:
            raise RuntimeError(f"{mod.__name__} imports {module}")
