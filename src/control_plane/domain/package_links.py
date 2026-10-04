"""Which package installed a catalog object (CP-ADR-0074 §11, amendment TASK-000904).

A catalog object is a ``(kind, key)`` of the tenant: all versions of a task
type, of a process, all revisions of an agent are one object. It is linked to
the package whose installation last named it — ``package_objects`` — or to
none: an object created by hand. The link is the object's, not a version's: a
version a person published later stays in the package it belongs to (who
changed a field is the plan's question, ``owner`` of ``packages:plan``).

- The core writes the link of the kinds it plans (:data:`PLANNED_KINDS`) in
  ``POST /packages:apply``; the installer applies the other kinds through their
  own routes and names what it applied in ``POST /packages:record``.
- ``TaskType``, ``Agent`` and ``WorkRule`` are planned since the amendment of
  2026-09-29, and the installer still applies them through their routes
  until it moves to plan and apply: it records them as well. Its record
  clears what an earlier apply wanted (``spec``): the installer overwrote the
  object, every field is the package's again.
- ``POST /agents`` with ``package`` links the agent as well: the revision's
  source and the object's link are the same package.

Pure values; no I/O.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from control_plane.domain.package_plan import PLANNED_KINDS

# The catalog kinds the core holds, in the order of the catalog schema
# (``ConnectionType`` right after ``Capability``: the installer's order,
# CP-ADR-0079 §2).
# NotificationRule lives in the notification service, Package and
# Installation are not objects of a tenant's catalog.
LINKED_KINDS = (
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "ConnectionType",
    "Skill",
    "WorkRule",
    "Agent",
    "Process",
    "Calendar",
    "View",
)
# The kinds only POST /packages:apply publishes and links: those of the
# process engine and the screens of a package (CP-ADR-0080).
ENGINE_KINDS = ("Process", "Calendar", "View")
assert set(ENGINE_KINDS) <= set(PLANNED_KINDS)
# The kinds the installer records: all but the engine's.
RECORDED_KINDS = tuple(kind for kind in LINKED_KINDS if kind not in ENGINE_KINDS)


@dataclass(frozen=True)
class PackageLink:
    """``PackageLinkOut``: the package that installed an object."""

    key: str
    version: str | None
    install_hash: str | None
    installed_at: datetime

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "installHash": self.install_hash,
            "installedAt": self.installed_at.isoformat().replace("+00:00", "Z"),
        }
