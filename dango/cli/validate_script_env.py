"""dango/cli/validate_script_env.py

Static checks for user scripts under ``scripts/`` run by ``dango validate``.

Scripts started on a cloud server with isolation receive only the environment
names declared in ``.dango/scripts.yml`` and cannot write the warehouse unless
declared ``warehouse: write``. These checks read each script with ``ast`` (no
execution, no import) and report, as warnings only:

* an environment variable read by a literal name that is not declared,
* a declared name that is missing from ``.env``,
* an apparent warehouse write in a script not declared ``warehouse: write``.

Results are ``(name, status, message)`` tuples; status is ``pass`` or ``warn``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

__all__ = ["check_script_env"]

Result = tuple[str, str, str]

_WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|CREATE\s+(OR\s+REPLACE\s+)?(TABLE|VIEW|SCHEMA)|DELETE\s+FROM"
    r"|DROP\s+(TABLE|VIEW|SCHEMA)|ALTER\s+TABLE|UPDATE\s+\S+\s+SET|COPY\b[^;]*\bTO\b)",
    re.IGNORECASE,
)


def _provided_names() -> frozenset[str]:
    """Names the isolated launcher always puts in a script's environment."""
    from dango.platform.run_as import build_env

    return frozenset(build_env("script", Path("/"), {}, run_id="x", tz="UTC"))


def _is_declarable(name: str) -> bool:
    from dango.config.scripts import check_env_name

    try:
        check_env_name(name)
    except ValueError:
        return False
    return True


class _Scan(ast.NodeVisitor):
    """Collects literal env reads and duckdb connection / write signals."""

    def __init__(self) -> None:
        self.os_names: set[str] = set()
        self.environ_names: set[str] = set()
        self.getenv_names: set[str] = set()
        self.duckdb_names: set[str] = set()
        self.connect_names: set[str] = set()
        self.env_reads: dict[str, None] = {}
        self.writable_connect = False
        self.write_sql = False
        self.uses_lock = False

    def visit_Import(self, node: ast.Import) -> None:
        """Track aliases of ``os`` and ``duckdb``."""
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if alias.name == "os":
                self.os_names.add(bound)
            elif alias.name == "duckdb":
                self.duckdb_names.add(bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Track ``from os import environ/getenv`` and ``from duckdb import connect``."""
        for alias in node.names:
            bound = alias.asname or alias.name
            if node.module == "os" and alias.name == "environ":
                self.environ_names.add(bound)
            elif node.module == "os" and alias.name == "getenv":
                self.getenv_names.add(bound)
            elif node.module == "duckdb" and alias.name == "connect":
                self.connect_names.add(bound)
            if alias.name == "DbtLock":
                self.uses_lock = True

    def _is_environ(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.environ_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.os_names
        )

    def _read(self, key: ast.expr | None) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self.env_reads[key.value] = None

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Record ``environ["X"]`` reads."""
        if isinstance(node.ctx, ast.Load) and self._is_environ(node.value):
            self._read(node.slice)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Note a ``DbtLock`` reference."""
        if node.id == "DbtLock":
            self.uses_lock = True

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Note a ``DbtLock`` reference."""
        if node.attr == "DbtLock":
            self.uses_lock = True
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """Note SQL string literals that write."""
        if isinstance(node.value, str) and _WRITE_SQL.search(node.value):
            self.write_sql = True

    def visit_Call(self, node: ast.Call) -> None:
        """Record ``getenv``/``environ.get`` reads and ``duckdb.connect`` calls."""
        func = node.func
        first = node.args[0] if node.args else None
        if isinstance(func, ast.Attribute):
            if func.attr == "get" and self._is_environ(func.value):
                self._read(first)
            elif (
                func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.os_names
            ):
                self._read(first)
            elif (
                func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.duckdb_names
            ):
                self._connect(node)
        elif isinstance(func, ast.Name):
            if func.id in self.getenv_names:
                self._read(first)
            elif func.id in self.connect_names:
                self._connect(node)
        self.generic_visit(node)

    def _connect(self, node: ast.Call) -> None:
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and first.value in ("", ":memory:"):
            return
        read_only: ast.expr | None = None
        if len(node.args) > 1:
            read_only = node.args[1]
        for kw in node.keywords:
            if kw.arg == "read_only":
                read_only = kw.value
            elif kw.arg == "config" and "read_only" in ast.dump(kw.value):
                return
        if read_only is None or (isinstance(read_only, ast.Constant) and not read_only.value):
            self.writable_connect = True


def _script_files(scripts_dir: Path) -> list[Path]:
    return [
        p
        for p in sorted(scripts_dir.rglob("*.py"))
        if p.name != "__init__.py" and not p.name.startswith((".", "_"))
    ]


def check_script_env(project_root: Path) -> list[Result]:
    """Return validation results for the scripts under ``project_root/scripts``.

    Warnings only. A script that cannot be read or parsed yields a ``pass``
    line saying it was ignored.
    """
    scripts_dir = project_root / "scripts"
    if not scripts_dir.is_dir():
        return []

    from dango.config.scripts import ScriptsConfig, load_scripts_config

    results: list[Result] = []
    try:
        config = load_scripts_config(project_root)
        config_ok = True
    except Exception:
        config, config_ok = ScriptsConfig(), False
        results.append(
            (
                "scripts: .dango/scripts.yml",
                "pass",
                "could not be read; did not run script environment and warehouse checks",
            )
        )

    provided = _provided_names()
    env_file = project_root / ".env"
    dotenv: dict[str, str | None] = {}
    if env_file.is_file():
        from dotenv import dotenv_values

        dotenv = dict(dotenv_values(env_file))

    if config_ok:
        for entry in config.scripts:
            missing = [n for n in entry.env if dotenv.get(n) is None]
            if missing:
                results.append(
                    (
                        f"scripts: {entry.path}",
                        "warn",
                        f"declares {', '.join(missing)} in .dango/scripts.yml but "
                        f"{'it is' if len(missing) == 1 else 'they are'} not set in .env",
                    )
                )

    for script in _script_files(scripts_dir):
        rel = script.relative_to(scripts_dir).as_posix()
        name = f"scripts: {rel}"
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError):
            results.append(
                (name, "pass", "not checked (syntax error); could not check env or writes")
            )
            continue
        except OSError:
            continue
        scan = _Scan()
        scan.visit(tree)
        if not config_ok:
            continue

        declared = set(config.env_for(rel))
        for var in scan.env_reads:
            if var in declared or var in provided or not _is_declarable(var):
                continue
            results.append(
                (
                    name,
                    "warn",
                    f"script {rel} reads {var} but does not declare it; on cloud servers "
                    "with isolation it will not receive it. Fix: add it to env: in "
                    ".dango/scripts.yml",
                )
            )
        if (
            scan.writable_connect
            and (scan.write_sql or scan.uses_lock)
            and config.warehouse_for(rel) != "write"
        ):
            results.append(
                (
                    name,
                    "warn",
                    f"script {rel} appears to write the warehouse but is not declared "
                    "warehouse: write; on cloud servers with isolation the write will "
                    "fail. Fix: set warehouse: write in .dango/scripts.yml",
                )
            )
    return results
