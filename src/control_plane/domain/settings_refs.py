"""``settings`` in the expressions of an object of a package (CP-ADR-0081 §6).

An object knows its package by ``package_objects``; the variable
``settings`` of its expressions is typed by the schema of the active
revision of that package's settings (at a plan — by the schema in the
package's files), as ``data`` is typed by ``spec.data``. A reference the
schema does not declare is ``settings_ref_unknown`` — so is any reference in
an object without a package, or of a package that declares no settings — and
a declared field of a type that does not fit the place of the expression is
``settings_ref_type``; both with the path of the expression. Other faults of
an expression keep their codes.

**Typing.** The schema of the settings is the variable's type, every field
nullable: a required field without a ``default`` is absent until it is
saved, and ``has(settings.<field>)`` tells it (§1). ``x-ref`` is a string.

**How a fault is told.** An expression is compiled with ``settings`` typed;
when that fails as a type error, it is compiled again with ``settings``
untyped (``map(string, dyn)``): the fault is the settings' when the untyped
compilation passes. Then the paths it reads under ``settings`` name an
undeclared field (``settings_ref_unknown``) or the types do not fit
(``settings_ref_type``).

Pure functions over plain values; no I/O.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from control_plane.domain.cel_profile import (
    EXPRESSION_TYPE_ERROR,
    Environment,
    ExpressionError,
    Program,
)

SETTINGS = "settings"
REF_UNKNOWN = "settings_ref_unknown"
REF_TYPE = "settings_ref_type"
# The variable of an object whose settings are unknown: any read is an error of its own.
UNTYPED: Mapping[str, Any] = {"type": "object"}

JsonSchema = Mapping[str, Any]


@dataclass(frozen=True)
class SettingsScope:
    """What ``settings`` is for one object.

    ``package`` — the key of the object's package (``None``: the object is
    not from a package); ``schema`` — the settings schema of the revision the
    expressions are typed by (``None``: the package declares no settings);
    ``revision`` — its number (``None`` at a plan, before the apply).
    """

    package: str | None = None
    schema: JsonSchema | None = None
    revision: int | None = None

    @property
    def variable(self) -> JsonSchema:
        """The type of the variable ``settings`` in the environments of the object."""
        if self.schema is None:
            return UNTYPED
        return nullable(self.schema)


NONE = SettingsScope()


def nullable(schema: JsonSchema) -> dict[str, Any]:
    """``schema`` without ``required`` at any level: every field may be absent."""
    out: dict[str, Any] = {k: v for k, v in schema.items() if k not in ("required", "default")}
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        out["properties"] = {
            name: nullable(node) if isinstance(node, Mapping) else node
            for name, node in properties.items()
        }
    items = schema.get("items")
    if isinstance(items, Mapping):
        out["items"] = nullable(items)
    return out


def settings_reads(reads: Iterable[str]) -> list[str]:
    """The paths under ``settings`` of the reads of a program."""
    return [
        path
        for path in reads
        if path == SETTINGS or path.startswith(f"{SETTINGS}.") or path.startswith(f"{SETTINGS}[")
    ]


def reads_settings(program: Program) -> bool:
    return bool(settings_reads(program.reads))


def _segments(path: str) -> list[str]:
    """``settings.window.start`` → ``[window, start]``; ``settings.tags[0]`` → ``[tags, [0]]``."""
    rest = path[len(SETTINGS) :]
    out: list[str] = []
    for part in rest.replace("[", ".[").split("."):
        if part:
            out.append(part)
    return out


def declared(schema: JsonSchema | None, path: str) -> bool:
    """Whether ``path`` (a read under ``settings``) names a field of ``schema``."""
    if schema is None:
        return False
    node: Any = schema
    for segment in _segments(path):
        if not isinstance(node, Mapping):
            return False
        if segment.startswith("["):
            if node.get("type") != "array" or not isinstance(node.get("items"), Mapping):
                return False
            node = node["items"]
            continue
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return False
        node = properties[segment]
    return True


def unknown_error(scope: SettingsScope, read: str, *, path: str | None) -> ExpressionError:
    """``settings_ref_unknown`` of the read ``read`` of an expression at ``path``."""
    if scope.package is None:
        why = "the object is not from a package: it has no settings"
        hint = None
    elif scope.schema is None:
        why = f"package {scope.package} declares no settings"
        hint = "declare spec.settings in package.yaml"
    else:
        why = f"the settings of package {scope.package} declare no such field"
        hint = None
    return ExpressionError(REF_UNKNOWN, f"{read}: {why}", path=path, hint=hint)


def check_reads(scope: SettingsScope, reads: Iterable[str], *, path: str | None) -> None:
    """``settings_ref_unknown`` for the first read under ``settings`` the scope lacks."""
    for read in settings_reads(reads):
        if scope.schema is None or not declared(scope.schema, read):
            raise unknown_error(scope, read, path=path)


def type_error(reads: Iterable[str], *, path: str | None, detail: str = "") -> ExpressionError:
    named = ", ".join(settings_reads(reads)) or SETTINGS
    return ExpressionError(
        REF_TYPE,
        f"the type of {named} does not fit the place of the expression"
        + (f": {detail}" if detail else ""),
        path=path,
    )


def compile_expression(
    scope: SettingsScope,
    build: Callable[[JsonSchema], Environment | None],
    text: str,
    *,
    path: str | None = None,
) -> Program:
    """Compile ``text`` in the environment ``build`` gives for a type of ``settings``.

    :class:`ExpressionError` — with the codes of the settings references when
    they are the fault, with the codes of the profile otherwise.
    """
    typed = scope.variable
    environment = build(typed)
    if environment is None:
        raise ExpressionError(EXPRESSION_TYPE_ERROR, "the environment cannot be built", path=path)
    try:
        program = environment.compile(text, path=path)
    except ExpressionError as exc:
        if typed is UNTYPED or exc.code != EXPRESSION_TYPE_ERROR:
            raise
        loose = loose_program(build, text, path=path)
        if loose is None or not reads_settings(loose):
            raise
        check_reads(scope, loose.reads, path=path)
        raise type_error(loose.reads, path=path, detail=exc.reason) from None
    check_reads(scope, program.reads, path=path)
    return program


def loose_program(
    build: Callable[[JsonSchema], Environment | None], text: str, *, path: str | None = None
) -> Program | None:
    """``text`` compiled with ``settings`` untyped; ``None`` when that fails too."""
    environment = build(UNTYPED)
    if environment is None:
        return None
    try:
        return environment.compile(text, path=path)
    except ExpressionError:
        return None
