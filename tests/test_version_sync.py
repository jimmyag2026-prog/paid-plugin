"""Enforces version single source of truth.

The canonical version string lives in ``paid/_version.py``. Every other
file that carries a version number must match it; otherwise this test
catches the drift before release.

Files checked:
- ``plugin.yaml`` ``version:`` field (read by Hermes at plugin load)
- ``pyproject.toml`` declares ``dynamic = ["version"]`` (no literal)
- the setuptools dynamic ``attr`` actually *resolves* to the canonical
  version using setuptools' own resolver — this is the build-time path
  (``pip install`` / ``python -m build``) and the exact failure mode the
  SSOT change exists to fix (a wheel labeled with a stale version). The
  import-path check alone does NOT exercise this.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from paid._version import __version__ as CANONICAL


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_plugin_yaml_version_matches_canonical() -> None:
    """plugin.yaml ``version:`` must equal paid._version.__version__."""
    with (REPO_ROOT / "plugin.yaml").open() as f:
        data = yaml.safe_load(f)
    assert data["version"] == CANONICAL, (
        f"plugin.yaml version={data['version']!r} drifted from "
        f"paid/_version.py={CANONICAL!r}. "
        f"Run bin/bump-version.sh to keep them aligned."
    )


def test_pyproject_uses_dynamic_version() -> None:
    """pyproject.toml must declare version as dynamic, not hardcoded.

    A hardcoded ``version = "x.y.z"`` line is the exact bug this PR was
    meant to prevent — catch it if anyone re-introduces it.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    # The [project] section should declare dynamic, not a literal version.
    project_match = re.search(
        r"\[project\](.*?)(\n\[|\Z)", text, re.DOTALL
    )
    assert project_match is not None, "pyproject.toml has no [project] section"
    project_block = project_match.group(1)
    assert 'dynamic' in project_block and 'version' in project_block, (
        "pyproject.toml [project] section must declare `dynamic = [\"version\"]`"
    )
    # No literal `version = "..."` line in [project].
    assert not re.search(r'^version\s*=\s*"', project_block, re.MULTILINE), (
        "pyproject.toml [project] section must not hardcode `version = \"...\"`. "
        "Use dynamic = [\"version\"] + [tool.setuptools.dynamic] instead."
    )


def test_paid_package_exports_version() -> None:
    """`from paid import __version__` must work and return canonical value."""
    import paid

    assert paid.__version__ == CANONICAL


def test_setuptools_dynamic_resolves_to_canonical() -> None:
    """The pyproject ``[tool.setuptools.dynamic]`` attr must resolve to the
    canonical version *via setuptools' own resolver* — the build-time path.

    The other tests check the import path (`from paid import __version__`)
    and the pyproject declaration shape, but NOT that setuptools actually
    resolves ``{attr = "paid._version.__version__"}`` to the right value.
    That gap is precisely the original bug (a wheel labeled with a stale
    version while the runtime was correct). ``read_attr`` is the same
    function setuptools.config.pyprojecttoml uses to expand dynamic attrs,
    so this exercises the real build resolution without a full ``build``.
    """
    from setuptools.config.expand import read_attr

    resolved = read_attr("paid._version.__version__", root_dir=str(REPO_ROOT))
    assert resolved == CANONICAL, (
        f"setuptools dynamic attr resolved to {resolved!r} but canonical "
        f"is {CANONICAL!r} — `pip install`/`build` would label the wheel "
        f"with the wrong version. Check [tool.setuptools.dynamic] in "
        f"pyproject.toml and paid/_version.py."
    )
