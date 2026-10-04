"""Where the contract tests take the catalog schema of packages from.

The schema belongs to package-sdk (``schema/v1``); since S007 the superproject
holds package-sdk as its submodule ``package-sdk`` and no longer has
``packages/schema``. The core keeps pinned copies in
``tests/fixtures/superproject/`` so that it is tested outside the superproject
too; inside it the pinned copies must equal package-sdk.

Inside the umbrella (a superproject whose ``.gitmodules`` declares
control-plane) a missing package-sdk schema is an error: the submodule is not
checked out, and a skip would hide a drifted copy. Outside the umbrella the
comparison with the live schema is a visible skip with its reason, and the
other tests fall back to the pinned copies (TAI-ADR-0064, rule 4).

Two layouts of the superproject are known (TAI-ADR-0064): the flat one, with
control-plane and package-sdk at its root, and the one with segments, with
control-plane at ``services/control-plane`` and package-sdk at
``sdk/package-sdk``. The layout is told by where control-plane itself lies,
never by looking for package-sdk in both places: a schema found at the wrong
path would be compared in place of the missing one.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINNED_SCHEMAS = ROOT / "tests" / "fixtures" / "superproject"
# (control-plane, package-sdk): their paths in the superproject, by layout.
FLAT_LAYOUT = ("control-plane", "package-sdk")
SEGMENTS_LAYOUT = ("services/control-plane", "sdk/package-sdk")


def layout_of(root: Path) -> tuple[str, str]:
    """The layout a control-plane checked out at ``root`` belongs to."""
    return SEGMENTS_LAYOUT if root.parent.name == "services" else FLAT_LAYOUT


def umbrella_of(root: Path) -> Path:
    """The directory a control-plane at ``root`` is a submodule of, if it is one."""
    own, _ = layout_of(root)
    return root.parents[own.count("/")]


def package_sdk_schemas_of(root: Path) -> Path:
    return umbrella_of(root) / layout_of(root)[1] / "schema" / "v1"


UMBRELLA = umbrella_of(ROOT)
PACKAGE_SDK_SCHEMAS = package_sdk_schemas_of(ROOT)
# The schemas the core pins; each one is held equal to package-sdk.
PINNED_NAMES = ("object.schema.json", "test.schema.json")


def inside_umbrella(root: Path = ROOT) -> bool:
    """control-plane at ``root`` is a submodule of its umbrella, at its layout's path."""
    gitmodules = umbrella_of(root) / ".gitmodules"
    if not gitmodules.is_file():
        return False
    own = re.escape(layout_of(root)[0])
    declared = re.compile(rf"^\s*path\s*=\s*{own}\s*$", re.MULTILINE)
    return bool(declared.search(gitmodules.read_text("utf-8")))


def schema_path(name: str) -> Path:
    """package-sdk's schema when it is checked out beside control-plane, else the pinned copy."""
    live = PACKAGE_SDK_SCHEMAS / name
    return live if live.is_file() else PINNED_SCHEMAS / name


def live_schema_path(name: str) -> Path:
    """package-sdk's schema; if absent, an error inside the umbrella and a skip outside it."""
    live = PACKAGE_SDK_SCHEMAS / name
    if live.is_file():
        return live
    if inside_umbrella(ROOT):
        pytest.fail(
            f"{live.relative_to(UMBRELLA)} is missing in the superproject:"
            " check out the submodule package-sdk"
            f" (git submodule update --init {layout_of(ROOT)[1]})"
        )
    pytest.skip(
        f"package-sdk is not checked out at {layout_of(ROOT)[1]} of the superproject:"
        " compared with the pinned copy and its digest only"
    )
