"""Automatic catalog of every direct ``vk.send_message`` text callsite.

The catalog is derived from active bot source files, so the admin panel never
shows texts that are no longer sent. Dynamic expressions fall back to
``{default}``, preserving the fully rendered original while still allowing an
administrator to replace it or add text around it.
"""
from __future__ import annotations

import ast
import hashlib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_FILES = {"vk_client.py", "keyboards.py", "main.py"}
SKIP_FUNCTIONS = {"send_bot_message", "show_start"}


def callsite_key(filename: str, function: str, line: int) -> str:
    stem = Path(filename).stem
    raw = f"auto.{stem}.{function}.{int(line)}"
    if len(raw) <= 80:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"auto.{stem[:18]}.{function[:35]}.{digest}"[:80]


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Subscript):
        base = _dotted(node.value)
        if base and isinstance(node.slice, ast.Constant):
            return f"{base}[{node.slice.value!r}]"
    return None


def _template(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if not isinstance(value, ast.FormattedValue):
                return "{default}"
            field = _dotted(value.value)
            if not field:
                return "{default}"
            conversion = "" if value.conversion == -1 else "!" + chr(value.conversion)
            spec = ""
            if value.format_spec is not None:
                if not isinstance(value.format_spec, ast.JoinedStr) or not all(
                    isinstance(item, ast.Constant) for item in value.format_spec.values
                ):
                    return "{default}"
                spec = ":" + "".join(str(item.value) for item in value.format_spec.values)
            parts.append("{" + field + conversion + spec + "}")
        return "".join(parts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return node.func.value.value
    return "{default}"


def _function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "module"


def _is_already_editable(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name) and node.func.id == "msg":
        return True
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id == "bm" and node.func.attr in {"render", "get_message"}
    return False


@lru_cache(maxsize=1)
def discover() -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for path in sorted((ROOT / "bot").glob("*.py")):
        if path.name in SKIP_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "vk"
                and node.func.attr == "send_message"
            ):
                continue
            text_node = node.args[1] if len(node.args) > 1 else next(
                (kw.value for kw in node.keywords if kw.arg == "text"), None
            )
            if text_node is None or _is_already_editable(text_node):
                continue
            function = _function_name(node, parents)
            if function in SKIP_FUNCTIONS:
                continue
            key = callsite_key(str(path), function, node.lineno)
            title = f"{path.stem} · {function} · строка {node.lineno}"
            result[key] = (title, _template(text_node))
    return result
