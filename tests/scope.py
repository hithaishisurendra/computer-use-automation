"""Which packages a structural guard covers, derived rather than listed.

Every guard used to carry its own hardcoded package list. That is this
project's recurring defect wearing a test's clothes: `api/` was added, four
lists were not updated, and four guards went on reporting clean over code they
never read. A guard with stale scope is worse than no guard, because it
answers the question it was asked without covering the ground the asker meant.

So scope is computed from the repository. A new package is covered the moment
it exists, and the only way to exclude one is to name it here with a reason --
which is reviewable, unlike an omission.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are not first-party runtime code. Each needs a reason,
# because "it was never added to the list" is exactly the failure this
# module exists to prevent.
_NOT_RUNTIME = {
    "tests": "the guards themselves",
    "docs": "prose",
    "config": "data",
    "capabilities": "data",
    "evidence": "run output",
    "coreserv": "a target application this repo ships as a fixture, not engine code",
    ".venv": "dependencies",
    ".git": "vcs",
    ".pytest_cache": "cache",
    "__pycache__": "build output",
}


def packages() -> list[str]:
    """Every first-party package a structural guard should consider."""
    return sorted(
        p.name
        for p in REPO_ROOT.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in _NOT_RUNTIME
        and any(p.glob("*.py"))
    )


def engine_packages() -> list[str]:
    """Packages that must not know which application they are driving.

    `discovery` and `scripts` are excluded on purpose and by name: discovery
    builds the app-specific profile lookups, and scripts are per-target
    diagnostics. Everything else -- including any package added later -- has
    to be app-agnostic.
    """
    return [p for p in packages() if p not in ("discovery", "scripts")]


def sources(package: str) -> list[Path]:
    """Every Python file in a package, including nested ones.

    rglob rather than glob: a guard that reads only the top level would miss
    a subpackage, which is the same stale-scope failure one directory down.
    """
    return sorted((REPO_ROOT / package).rglob("*.py"))
