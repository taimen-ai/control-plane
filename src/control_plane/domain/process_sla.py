"""SLA deadlines of steps and of the process: recipes and their arithmetic (CP-ADR-0078 §1, §3).

A deadline is the ``due`` of a waiting step (``human``, ``approve``, ``call``,
``recall``, ``listen``) or ``spec.due`` of the process. Its forms:

- ``P2D`` — astronomical time, as before;
- ``{at: <cel>}`` — a moment or a duration from the data, as before;
- ``{duration: PT4H}`` — astronomical time that may carry ``warnBefore``;
- ``{workdays: n}`` — ``cal.addWorkdays(entry, n)``: the same time of day
  ``n`` working days on; from outside the working hours of a calendar that
  has them, the count starts at its next working interval;
- ``{workhours: n}`` — ``n`` hours of working time (``cal.addWorkingTime``).

``n`` of ``workdays`` and ``workhours``, of the deadline and of its
``warnBefore`` alike, may be ``{expr: <cel>}``: a non-negative integer the
engine computes once, when the step is entered (CP-ADR-0081, amendment 2026-10-03 G1).

``calendar`` names the calendar of the working units (the process's
``spec.calendar`` by default); ``warnBefore`` — a duration, ``{workdays}`` or
``{workhours}`` before the deadline, counted back from it on the same
calendar. Without ``warnBefore`` there is no warning.

A *recipe* is how the engine stores a timer's moment in its state (plain
JSON): ``{kind: duration, value}``, ``{kind: at, path}``, ``{kind: workdays,
n, calendar}``, ``{kind: workhours, hours, calendar}`` (``expr`` in place of
``n``/``hours`` — the path of the expression — until the engine computes it,
``from`` after: the path it was computed from), ``{kind: before,
due, span}`` (the warning threshold) and the engine's own ``{kind: after,
due, after}`` of escalations. The engine computes a recipe from a base
moment; the working units are computed here, by the calendar version the
input carries, so a live run, a package test and a replay agree.

Pure functions over plain values; no I/O.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import Any

from control_plane.domain.calendar import Calendar, CalendarError

# Timer kinds of a deadline (CP-ADR-0074 §8, amendment 2026-09-29).
SLA = "sla"
SLA_WARNING = "sla_warning"
SLA_TIMERS = (SLA, SLA_WARNING)
# Facts of SLA deadlines the engine emits; the application adds the step's attempt.
SLA_EVENT_PREFIX = "process.sla_"
# Scopes of a deadline; a timer of the process's deadline names this element.
STEP = "step"
PROCESS = "process"
# Working units of a deadline: recipes of these kinds read a calendar.
WORKING = ("workdays", "workhours")

# Units the remainder of a frozen deadline is kept in (``process_timers.remaining_unit``,
# CP-ADR-0078 §4): seconds of wall-clock time, seconds of working time, and
# ``workdays`` — whole working days times ``DAY`` plus the second of the day
# the deadline falls on, local to the calendar.
WALL = "wall"
WORKING_SECONDS = "working_seconds"
WORKDAYS = "workdays"
DAY = 86400

# The state of a computed deadline kept in the instance's state: ``pending``
# until a threshold passes, then ``warning`` and ``breached``; ``failed`` when
# it could not be computed (then ``error`` says why).
PENDING = "pending"
WARNING = "warning"
BREACHED = "breached"
FAILED = "failed"


class DeadlineError(ValueError):
    """A deadline cannot be computed: no calendar, or the calendar cannot answer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def due_recipe(value: Any, path: str, calendar: str | None) -> dict[str, Any]:
    """The recipe of a ``due`` at ``path``; ``calendar`` — the process's ``spec.calendar``."""
    if isinstance(value, str):
        return {"kind": "duration", "value": value}
    if "at" in value:
        return {"kind": "at", "path": path + "/at"}
    if "duration" in value:
        return {"kind": "duration", "value": value["duration"]}
    return _working(value, value.get("calendar") or calendar, path)


def warn_recipe(
    value: Any, due: Mapping[str, Any], calendar: str | None, path: str = ""
) -> dict[str, Any] | None:
    """The recipe of the warning threshold of the ``due`` at ``path``; ``None`` without
    ``warnBefore``."""
    if not isinstance(value, Mapping) or value.get("warnBefore") is None:
        return None
    span = value["warnBefore"]
    key = value.get("calendar") or calendar
    back = (
        {"kind": "duration", "value": span}
        if isinstance(span, str)
        else _working(span, key, warn_path(path))
    )
    return {"kind": "before", "due": dict(due), "span": back}


def warn_path(path: str) -> str:
    """Where the ``warnBefore`` of the ``due`` at ``path`` stands."""
    return path + "/warnBefore"


def _working(value: Mapping[str, Any], calendar: str | None, path: str) -> dict[str, Any]:
    unit = "workdays" if "workdays" in value else "workhours"
    amount = value[unit]
    if isinstance(amount, Mapping):
        return {"kind": unit, "expr": f"{path}/{unit}/expr", "calendar": calendar}
    if unit == "workdays":
        return {"kind": unit, "n": int(amount), "calendar": calendar}
    return {"kind": unit, "hours": amount, "calendar": calendar}


# The largest amounts of the working units, as the schema bounds the numbers.
MAX_AMOUNT = {"workdays": 1000, "workhours": 10000}


def amount_of(value: Any, unit: str, path: str) -> int:
    """The amount an ``{expr}`` of a working unit gave; :class:`DeadlineError` otherwise.

    A non-negative integer no larger than a number of the unit may be; a
    double with no fraction (a JSON number read untyped) counts as one.
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeadlineError(
            "expression_error",
            f"{path}: {unit} must be a non-negative integer, the expression gave"
            f" {type(value).__name__}",
        )
    if not 0 <= value <= MAX_AMOUNT[unit]:
        raise DeadlineError(
            "expression_error",
            f"{path}: {unit} must be from 0 to {MAX_AMOUNT[unit]}, the expression gave {value}",
        )
    return value


def computed(recipe: Mapping[str, Any], amount: int) -> dict[str, Any]:
    """A working-unit recipe with the amount its expression gave in place of the expression."""
    out = {k: v for k, v in recipe.items() if k != "expr"}
    out["n" if recipe["kind"] == "workdays" else "hours"] = amount
    out["from"] = recipe["expr"]
    return out


def computed_amounts(recipe: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the expressions of a recipe gave, by their paths: what the journal records."""
    if not isinstance(recipe, Mapping):
        return {}
    out: dict[str, Any] = {}
    if recipe.get("kind") in WORKING and recipe.get("from"):
        out[str(recipe["from"])] = recipe["n" if recipe["kind"] == "workdays" else "hours"]
    for part in ("due", "span", "after"):
        out.update(computed_amounts(recipe.get(part)))
    return out


def working(
    recipe: Mapping[str, Any],
    base: datetime,
    calendars: Mapping[str, Calendar],
    *,
    back: bool = False,
) -> tuple[datetime, bool]:
    """A ``workdays``/``workhours`` recipe from ``base`` (before it when ``back``)."""
    key = recipe.get("calendar")
    calendar = _calendar(key, calendars)
    try:
        if recipe["kind"] == "workhours":
            hours = timedelta(hours=float(recipe["hours"]))
            answer = calendar.add_working_time_at(base, -hours if back else hours)
            return answer.value, answer.provisional
        n = int(recipe["n"])
        if back:
            answer = calendar.add_workdays_at(base, -n)
            return answer.value, answer.provisional
        provisional = False
        if calendar.working_hours is not None:
            start = calendar.next_working_time_at(base)
            base, provisional = start.value, start.provisional
        answer = calendar.add_workdays_at(base, n)
        return answer.value, provisional or answer.provisional
    except CalendarError as exc:
        raise DeadlineError(exc.code, f"calendar {key!r}: {exc.message}") from None


def _calendar(key: Any, calendars: Mapping[str, Calendar]) -> Calendar:
    if key is None:
        raise DeadlineError(
            "calendar_missing", "working units are counted by a calendar, and none is named"
        )
    calendar = calendars.get(str(key))
    if calendar is None:
        raise DeadlineError("calendar_missing", f"calendar {key!r} is not available")
    return calendar


def _unit_recipe(recipe: Mapping[str, Any]) -> Mapping[str, Any]:
    """The recipe whose unit a timer's remainder is kept in: a threshold's is its deadline's."""
    return recipe["due"] if recipe.get("kind") == "before" else recipe


def remainder(
    recipe: Mapping[str, Any],
    due: datetime,
    at: datetime,
    calendars: Mapping[str, Calendar],
) -> tuple[float | None, str]:
    """What is left of a deadline timer due at ``due`` when frozen at ``at``, and its unit.

    ``None`` for a deadline from the data (``{at}``): it is recomputed from the
    data on resume and does not move. A duration keeps wall-clock seconds;
    ``workhours``, and ``workdays`` of a calendar with working hours, keep
    seconds of working time; ``workdays`` of a calendar without them keep
    whole working days and the time of day (``WORKDAYS``). A timer already due
    keeps nothing: it fires on resume.
    """
    own = _unit_recipe(recipe)
    kind = own.get("kind")
    if kind == "at":
        return None, WALL
    if due <= at:
        return 0.0, WALL
    if kind not in WORKING:
        return (due - at).total_seconds(), WALL
    calendar = _calendar(own.get("calendar"), calendars)
    try:
        if kind == "workhours" or calendar.working_hours is not None:
            spent = calendar.working_time_between_at(at, due).value
            return spent.total_seconds(), WORKING_SECONDS
        days = calendar.workdays_between_at(at, due).value
        local = due.astimezone(calendar.zone)
        of_day = local.hour * 3600 + local.minute * 60 + local.second
        return float(max(0, days) * DAY + of_day), WORKDAYS
    except CalendarError as exc:
        raise DeadlineError(exc.code, f"calendar {own.get('calendar')!r}: {exc.message}") from None


def thaw(
    recipe: Mapping[str, Any],
    remaining: float,
    unit: str,
    at: datetime,
    calendars: Mapping[str, Calendar],
) -> tuple[datetime, bool]:
    """The moment a frozen remainder in a working ``unit`` runs out when resumed at ``at``.

    ``WORKING_SECONDS`` — that much working time from ``at``
    (``cal.addWorkingTime``). ``WORKDAYS`` — the kept time of day that many
    working days after the day of ``at``; with no whole day left and a resume
    on a day off, the next working day.
    """
    own = _unit_recipe(recipe)
    calendar = _calendar(own.get("calendar"), calendars)
    try:
        if unit == WORKING_SECONDS:
            answer = calendar.add_working_time_at(at, timedelta(seconds=remaining))
            return answer.value, answer.provisional
        days, of_day = divmod(int(remaining), DAY)
        start = calendar.local_date(at)
        today = calendar.is_workday(start)
        if not days and not today.value:
            days = 1
        moved = calendar.add_workdays(start, days)
        wall = datetime.combine(
            moved.value, time(of_day // 3600, of_day % 3600 // 60, of_day % 60), calendar.zone
        )
        return wall.astimezone(at.tzinfo), moved.provisional or today.provisional
    except CalendarError as exc:
        raise DeadlineError(exc.code, f"calendar {own.get('calendar')!r}: {exc.message}") from None


def pause(
    recipe: Mapping[str, Any],
    unit: str,
    before: datetime,
    after: datetime,
    calendars: Mapping[str, Calendar],
) -> dict[str, Any] | None:
    """How far a resume moved a timer from ``before`` to ``after``, in its frozen ``unit``.

    The pause a recount adds back (:func:`extend`) when it counts the timer
    from its base again: a migration, a new calendar version. ``None`` when it
    did not move. ``WORKDAYS`` keeps whole working days times ``DAY``, as the
    remainder does.
    """
    if after <= before:
        return None
    key = None
    if unit == WALL:
        amount = (after - before).total_seconds()
    else:
        key = _unit_recipe(recipe).get("calendar")
        calendar = _calendar(key, calendars)
        try:
            if unit == WORKING_SECONDS:
                amount = calendar.working_time_between_at(before, after).value.total_seconds()
            else:
                amount = float(max(0, calendar.workdays_between_at(before, after).value) * DAY)
        except CalendarError as exc:
            raise DeadlineError(exc.code, f"calendar {key!r}: {exc.message}") from None
    if amount <= 0:
        return None
    return {"amount": amount, "unit": unit, "calendar": key}


def add_pause(pauses: list[dict[str, Any]], more: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The pauses of a timer with one more: one of the same unit and calendar adds up."""
    out = [dict(p) for p in pauses]
    if out and (out[-1]["unit"], out[-1]["calendar"]) == (more["unit"], more["calendar"]):
        out[-1]["amount"] += more["amount"]
    else:
        out.append(dict(more))
    return out


def extend(
    moment: datetime, pause: Mapping[str, Any], calendars: Mapping[str, Calendar]
) -> tuple[datetime, bool]:
    """``moment`` moved on by a kept pause (:func:`pause`), by the calendar it was counted on."""
    amount = float(pause["amount"])
    if pause["unit"] == WALL:
        return moment + timedelta(seconds=amount), False
    key = pause.get("calendar")
    calendar = _calendar(key, calendars)
    try:
        if pause["unit"] == WORKING_SECONDS:
            answer = calendar.add_working_time_at(moment, timedelta(seconds=amount))
        else:
            answer = calendar.add_workdays_at(moment, int(amount // DAY))
    except CalendarError as exc:
        raise DeadlineError(exc.code, f"calendar {key!r}: {exc.message}") from None
    return answer.value, answer.provisional


def calls_calendar(recipe: Mapping[str, Any] | None, key: Any) -> bool | None:
    """Whether a working-unit recipe (or one made of them) reads calendar ``key``.

    ``None`` for a recipe this module does not know: the engine decides.
    """
    if not isinstance(recipe, Mapping):
        return False
    kind = recipe.get("kind")
    if kind in WORKING:
        return key is None or recipe.get("calendar") == key
    if kind == "before":
        return bool(calls_calendar(recipe.get("due"), key)) or bool(
            calls_calendar(recipe.get("span"), key)
        )
    return None


def overdue_seconds(
    due_at: datetime, at: datetime, stops: Sequence[Mapping[str, Any]] | None = None
) -> int:
    """Whole seconds ``at`` is past ``due_at``; never negative.

    ``stops`` are the suspensions (``{from, to}``) the deadline sat through
    already past: its clock stood still, so what of them falls between
    ``due_at`` and ``at`` is not overdue time. A suspension still going on
    has no ``to``.
    """
    overdue = (at - due_at).total_seconds()
    for stop in stops or ():
        begin, end = _moment(stop.get("from")), _moment(stop.get("to")) or at
        if begin is None:
            continue
        overdue -= max(0.0, (min(end, at) - max(begin, due_at)).total_seconds())
    return max(0, int(overdue))


def record(
    *,
    due_at: str | None,
    warn_at: str | None,
    provisional: bool,
    timer: str | None = None,
    warn_timer: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The deadline kept in the state: of an activity (``sla``) or of the instance (``sla``)."""
    return {
        "state": FAILED if error is not None else PENDING,
        "dueAt": due_at,
        "warnAt": warn_at,
        "provisional": provisional,
        "timer": timer,
        "warnTimer": warn_timer,
        "error": dict(error) if error is not None else None,
    }


# --- the projection (CP-ADR-0078 §6) ---------------------------------------------------

# States of a deadline on read, worst first: an instance shows the worst of its
# process deadline and the deadlines of its open steps.
SHOWN = ("breached", "warning", "unknown", "paused", "ok", "none")


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def frozen(found: Mapping[str, Any], timers: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The frozen timer of a deadline, if its clock stands; ``None`` while it runs.

    Clocks stand per thread, not per instance: while an instance is suspended
    the timers of ``onEvent`` blocks and ``correlate`` steps run on, and so do
    their deadlines (CP-ADR-0078 §4). A deadline already breached or not
    computed is not frozen whatever its timer says, and neither is one whose
    moment had passed when its timer froze: it was breached by the clock
    before the pause, only not yet recorded by the worker (FR-021); its
    overdue time stands in ``overdueStops`` instead.
    """
    if found.get("state") in (BREACHED, FAILED):
        return None
    timer = timers.get(str(found.get("timer") or ""))
    if not isinstance(timer, Mapping) or timer.get("state") != "frozen":
        return None
    was_due, frozen_at = _moment(timer.get("frozenFrom")), _moment(timer.get("frozenAt"))
    if was_due is not None and frozen_at is not None and was_due <= frozen_at:
        return None
    return timer


def shown(
    found: Mapping[str, Any] | None,
    *,
    now: datetime,
    timers: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str, int | None]:
    """A deadline record as the projection shows it: ``(due, slaState, overdueSeconds)``.

    The state is computed from ``dueAt``, ``warnAt`` and ``now``, not from
    whether the worker fired the timers (FR-021): a deadline past ``dueAt`` is
    ``breached`` before its breach is recorded. A deadline that could not be
    computed is ``unknown``; a deadline whose timer is frozen is ``paused``
    with the remainder the timer keeps and its unit (``remainingUnit``).
    ``overdueSeconds`` leaves out the suspensions the deadline sat through
    already past (``overdueStops``, the one going on included), as the event
    of the breach and the step's exit do.
    ``remainingSeconds`` of a running deadline is the wall-clock time to
    ``dueAt``.
    """
    if not isinstance(found, Mapping):
        return None, "none", None
    provisional = bool(found.get("provisional"))
    due_at = _moment(found.get("dueAt"))
    warn_at = _moment(found.get("warnAt"))
    due: dict[str, Any] = {
        "dueAt": None,
        "warnAt": None,
        "provisional": provisional,
        "remainingSeconds": None,
        "remainingUnit": None,
    }
    if found.get("state") == FAILED or due_at is None:
        return due, "unknown", None
    timer = frozen(found, timers)
    if timer is not None:
        kept = timer.get("remaining")
        if kept is not None:
            due["remainingSeconds"] = int(kept)
            due["remainingUnit"] = timer.get("remainingUnit") or WALL
        return due, "paused", None
    due.update(dueAt=found.get("dueAt"), warnAt=found.get("warnAt"))
    if found.get("state") == BREACHED or due_at <= now:
        return due, "breached", overdue_seconds(due_at, now, found.get("overdueStops"))
    due.update(remainingSeconds=int((due_at - now).total_seconds()), remainingUnit=WALL)
    if found.get("state") == WARNING or (warn_at is not None and warn_at <= now):
        return due, "warning", None
    return due, "ok", None


def worst(states: list[str]) -> str:
    """The worst of the states of an instance's deadlines; ``none`` without any."""
    return min(states, key=SHOWN.index, default="none")


def open_moments(
    records: list[Mapping[str, Any] | None],
    timers: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None]:
    """``(sla_due_at, sla_warn_at)``: the earliest deadline and warning of running deadlines.

    What ``GET /process-instances?slaState=`` filters on; a deadline that was
    not computed has neither, and neither has one whose clock stands (its
    timer is frozen).
    """
    dues: list[datetime] = []
    warns: list[datetime] = []
    for found in records:
        if not isinstance(found, Mapping) or found.get("state") == FAILED:
            continue
        if frozen(found, timers) is not None:
            continue
        due_at = _moment(found.get("dueAt"))
        warn_at = _moment(found.get("warnAt"))
        if due_at is not None:
            dues.append(due_at)
        if warn_at is not None:
            warns.append(warn_at)
    return min(dues, default=None), min(warns, default=None)
