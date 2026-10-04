"""Amendment subsection labels of a decision record stay unambiguous.

An amendment numbers its subsections with a letter and a number (``### Ж1. ...``,
``#### Е3 — ...``), and the rest of the repository cites them as ``(Ж4)``. Parallel
tasks used to take the same "next free" letter in one ADR, and after the merge one
citation named two subsections. The rule checked here: inside one ADR a letter
belongs to one series — the headings under one parent section (an amendment or a
section) — and a number repeats in no series.

Clashes found on main when the check came (TASK-001279):

- CP-ADR-0073 had two series ``Е`` (``workingCopy`` and ``identity:replace``) with the
  same numbers. The ``identity:replace`` series became ``И``: the ``workingCopy``
  series is the one runbooks and other documents cite.
- CP-ADR-0067 continues series ``В`` from one amendment into the next (В1-В4, then
  В5-В9): deliberate, numbers do not repeat, and the superproject schema copy cites
  В5-В7. It is allowed below by name; a new continuation is a new letter instead.

Found when main was merged into ``integrations-connections``:

- CP-ADR-0063 continues series ``Ж`` from the amendment on ``target: task``
  (Ж1-Ж5) into the one on the author of a fact (Ж6-Ж7). Numbers do not repeat,
  and the series is cited as one outside the repository (the package-sdk schema
  cites Ж1, Ж2, Ж5, Ж6 side by side), so it is allowed by name as well.
- CP-ADR-0073: the scope grammar of the branch took ``Е``, which main's
  ``workingCopy`` series holds; the branch series became ``К``.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = REPO / "docs" / "adr"

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A letter, a number, then the end, a dot, a dash or a space: "Ж1.", "Е3 —", "M1".
LABEL = re.compile(r"^([A-ZА-ЯЁ])(\d+)(?=$|[.\s:—–-])")
FENCE = re.compile(r"^\s*(```|~~~)")

# (ADR file, letter) whose series deliberately continues into a later section.
CONTINUED_SERIES = {
    ("0067-verification-stage.md", "В"),
    ("0063-work-derivation-rules.md", "Ж"),
}


@dataclass(frozen=True)
class Labelled:
    letter: str
    number: int
    lineno: int
    line: str
    parent: str


def labelled_headings(text: str) -> list[Labelled]:
    """Labelled headings with the nearest unlabelled heading above them of a higher level."""
    found: list[Labelled] = []
    # Unlabelled headings still open: (level, "line N: text").
    open_sections: list[tuple[int, str]] = []
    fence: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        fenced = FENCE.match(line)
        if fenced:
            marker = fenced.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        heading = HEADING.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        label = LABEL.match(heading.group(2))
        while open_sections and open_sections[-1][0] >= level:
            open_sections.pop()
        if label:
            parent = open_sections[-1][1] if open_sections else "<top of file>"
            found.append(Labelled(label.group(1), int(label.group(2)), lineno, line, parent))
        else:
            open_sections.append((level, f"line {lineno}: {line}"))
    return found


def label_conflicts(name: str, text: str) -> list[str]:
    """Each letter: one parent section, no number twice. Messages name both headings."""
    problems: list[str] = []
    first_of_letter: dict[str, Labelled] = {}
    first_of_label: dict[tuple[str, int], Labelled] = {}
    for item in labelled_headings(text):
        first = first_of_letter.setdefault(item.letter, item)
        if first.parent != item.parent and (name, item.letter) not in CONTINUED_SERIES:
            problems.append(
                f"{name}: letter {item.letter} names two series — "
                f"line {first.lineno}: {first.line!r} (under {first.parent!r}) and "
                f"line {item.lineno}: {item.line!r} (under {item.parent!r})"
            )
            continue
        key = (item.letter, item.number)
        same = first_of_label.setdefault(key, item)
        if same is not item:
            problems.append(
                f"{name}: label {item.letter}{item.number} repeats in series {item.letter} — "
                f"line {same.lineno}: {same.line!r} and line {item.lineno}: {item.line!r}"
            )
    return problems


def test_adr_amendment_labels_are_unique() -> None:
    records = sorted(ADR_DIR.glob("*.md"))
    assert records, "no decision records found"
    problems = [p for path in records for p in label_conflicts(path.name, path.read_text("utf-8"))]
    assert not problems, (
        "Amendment subsection labels clash; reletter the later series to the next free "
        "letter of that ADR on the target branch and update every citation of it:\n"
        + "\n".join(problems)
    )


def test_the_check_sees_the_labelled_headings_of_real_records() -> None:
    # Guard against a pattern that silently matches nothing.
    text = (ADR_DIR / "0073-agent-registry.md").read_text("utf-8")
    letters = {item.letter for item in labelled_headings(text)}
    assert {"А", "Е", "И", "Ж", "З"} <= letters


AMENDMENT_ONE = """# ADR-0099: Sample

## Amendment 2026-10-01 (TASK-1)

### Ж1. First
### Ж2 — Second
"""


def test_a_letter_taken_by_two_amendments_fails_naming_both_headings() -> None:
    text = AMENDMENT_ONE + "\n## Amendment 2026-10-01 (TASK-2)\n\n### Ж1. Other\n"
    problems = label_conflicts("0099-sample.md", text)
    assert len(problems) == 1
    message = problems[0]
    assert "0099-sample.md" in message
    assert "letter Ж" in message
    assert "line 5: '### Ж1. First'" in message
    assert "line 10: '### Ж1. Other'" in message


def test_a_letter_reused_with_new_numbers_in_another_section_still_fails() -> None:
    text = AMENDMENT_ONE + "\n## Amendment 2026-10-02\n\n### Ж3. Continues?\n"
    assert len(label_conflicts("x.md", text)) == 1


def test_an_allowed_continuation_passes_but_still_may_not_repeat_numbers() -> None:
    name = "0067-verification-stage.md"
    continued = "## A\n\n### В1. x\n### В2. x\n\n## B\n\n### В3. y\n"
    assert label_conflicts(name, continued) == []
    assert label_conflicts("0099-other.md", continued) != []
    problems = label_conflicts(name, continued + "\n## C\n\n### В1. z\n")
    assert len(problems) == 1
    assert "label В1 repeats" in problems[0]


def test_a_number_repeated_in_one_series_fails() -> None:
    text = AMENDMENT_ONE + "### Ж2. Again\n"
    problems = label_conflicts("x.md", text)
    assert len(problems) == 1
    assert "label Ж2 repeats" in problems[0]
    assert "'### Ж2 — Second'" in problems[0]
    assert "'### Ж2. Again'" in problems[0]


def test_distinct_letters_and_several_letters_in_one_amendment_pass() -> None:
    text = AMENDMENT_ONE + "### З1. Next series, same amendment\n\n## Amendment 2\n\n### И1. x\n"
    assert label_conflicts("x.md", text) == []


def test_unlabelled_subheadings_between_labels_keep_the_series() -> None:
    text = AMENDMENT_ONE + "### Checks\n\n#### Detail\n\n### Ж3. Third\n#### Ж4. Deeper\n"
    assert label_conflicts("x.md", text) == []


def test_headings_inside_code_fences_are_ignored() -> None:
    text = AMENDMENT_ONE + "\n## Other\n\n```yaml\n### Ж1. not a heading\n```\n~~~\n## Ж1\n~~~\n"
    assert label_conflicts("x.md", text) == []


@pytest.mark.parametrize(
    "heading",
    [
        "### §13 Events",
        "## Амендмент 2 2026-09-25",
        "### ЖK1. two letters",
        "### Ж12abc",
        "### ж1. lower case",
        "Ж1. not a heading",
    ],
)
def test_text_that_is_not_a_label(heading: str) -> None:
    assert labelled_headings(f"## A\n\n{heading}\n") == []


@pytest.mark.parametrize(
    ("heading", "label"),
    [
        ("### Ж1. Dot", ("Ж", 1)),
        ("### Ж1 — Dash", ("Ж", 1)),
        ("#### Е3. Deeper", ("Е", 3)),
        ("## M1 — Latin", ("M", 1)),
        ("### З10", ("З", 10)),
        ("### Ё2: colon", ("Ё", 2)),
    ],
)
def test_label_forms(heading: str, label: tuple[str, int]) -> None:
    [item] = labelled_headings(f"# Title\n\n{heading}\n")
    assert (item.letter, item.number) == label
    assert item.parent == "line 1: # Title"


def test_empty_text_and_a_label_without_a_parent() -> None:
    assert label_conflicts("x.md", "") == []
    [item] = labelled_headings("### Ж1. Orphan\n")
    assert item.parent == "<top of file>"
