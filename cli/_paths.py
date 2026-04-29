"""Locate repo root by walking up from CWD until we find harness/rules/_index.yaml.

Used by every CLI verb so they can run from any subdirectory.
"""

from pathlib import Path

from . import REPO_ROOT_MARKER


class RepoRootNotFoundError(RuntimeError):
    pass


def find_repo_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / REPO_ROOT_MARKER).is_file():
            return candidate
    raise RepoRootNotFoundError(
        f"Could not locate {REPO_ROOT_MARKER} from {here}. "
        "Run mcp-ax from inside the project."
    )


def rules_dir(repo_root: Path) -> Path:
    return repo_root / "harness" / "rules"


def schemas_dir(repo_root: Path) -> Path:
    return repo_root / "harness" / "schemas"
