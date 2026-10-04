"""The repository catalog of a universal runner (TAI-ADR-0063 §3, universal-runner U005).

One coding agent works tasks of any repository its description lists. The
repository of a task is a key in a field of the task (``repositoryField``,
``customFields.repositoryKey`` for ``coding-task``); the catalog maps the key
— or an alias, an older name of the same repository — to the URL, the base
branch and whether branches are published there. The catalog is the only
source of addresses: a URL in a task is never taken, and a task without a
key, or with a key the catalog does not know, is not run in a repository by
default — it goes to a person as :data:`REPOSITORY_UNKNOWN`.

Working copies are cut by an :class:`ExecutionWorkspacePool` per repository,
made the first time a task of that repository comes. All pools share one
root, so a task keeps one container ``<root>/<publicId>/`` whatever its
repository: the copy of the repository sits in it under the entry's
directory — one name or a path of several segments (TAI-ADR-0064), so the
container is laid out like the superproject — and a copy made under the
one-repository form of ``workingCopy`` is found where it was.

The key a task ran under is written to the ``execution.workspace``
checkpoint. A task whose key changed since (a key corrected by a person)
leaves its old copy behind only when that copy holds no work: a clean copy
and a branch without commits of its own are removed, anything else stops
the run as :data:`REPOSITORY_CHANGED` — moving work between repositories is a
person's decision.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from control_plane_agent.workspace import ExecutionWorkspacePool

logger = logging.getLogger("control_plane_agent.catalog")

#: A task without a repository key, or with one the catalog does not know.
REPOSITORY_UNKNOWN = "repository_unknown"
#: A task whose key changed while its old copy holds work of its own.
REPOSITORY_CHANGED = "repository_changed"

# The schema's patterns (packages/schema/v1, $defs.repositoryKey and
# agentWorkingCopies.catalog.repositoryField). \Z, not $: $ admits a trailing
# newline.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}\Z")
# What a key or an alias may look like, to echo a value back safely.
_NAME_RE = re.compile(r"^\w[\w.-]{0,62}\Z")
_UNRESOLVED_RE = re.compile(r"\$\{[^}]*\}")
# $defs.repositoryAlias: Latin and Cyrillic letters, digits, ``. _ -``.
_ALIAS_CHARS = "A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u0400-\u04ff"
_ALIAS_RE = re.compile(rf"^[{_ALIAS_CHARS}][{_ALIAS_CHARS}._-]{{0,62}}\Z")
# $defs.workingCopyPath (agentWorkingCopies.catalogEntry.directory): one name
# or a path of several segments (TAI-ADR-0064), at most 200 characters.
_DIRECTORY_SEGMENT = r"[a-z0-9][a-z0-9._-]{0,99}"
_DIRECTORY_RE = re.compile(rf"^{_DIRECTORY_SEGMENT}(?:/{_DIRECTORY_SEGMENT})*\Z")
_DIRECTORY_MAX_LENGTH = 200
# ``checks`` is the daemon's switch, read by ``revision.settings_of``.
_CATALOG_FIELDS = frozenset(
    {"repositoryField", "superproject", "publish", "repositories", "checks"}
)
_ENTRY_FIELDS = frozenset({"url", "baseRef", "directory", "publish", "aliases"})


class CatalogError(ValueError):
    """``workingCopy`` names a catalog this runner cannot use."""


class RepositoryBlocked(Exception):
    """The task cannot be run in a repository of the catalog; a person decides.

    ``code`` is the failure reason of the run, ``reason`` the words for the
    person. Neither carries a URL: the task names a key, and the address is
    the host's configuration.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def is_catalog(working_copy: Mapping[str, Any]) -> bool:
    """The catalog form of ``workingCopy``, by the schema's rule."""
    return "repositories" in working_copy or "repositoryField" in working_copy


@dataclass(frozen=True)
class CatalogEntry:
    """One repository of the catalog under its canonical key."""

    key: str
    url: str
    base_ref: str = "HEAD"
    # The copy's directory in the task's container; the key by default.
    directory: str = ""
    publish: bool = True
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryCatalog:
    field: str
    entries: Mapping[str, CatalogEntry]
    # Key of the entry whose submodules pin the neighbours (U006).
    superproject: str | None = None

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> RepositoryCatalog:
        """Read ``workingCopy`` of the catalog form, refusing what the schema refuses.

        The core stores the section as data (CP-ADR-0073, amendment
        2026-09-30), so its shape is checked here too: a field this runner
        would ignore, or an ambiguous name, is a misconfiguration to fix, not
        a guess to make per task.
        """
        unknown = sorted(set(spec) - _CATALOG_FIELDS)
        if unknown:
            raise CatalogError(f"unknown field(s) of a catalog: {', '.join(unknown)}")
        field = spec.get("repositoryField")
        if not isinstance(field, str) or not _FIELD_RE.match(field):
            raise CatalogError("repositoryField must name a field of the task")
        publish = spec.get("publish", True)
        if not isinstance(publish, bool):
            raise CatalogError("publish must be a boolean")
        repositories = spec.get("repositories")
        if not isinstance(repositories, Mapping) or not repositories:
            raise CatalogError("repositories must list at least one repository")
        entries = {
            str(key): _entry(key, value, publish=publish) for key, value in repositories.items()
        }
        names: set[str] = set()
        for entry in entries.values():
            for name in (entry.key, *entry.aliases):
                folded = name.casefold()
                if folded in names:
                    raise CatalogError(
                        f"{name!r} is named twice in the catalog (keys and aliases "
                        "are compared case-insensitively)"
                    )
                names.add(folded)
        # The rest of the package-sdk check: one entry per address and per directory.
        urls: dict[str, str] = {}
        directories: dict[str, str] = {}
        keys = {key.casefold(): key for key in entries}
        for entry in entries.values():
            identity = _url_identity(entry.url)
            if identity in urls:
                raise CatalogError(f"{entry.key} and {urls[identity]} have the same url")
            urls[identity] = entry.key
            folded = entry.directory.casefold()
            if folded in directories:
                raise CatalogError(
                    f"{entry.key} and {directories[folded]} share the directory {entry.directory!r}"
                )
            directories[folded] = entry.key
            other = keys.get(folded)
            if other is not None and other != entry.key:
                raise CatalogError(
                    f"repositories.{entry.key}.directory {entry.directory!r} is the key of "
                    "another entry"
                )
        # A directory of several segments may lie inside another entry's
        # copy: one clone would hold the other (package-sdk check).
        for entry in entries.values():
            for outer in entries.values():
                if entry.directory.casefold().startswith(outer.directory.casefold() + "/"):
                    raise CatalogError(
                        f"repositories.{entry.key}.directory {entry.directory!r} lies inside "
                        f"the directory {outer.directory!r} of {outer.key}"
                    )
        superproject = spec.get("superproject")
        if superproject is not None and (
            not isinstance(superproject, str) or superproject not in entries
        ):
            raise CatalogError("superproject must be a key of the catalog")
        return cls(field=field, entries=entries, superproject=superproject)

    def resolve(self, name: str) -> CatalogEntry | None:
        """The entry a key or an alias names, case-insensitively; None if none."""
        folded = name.strip().casefold()
        if not folded:
            return None
        for entry in self.entries.values():
            if folded == entry.key.casefold() or folded in (a.casefold() for a in entry.aliases):
                return entry
        return None

    def entry_of(self, task: Mapping[str, Any]) -> CatalogEntry:
        """The repository of a task, by its field; :class:`RepositoryBlocked` if none.

        There is no default: a task run in a repository it did not name is
        work done in the wrong place without anyone noticing.
        """
        fields = task.get("customFields")
        value = fields.get(self.field) if isinstance(fields, Mapping) else None
        where = f"customFields.{self.field}"
        if value is None or (isinstance(value, str) and not value.strip()):
            raise RepositoryBlocked(
                REPOSITORY_UNKNOWN, f"the task has no repository key ({where} is empty)"
            )
        if not isinstance(value, str):
            raise RepositoryBlocked(
                REPOSITORY_UNKNOWN, f"{where} must be a string, got {type(value).__name__}"
            )
        entry = self.resolve(value)
        if entry is None:
            # Echoed only when it looks like a name: a URL or a path put in the
            # field may carry a credential or name a host, and the reason is
            # durable.
            shown = value.strip()
            named = f"{where} {shown!r}" if _NAME_RE.match(shown) else where
            raise RepositoryBlocked(
                REPOSITORY_UNKNOWN, f"{named} is not a repository of this agent's catalog"
            )
        return entry


def _entry(key: Any, value: Any, *, publish: bool) -> CatalogEntry:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise CatalogError(f"repository key {key!r} is not a lowercase ASCII key")
    if not isinstance(value, Mapping):
        raise CatalogError(f"repositories.{key} must be an object")
    unknown = sorted(set(value) - _ENTRY_FIELDS)
    if unknown:
        raise CatalogError(f"unknown field(s) of repositories.{key}: {', '.join(unknown)}")
    url = value.get("url")
    if not isinstance(url, str) or not url.strip():
        raise CatalogError(f"repositories.{key}.url is missing")
    if _UNRESOLVED_RE.search(url):
        # Installation variables are resolved when the package is installed;
        # one left here would be cloned as a literal path.
        raise CatalogError(f"repositories.{key}.url is an unresolved installation variable")
    _check_url(key, url.strip())
    base_ref = value.get("baseRef", "HEAD")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise CatalogError(f"repositories.{key}.baseRef must be a branch name")
    directory = value.get("directory", key)
    if (
        not isinstance(directory, str)
        or len(directory) > _DIRECTORY_MAX_LENGTH
        or not _DIRECTORY_RE.match(directory)
    ):
        raise CatalogError(
            f"repositories.{key}.directory must be a lowercase relative path: a name or "
            "segments joined by /, without .., an empty segment or a backslash"
        )
    own_publish = value.get("publish", publish)
    if not isinstance(own_publish, bool):
        raise CatalogError(f"repositories.{key}.publish must be a boolean")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(a, str) and _ALIAS_RE.match(a) for a in aliases
    ):
        raise CatalogError(f"repositories.{key}.aliases must be a list of names")
    return CatalogEntry(
        key=key,
        url=url.strip(),
        base_ref=base_ref.strip(),
        directory=directory,
        publish=own_publish,
        aliases=tuple(aliases),
    )


def _check_url(key: str, url: str) -> None:
    """An address git is given to clone: https without credentials, or local.

    The schema allows https only; ``file://`` and an absolute path are kept
    for local debugging and tests, as a directory is in the one-repository
    form. The value goes into
    git argv (after ``--``) and into logs, so what could be an option, carry a
    credential or a query is refused.
    """
    if "://" not in url and url.startswith("/"):
        return  # a repository on this host, as ``revision.mirror`` takes it
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # ``https://[abc/x`` or a port out of range: urlsplit's own words may
        # echo the value, which goes into a durable reason.
        raise CatalogError(f"repositories.{key}.url is not a valid URL") from None
    scheme = parts.scheme.lower()
    if scheme not in {"https", "file"} or (scheme == "https" and not parts.hostname):
        raise CatalogError(f"repositories.{key}.url must be an https URL")
    if port is not None and not 1 <= port <= 65535:
        raise CatalogError(f"repositories.{key}.url must have a port from 1 to 65535")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CatalogError(f"repositories.{key}.url must not carry credentials")
    if parts.query or parts.fragment:
        raise CatalogError(f"repositories.{key}.url must not have a query or a fragment")


def _url_identity(url: str) -> str:
    """An address as package-sdk compares them: no case, trailing ``/`` or ``.git``."""
    return url.lower().rstrip("/").removesuffix(".git")


#: Builds the pool of one entry; None when ``create`` is false and the host
#: holds no mirror of it (nothing of it can be on this host then).
PoolFactory = Callable[[CatalogEntry, bool], ExecutionWorkspacePool | None]


class RepositoryPools:
    """Working copies of every repository of a catalog, a pool per key, made lazily.

    ``make_pool`` turns an entry into its pool (``revision.workspace_pool_of``
    mirrors the URL on this host); it is called once per key, the first time
    a task of that repository is taken, so a catalog of ten repositories
    costs nothing until their work comes.

    The lock of the process guards only the choice of a pool. A pool is made
    under a lock of its key: the first clone of one repository, which may
    take minutes, keeps only tasks of that repository waiting.
    """

    def __init__(self, catalog: RepositoryCatalog, root: Path, make_pool: PoolFactory) -> None:
        self.catalog = catalog
        self.root = root
        self._make_pool = make_pool
        self._pools: dict[str, ExecutionWorkspacePool] = {}
        self._making: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def pool_for(self, entry: CatalogEntry) -> ExecutionWorkspacePool:
        pool = self._pool(entry, create=True)
        assert pool is not None  # a factory asked to create does
        return pool

    def existing_pool(self, entry: CatalogEntry) -> ExecutionWorkspacePool | None:
        """The pool of ``entry`` if copies of it can be on this host; None otherwise.

        Never clones: a repository this host has no mirror of holds no copy here.
        """
        return self._pool(entry, create=False)

    def _pool(self, entry: CatalogEntry, *, create: bool) -> ExecutionWorkspacePool | None:
        with self._lock:
            pool = self._pools.get(entry.key)
            if pool is not None:
                return pool
            making = self._making.setdefault(entry.key, threading.Lock())
        with making:
            with self._lock:
                pool = self._pools.get(entry.key)
            if pool is not None:
                return pool  # made while this thread waited
            pool = self._make_pool(entry, create)
            if pool is not None:
                with self._lock:
                    self._pools[entry.key] = pool
                logger.info("working copies of %s: pool made", entry.key)
            return pool

    def settle_change(self, task_key: str, previous: str | None, entry: CatalogEntry) -> None:
        """Clear the way for ``entry`` when the task ran under another key before.

        ``previous`` is the key of the task's newest ``execution.workspace``
        checkpoint. A copy of the old repository without work of its own is
        removed with its branch; one with work raises
        :class:`RepositoryBlocked` (:data:`REPOSITORY_CHANGED`) and is left
        as it is. An old key the catalog no longer knows has no pool to look
        in: the change is logged and the task goes on.
        """
        if not previous:
            return
        old = self.catalog.resolve(previous)
        if old is None:
            logger.warning(
                "%s ran under %r, which is no longer in the catalog; its copy is not looked for",
                task_key,
                previous[:80],
            )
            return
        if old.key == entry.key:
            return
        pool = self.existing_pool(old)
        if pool is None:
            return
        work = pool.discard(task_key)
        if work is not None:
            raise RepositoryBlocked(
                REPOSITORY_CHANGED,
                f"the task moved from {old.key} to {entry.key}, and its copy of {old.key} "
                f"holds {work}; move the work or drop it, then return the task",
            )
        logger.info("%s moved from %s to %s: the old copy is removed", task_key, old.key, entry.key)


def previous_repository(checkpoints: list[Mapping[str, Any]], kind: str) -> str | None:
    """``repositoryKey`` of the newest checkpoint of ``kind`` that has one.

    ``checkpoints`` are the task's, newest first, as ``/runs/{id}/context``
    gives them.
    """
    for checkpoint in checkpoints:
        if checkpoint.get("kind") != kind:
            continue
        key = (checkpoint.get("data") or {}).get("repositoryKey")
        if isinstance(key, str) and key:
            return key
    return None


__all__ = [
    "REPOSITORY_CHANGED",
    "REPOSITORY_UNKNOWN",
    "CatalogEntry",
    "CatalogError",
    "PoolFactory",
    "RepositoryBlocked",
    "RepositoryCatalog",
    "RepositoryPools",
    "is_catalog",
    "previous_repository",
]
