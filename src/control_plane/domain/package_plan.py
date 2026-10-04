"""The plan of a package apply: fields and their owners, renames, hashes (CP-ADR-0074 §11).

``POST /packages:plan`` shows what applying a package would do;
``POST /packages:apply`` does exactly that or refuses. What is decided here,
without I/O:

- **Fields.** A field is a top-level member of an object's ``spec``. Its
  ``before`` is the latest version in the catalog, its ``after`` what the
  package brings. Its owner is ``console`` when a person changed it since the
  last apply — the latest version differs from what that apply wanted — and
  ``package`` otherwise. A console field is kept unless the plan is asked to
  overwrite it (TAI-ADR-0044): the version the apply publishes takes the
  latest value of that field (:func:`diff_fields`). ``version`` of a process
  is its number, never a person's field.
- **Renames.** ``package.yaml → renames: [{kind, from, to}]`` (like
  ``moved`` in Terraform) of processes and calendars (:func:`renames`); a
  rename of another kind the core plans is a warning: nothing moves.
- **Hashes.** The package is the hash of its files; the catalog etag is the
  hash of the canonical list of the objects the package touches — kind, key,
  latest version and hash, what the last apply wrote and whether the key is
  retired; the plan hash covers the package, the etag, the changes and the
  fate of open instances (:func:`plan_hash`).

Pure functions over plain values; no I/O.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from control_plane.domain.package_source import ParsedPackage
from control_plane.domain.process_definition import Problem, pointer

# The kinds the core plans and applies, in the order it applies them (the
# installer's order): an agent names task types, a process names task types,
# an agent and calendars (CP-ADR-0074 §9), a rule names task types and acts
# as an agent (amendment 2026-09-29), a view names processes and task types
# (CP-ADR-0080).
PLANNED_KINDS = ("TaskType", "Agent", "Calendar", "Process", "WorkRule", "View")
# Kinds the core reads from a package and holds inside another: a component is
# inlined into the views that name it (CP-ADR-0080).
INLINED_KINDS = ("Component",)
# The kinds whose keys ``renames`` moves: their versions carry over to the new key.
RENAMED_KINDS = ("Calendar", "Process")
# Who applies the kinds of a package the core does not plan: rules of
# notifications live in the notification service, the rest the installer
# applies (package-sdk).
OUTSIDE = {"NotificationRule": "notification-service"}
INSTALLER = "installer"
OWNER_PACKAGE = "package"
OWNER_CONSOLE = "console"
ACTIONS = ("create", "update", "rename", "restore", "retire", "unchanged")
# Fields that are never a person's: the number of a process version.
NUMBER_FIELDS = frozenset({"version"})
_ABSENT = object()


def canonical_hash(value: Any) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``value`` (sorted keys, no spaces)."""
    body = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def package_hash(files: Iterable[tuple[str, str]]) -> str:
    """The hash of a package: its files, by path."""
    return canonical_hash([{"path": path, "content": content} for path, content in sorted(files)])


# --- fields --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    """``PlanFieldOut``: one field of an object that the apply changes or keeps."""

    name: str
    before: Any
    after: Any
    owner: str
    applies: bool

    def out(self) -> dict[str, Any]:
        return {
            "path": pointer("spec", self.name),
            "before": self.before,
            "after": self.after,
            "owner": self.owner,
            "applies": self.applies,
        }


def diff_fields(
    latest: Mapping[str, Any] | None,
    wanted: Mapping[str, Any],
    applied: Mapping[str, Any] | None,
    *,
    overwrite: bool = False,
) -> tuple[list[FieldChange], dict[str, Any]]:
    """The fields ``wanted`` changes against ``latest``, and the spec the apply publishes.

    ``applied`` — what the last apply of the object wanted (``None``: never
    applied by a package, every field is the package's). A field whose latest
    value differs from it is ``console``'s; unless ``overwrite``, the published
    spec keeps its latest value.
    """
    if latest is None:
        return [
            FieldChange(name, None, value, OWNER_PACKAGE, True)
            for name, value in sorted(wanted.items())
        ], dict(wanted)
    published = dict(wanted)
    changes: list[FieldChange] = []
    for name in sorted(set(latest) | set(wanted)):
        before = latest.get(name, _ABSENT)
        after = wanted.get(name, _ABSENT)
        if before == after:
            continue
        console = (
            applied is not None
            and name not in NUMBER_FIELDS
            and applied.get(name, _ABSENT) != before
        )
        applies = overwrite or not console
        if not applies:
            if before is _ABSENT:
                published.pop(name, None)
            else:
                published[name] = before
        changes.append(
            FieldChange(
                name,
                None if before is _ABSENT else before,
                None if after is _ABSENT else after,
                OWNER_CONSOLE if console else OWNER_PACKAGE,
                applies,
            )
        )
    return changes, published


# --- renames -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rename:
    kind: str
    source: str
    target: str


def renames(package: ParsedPackage) -> tuple[list[Rename], list[Problem]]:
    """The renames of processes and calendars, with the findings of the list.

    ``to`` is an object of the package, ``from`` is not; a key is renamed
    once. A rename of another kind the core plans moves nothing (a warning):
    the old key stays as it is.
    """
    manifest = package.manifest_object
    if manifest is None:
        return [], []
    found: list[Rename] = []
    problems: list[Problem] = []
    objects = {(obj.kind, obj.key) for obj in package.objects}
    sources: set[tuple[str, str]] = set()
    targets: set[tuple[str, str]] = set()
    for index, item in enumerate(manifest.spec.get("renames") or ()):
        if not isinstance(item, Mapping):
            continue
        kind, source, target = item.get("kind"), item.get("from"), item.get("to")
        if kind not in PLANNED_KINDS or not isinstance(source, str) or not isinstance(target, str):
            continue
        path = pointer("spec", "renames", index)
        if kind not in RENAMED_KINDS:
            problems.append(
                manifest.place(
                    Problem(
                        "rename_not_planned",
                        "warning",
                        path,
                        f"a {kind} is not renamed by the plan: {kind}/{source} stays,"
                        f" {kind}/{target} is applied as its own key",
                        hint=f"retire {kind}/{source} in the installation if it is gone",
                    )
                )
            )
            continue

        def refuse(message: str, where: str = path) -> None:
            problems.append(manifest.place(Problem("invalid_rename", "error", where, message)))

        if source == target:
            refuse(f"{kind}/{source} is renamed to itself")
        elif (kind, target) not in objects:
            refuse(f"the package has no {kind}/{target} to rename to", path + "/to")
        elif (kind, source) in objects:
            refuse(f"the package still has {kind}/{source}", path + "/from")
        elif (kind, source) in sources:
            refuse(f"{kind}/{source} is renamed twice", path + "/from")
        elif (kind, target) in targets:
            refuse(f"two keys are renamed to {kind}/{target}", path + "/to")
        else:
            sources.add((kind, source))
            targets.add((kind, target))
            found.append(Rename(kind, source, target))
    return found, problems


# --- hashes --------------------------------------------------------------------------------


def catalog_etag(entries: Iterable[Mapping[str, Any]]) -> str:
    """The etag of the catalog a plan was built on.

    ``entries`` — ``{kind, key, version, hash, applied, retired}`` of every
    object the package touches: its objects and the keys it renames away.
    """
    return canonical_hash(sorted(entries, key=lambda e: (str(e["kind"]), str(e["key"]))))


def plan_body(processes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What of ``PlanProcessOut`` the plan hash covers: versions and the fate of instances.

    The behaviour (a replay of a sample of instances) and the number of open
    instances are reports: every input of a live instance would change them.
    Which versions have open instances, what becomes of them and whether a
    migration is missing is what the apply does, and is hashed.
    """
    return [
        {
            "key": item["key"],
            "fromVersion": item["fromVersion"],
            "toVersion": item["toVersion"],
            "instances": [
                {
                    "version": group["version"],
                    "fate": group["fate"],
                    "migrationRequired": group["migrationRequired"],
                }
                for group in item["instances"]
            ],
        }
        for item in processes
    ]


def outside(package: ParsedPackage) -> list[dict[str, str]]:
    """``PlanOutsideOut``: the objects the core does not plan, and who applies them."""
    return [
        {"kind": obj.kind, "key": obj.key, "appliedBy": OUTSIDE.get(obj.kind, INSTALLER)}
        for obj in sorted(package.objects, key=lambda o: (o.kind, o.key))
        if obj.kind not in PLANNED_KINDS
        and obj.kind not in INLINED_KINDS
        and obj.kind not in ("Package", "Installation")
    ]


def plan_hash(
    package: str,
    etag: str,
    changes: Sequence[Mapping[str, Any]],
    processes: Sequence[Mapping[str, Any]],
    *,
    overwrite: bool = False,
    settings: Mapping[str, Any] | None = None,
) -> str:
    """``planHash``: the package, the catalog etag, the changes and the fate of instances.

    ``settings`` — the ``settings`` section of the plan (CP-ADR-0081 §7), when
    the package declares or declared settings; a plan without one hashes as before.
    """
    body: dict[str, Any] = {
        "package": package,
        "catalogEtag": etag,
        "changes": list(changes),
        "processes": plan_body(processes),
        "overwriteConsole": overwrite,
    }
    if settings is not None:
        body["settings"] = dict(settings)
    return canonical_hash(body)
