"""When a running cycle finishes, timed from when each remainingTime arrived.

`now() + remainingTime` recomputed on every flatten puts the finish late by
however old the reading is. The coordinator re-flattens whenever any resource
updates, and an MW7300B pushes /operational/state/vs/0 only about every 10 s,
so its finish crept up to 6 s late between reads (issue #772). This store
records the finish when a new remainingTime value arrives, and the coordinator
merges it onto the rep under FIELD for the finish_time descriptors.

A /device/0 sweep's value runs 2-3 s behind a push, so a sweep that would move
the finish later by no more than that lag is ignored. A pause freezes the
finish and resuming re-anchors it; so does a countdown that stops moving while
the cycle still runs (an oven preheating). Rounding and the deadband belong to
the reader (finish_at and the sensor's hysteresis).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

REMAINING = "x.com.samsung.da.remainingTime"
STATE = "x.com.samsung.da.state"
DELAY_END = "x.com.samsung.da.delayEndTime"

# Synthetic, read-time only, like cloudcourse.FIELD: never cached, written to
# the device, or part of a diagnostics dump.
FIELD = "x.localthings.finish"
# The same for a pending laundry delay: when delayEndTime points.
DELAY_FIELD = "x.localthings.delay_finish"

# Samsung's spellings and the OCF ones a board may report instead.
_PAUSED = frozenset({"pause", "paused"})
_IDLE = frozenset({"ready", "end", "stop", "idle"})

# How far behind a push the sweep's reading runs, with margin (#772).
_SWEEP_LAG = timedelta(seconds=5)
# A reading unchanged for longer than one step plus this has stalled.
_STALL_SLACK = timedelta(seconds=10)
# A delay tick re-anchored within this of the held finish is arrival jitter,
# not a change: a WF8000A washer's delay finish flipped a minute back and forth
# every tick when recomputed from now() on each read.
_DELAY_JITTER = timedelta(seconds=30)


def remaining_seconds(raw) -> int | None:
    """'HH:MM:SS' (or 'MM:SS') -> total seconds, or None."""
    if not isinstance(raw, str):
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        parts = [0, *parts]
    if len(parts) != 3:
        return None
    h, m, s = parts
    return h * 3600 + m * 60 + s


def _has_seconds(total_s: int) -> bool:
    """Minute-only boards park the seconds at :00 or :59 (NX60T8311SS, #457)."""
    return total_s % 60 not in (0, 59)


def _step(seconds: bool) -> timedelta:
    return timedelta(seconds=1 if seconds else 60)


@dataclass
class _Anchor:
    raw: object = None
    raw_at: datetime | None = None
    finish: datetime | None = None
    paused: bool = False
    seconds: bool = False


class FinishAnchors:
    """Per actual href. Mutated from whichever thread applied the update, so
    every access takes the lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._anchors: dict[str, _Anchor] = {}
        self._delays: dict[str, _Anchor] = {}

    def observe(self, href: str, rep: dict, source: str, now: datetime) -> bool:
        """Learn from one applied rep; True if either finish changed."""
        delay_changed = self._observe_delay(href, rep, now)
        return self._observe_remaining(href, rep, source, now) or delay_changed

    def _observe_delay(self, href: str, rep: dict, now: datetime) -> bool:
        """Anchor delayEndTime when its value changes. Before Start it holds
        still, so once stalled the finish walks with the clock, which is
        what the appliance will do when started."""
        if DELAY_END not in rep:
            return False
        raw = rep.get(DELAY_END)
        total_s = remaining_seconds(raw)
        with self._lock:
            a = self._delays.setdefault(href, _Anchor())
            before = a.finish
            if not total_s:
                a.finish = None
            else:
                finish = now + timedelta(seconds=total_s)
                if raw != a.raw:
                    if a.finish is None or abs(finish - a.finish) > _DELAY_JITTER:
                        a.finish = finish
                    a.raw_at = now
                elif a.raw_at is None or now - a.raw_at > _step(False) + _STALL_SLACK:
                    a.finish = finish
            a.raw = raw
            return a.finish != before

    def _observe_remaining(self, href: str, rep: dict, source: str, now: datetime) -> bool:
        if REMAINING not in rep:
            return False
        raw = rep.get(REMAINING)
        state = str(rep.get(STATE) or "").lower()
        total_s = remaining_seconds(raw)
        with self._lock:
            a = self._anchors.setdefault(href, _Anchor())
            before = a.finish
            if total_s and _has_seconds(total_s):
                a.seconds = True
            if raw != a.raw:
                a.raw_at = now
            if state in _PAUSED:
                a.paused = True
                # Held while it is still ahead; once the pause outlasts it,
                # resuming now is the earliest it can finish.
                if total_s and (a.finish is None or a.finish <= now):
                    a.finish = now + timedelta(seconds=total_s)
            elif not total_s or state in _IDLE:
                a.finish, a.paused = None, False
            else:
                finish = now + timedelta(seconds=total_s)
                stalled = (
                    raw == a.raw
                    and a.raw_at is not None
                    and now - a.raw_at > _step(a.seconds) + _STALL_SLACK
                )
                if a.paused or a.finish is None or raw != a.raw or stalled:
                    stale = (
                        source == "sweep"
                        and not a.paused
                        and a.finish is not None
                        and a.finish < finish <= a.finish + _step(a.seconds) + _SWEEP_LAG
                    )
                    if stale:
                        # Not recorded, so the push carrying it still anchors.
                        return False
                    a.finish, a.paused = finish, False
            # Compared with the previous reading only: adding time on the
            # panel can bring back a value seen earlier in the cycle.
            a.raw = raw
            return a.finish != before

    def fields(self) -> dict[str, dict]:
        """{href: FIELD value} for every href with a running finish."""
        with self._lock:
            return {
                href: {"finish": a.finish, "seconds": a.seconds}
                for href, a in self._anchors.items()
                if a.finish is not None
            }

    def delay_fields(self) -> dict[str, datetime]:
        """{href: DELAY_FIELD value} for every href with a pending delay."""
        with self._lock:
            return {href: a.finish for href, a in self._delays.items() if a.finish is not None}

    def delay_field(self, href: str) -> datetime | None:
        with self._lock:
            a = self._delays.get(href)
            return a.finish if a else None

    def field(self, href: str) -> dict | None:
        with self._lock:
            a = self._anchors.get(href)
            if a is None or a.finish is None:
                return None
            return {"finish": a.finish, "seconds": a.seconds}


def finish_at(rep: dict, now: datetime | None = None) -> datetime | None:
    """The finish at the precision the board reports: to the second where it
    reports seconds, else rounded to the *nearest* minute (rounding down ran
    ahead of the panel, #771). Falls back to now() + remainingTime when no
    anchor is merged (a dump, or before the first reading)."""
    anchor = rep.get(FIELD)
    if isinstance(anchor, dict) and isinstance(anchor.get("finish"), datetime):
        finish, seconds = anchor["finish"], bool(anchor.get("seconds"))
    else:
        total_s = remaining_seconds(rep.get(REMAINING))
        if not total_s:
            return None
        finish = (now or datetime.now(UTC)) + timedelta(seconds=total_s)
        seconds = _has_seconds(total_s)
    if seconds:
        return finish.replace(microsecond=0)
    return (finish + timedelta(seconds=30)).replace(second=0, microsecond=0)


def running_finish(rep: dict, active: bool) -> datetime | None:
    """finish_time's value: the finish while `active`, the held one while
    paused, else None. Shared by every family's descriptor."""
    if active or (str(rep.get(STATE) or "").lower() in _PAUSED and FIELD in rep):
        return finish_at(rep)
    return None


def resolution(rep: dict) -> timedelta:
    """The smallest step this rep's finish can move by."""
    anchor = rep.get(FIELD)
    return _step(bool(anchor.get("seconds")) if isinstance(anchor, dict) else False)
