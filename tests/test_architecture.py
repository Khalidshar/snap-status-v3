"""Repository-wide guards: import boundaries between apps/core and required reference docs."""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGES = {
    "studio_core": REPO / "packages" / "studio-core" / "studio_core",
    "snap_bot": REPO / "apps" / "snap-bot" / "snap_bot",
    "status_bot": REPO / "apps" / "status-bot" / "status_bot",
}
FORBIDDEN_IMPORTS = {
    "studio_core": {"snap_bot", "status_bot"},
    "snap_bot": {"status_bot"},
    "status_bot": {"snap_bot"},
}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_import_boundaries(package):
    violations = [
        f"{source.relative_to(REPO)} imports {root}"
        for source in PACKAGES[package].rglob("*.py")
        for root in _imported_roots(source) & FORBIDDEN_IMPORTS[package]
    ]
    assert not violations


def test_each_app_has_its_own_import_package():
    for app_dir in (REPO / "apps").iterdir():
        if app_dir.is_dir():
            assert (app_dir / app_dir.name.replace("-", "_") / "__init__.py").is_file()


@pytest.mark.parametrize(
    "relative", ["docs/DISCOVERY.md", "docs/MIGRATION-MATRIX.md", "config/defaults.toml"]
)
def test_reference_files_present(relative):
    assert (REPO / relative).is_file()
