"""The core knows no meaning of a setting (CP-ADR-0081 §9, FR-017).

The fields of the pilot package's settings are data of the package: no name
of them is written in ``src/``. ``approverRole`` is also a field of the core's
own acceptance checks (CP-ADR-0067: ``human`` check), so it is guarded in the
code of settings only; the names only the pilot has are guarded everywhere.
The domain words of the neutrality guard of processes cover the same files
(``tests/unit/test_process_neutrality.py``).
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
CORE = SRC / "control_plane"
SETTINGS_CODE = [
    CORE / "domain" / "package_settings.py",
    CORE / "application" / "commands" / "package_settings.py",
    CORE / "application" / "queries" / "package_settings.py",
    CORE / "api" / "v1" / "package_settings.py",
]
# Fields of the settings of the pilot package (TAI-ADR-0067, CP-ADR-0081 §1).
PILOT_ONLY = ["approvalThreshold", "reviewDueWorkdays", r"invoice\w*"]
PILOT_IN_SETTINGS = [*PILOT_ONLY, "approverRole", r"threshold\w*"]


def _offenders(paths: list[Path], words: list[str]) -> list[str]:
    pattern = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)
    found = []
    for path in paths:
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()[:80]}")
    return found


def test_no_field_of_the_pilot_is_named_in_the_core() -> None:
    sources = sorted([*SRC.rglob("*.py"), *SRC.rglob("*.json"), *SRC.rglob("*.yaml")])
    assert sources
    assert _offenders(sources, PILOT_ONLY) == []


def test_the_code_of_settings_names_no_field_of_any_package() -> None:
    assert all(path.is_file() for path in SETTINGS_CODE)
    assert _offenders(SETTINGS_CODE, PILOT_IN_SETTINGS) == []
