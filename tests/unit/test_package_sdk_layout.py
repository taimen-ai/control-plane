"""Where the contract tests look for package-sdk, on both layouts (TAI-ADR-0064).

The comparison of the pinned schemas with package-sdk must not stop silently
when the superproject moves to ``services/`` and ``sdk/`` (rule 4 of the ADR):
inside the umbrella a missing package-sdk fails, outside it the skip says why.
"""

from pathlib import Path

import pytest

from tests import package_sdk
from tests.package_sdk import (
    FLAT_LAYOUT,
    SEGMENTS_LAYOUT,
    inside_umbrella,
    layout_of,
    package_sdk_schemas_of,
    umbrella_of,
)

# (where control-plane is checked out in the umbrella, where package-sdk is)
LAYOUTS = [
    pytest.param("control-plane", "package-sdk", id="flat"),
    pytest.param("services/control-plane", "sdk/package-sdk", id="segments"),
]


def _gitmodules(umbrella: Path, *paths: str) -> None:
    umbrella.mkdir(parents=True, exist_ok=True)
    (umbrella / ".gitmodules").write_text(
        "".join(f'[submodule "{Path(p).name}"]\n\tpath = {p}\n\turl = x\n' for p in paths)
    )


@pytest.mark.parametrize(("own", "sdk"), LAYOUTS)
def test_the_layout_is_told_by_where_control_plane_lies(tmp_path: Path, own: str, sdk: str) -> None:
    umbrella = tmp_path / "umbrella"
    root = umbrella / own

    assert layout_of(root) == (own, sdk)
    assert umbrella_of(root) == umbrella
    assert package_sdk_schemas_of(root) == umbrella / sdk / "schema" / "v1"


@pytest.mark.parametrize(("own", "sdk"), LAYOUTS)
def test_inside_the_umbrella_only_at_the_path_of_its_layout(
    tmp_path: Path, own: str, sdk: str
) -> None:
    umbrella = tmp_path / "umbrella"
    root = umbrella / own
    assert not inside_umbrella(root)  # no .gitmodules
    _gitmodules(umbrella, sdk)
    assert not inside_umbrella(root)  # control-plane is not a submodule
    _gitmodules(umbrella, sdk, own)
    assert inside_umbrella(root)


def test_a_superproject_of_the_other_layout_is_not_its_umbrella(tmp_path: Path) -> None:
    """A copy at ``services/control-plane`` under a flat ``.gitmodules`` is no submodule."""
    umbrella = tmp_path / "umbrella"
    _gitmodules(umbrella, *FLAT_LAYOUT)
    assert not inside_umbrella(umbrella / SEGMENTS_LAYOUT[0])
    _gitmodules(umbrella, *SEGMENTS_LAYOUT)
    assert not inside_umbrella(umbrella / FLAT_LAYOUT[0])


def _checked_out(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(package_sdk, "ROOT", root)
    monkeypatch.setattr(package_sdk, "UMBRELLA", umbrella_of(root))
    monkeypatch.setattr(package_sdk, "PACKAGE_SDK_SCHEMAS", package_sdk_schemas_of(root))


@pytest.mark.parametrize(("own", "sdk"), LAYOUTS)
def test_a_missing_package_sdk_fails_inside_the_umbrella(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: str, sdk: str
) -> None:
    umbrella = tmp_path / "umbrella"
    _gitmodules(umbrella, own, sdk)
    _checked_out(monkeypatch, umbrella / own)

    with pytest.raises(pytest.fail.Exception, match=f"{sdk}/schema/v1/object.schema.json"):
        package_sdk.live_schema_path("object.schema.json")


@pytest.mark.parametrize(("own", "sdk"), LAYOUTS)
def test_a_missing_package_sdk_outside_the_umbrella_is_a_skip_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: str, sdk: str
) -> None:
    _checked_out(monkeypatch, tmp_path / "copy" / own)

    with pytest.raises(pytest.skip.Exception, match=f"not checked out at {sdk}"):
        package_sdk.live_schema_path("object.schema.json")


@pytest.mark.parametrize(("own", "sdk"), LAYOUTS)
def test_the_live_schema_is_found_on_both_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: str, sdk: str
) -> None:
    umbrella = tmp_path / "umbrella"
    _gitmodules(umbrella, own, sdk)
    live = umbrella / sdk / "schema" / "v1" / "object.schema.json"
    live.parent.mkdir(parents=True)
    live.write_text("{}")
    _checked_out(monkeypatch, umbrella / own)

    assert package_sdk.live_schema_path("object.schema.json") == live
    assert package_sdk.schema_path("object.schema.json") == live
