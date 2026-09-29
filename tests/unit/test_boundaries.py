"""The emulator shares source and database with CDMS but must stay a separate system (decision D12).

CDMS reaches it over HTTP only, so the real Vietful can replace it by changing a URL; the emulator does not
use CDMS's change-detection logic, so it cannot accidentally agree with CDMS by sharing its bugs.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cdms"
EMULATOR = SRC / "emulator"


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_cdms_never_imports_the_emulator() -> None:
    offenders = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if EMULATOR not in path.parents
        and any(module.startswith("cdms.emulator") for module in imported_modules(path))
    }
    assert offenders == set()


def test_cdms_never_queries_the_emulator_schema() -> None:
    offenders = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if EMULATOR not in path.parents and "vietful." in path.read_text(encoding="utf-8")
    }
    assert offenders == set()


def test_emulator_uses_only_shared_infrastructure() -> None:
    allowed = ("cdms.config", "cdms.db.base", "cdms.db.session", "cdms.logs", "cdms.emulator")
    used = {
        module
        for path in EMULATOR.rglob("*.py")
        for module in imported_modules(path)
        if module.startswith("cdms")
    }
    assert {module for module in used if not module.startswith(allowed)} == set()
