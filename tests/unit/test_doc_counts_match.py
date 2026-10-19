"""tests/unit/test_doc_counts_match.py

Doc numbers must match the code: README source counts and CLAUDE.md file line counts.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "doc_counts", REPO_ROOT / "scripts" / "doc_counts.py"
)
doc_counts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doc_counts)

pytestmark = pytest.mark.unit


def test_readme_source_counts_match_registry():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    quoted = {int(n) for n in re.findall(r"\b(\d+) (?:data )?sources\b", readme)}
    assert quoted, "README no longer states a source count"
    assert quoted == {doc_counts.source_count()}


def test_claude_md_line_counts_match_files():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    stated = {m.group(1): int(m.group(2)) for m in doc_counts._ROW.finditer(text)}
    assert stated
    assert stated == doc_counts.line_counts()


def _registered_mcp_tools() -> set[str]:
    import ast

    tools = set()
    for path in (REPO_ROOT / "dango" / "cli" / "commands").glob("mcp_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                "tool" in ast.dump(d) for d in node.decorator_list
            ):
                tools.add(node.name)
    return tools


def test_readme_mcp_table_names_registered_tools():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### MCP tools by blast radius", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "`" in line]
    named = set()
    for row in rows:
        first_cell = row.split("|")[1]
        named |= set(re.findall(r"`([a-z_]+)`", first_cell))
    registered = _registered_mcp_tools()
    assert not {n for n in named if "_" in n and not n.endswith("_")} - registered
