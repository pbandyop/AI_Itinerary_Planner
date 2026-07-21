# %% [markdown]
# # Feasibility eval (Colab / Jupyter)
#
# **Input:** Eval CSV from the companion UI (`rag_eval.csv`) with `itinerary_json`
# on plan/edit turns (and optional `day_paces_json` for per-day pace after edits).
# Tip / RAG-only rows are ignored.
#
# **Load options (Cell 2):** `LOAD_MODE = "upload"` (file picker) or `"path"` (CSV_PATH).
#
# **Pace rules (aligned with live planner — not a flat 9h / 1-1-0 template):**
# | Pace | Start | Morning | Afternoon | Evening | Day window |
# |------|-------|---------|-----------|---------|------------|
# | relaxed | 10:00 | 1 | 1 | 0–1 food/park/market or relax | 10:00→21:00 (11h) |
# | balanced | 09:00 | 2 | 2 | same | 09:00→21:00 (12h) |
# | packed | 08:30 | ≥3 | ≥3, done by **18:00** | same | 08:30→21:00 (12.5h) |
# | All | — | — | — | day hard end **21:00**; **each** travel leg ≤ 90 min | |
#
# **Output columns (one row per plan):** identity + `overall` / `overall_reason`, then
# for each day (blank if absent) Pass/Fail checks with a reason column beside each:
# `D{n}_slots`+`_reason`, `D{n}_M/A/E`+`_reason`, `D{n}_pace`,
# `D{n}_counts` (Excel-safe `M2 A2 E1` = morning/afternoon/evening stops),
# `D{n}_pace_ok`+`_reason`, `D{n}_start_ok`/`end_ok`/`load_ok`/`travel_ok`/`ok`
# each with `_reason`. `failures_detail` still lists all fail strings.
#
# Paste cells into Colab top-to-bottom, or run this file with `# %%` cell support.

# %%
# Cell 1 — imports + rules
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

DAY_END_MIN = 21 * 60  # 21:00 hard end
PACKED_AFTERNOON_END_MIN = 18 * 60  # 18:00 packed afternoon hard end
MAX_LEG_TRAVEL_MIN = 90
MAX_DAYS = 4

# Planner day starts (minutes from midnight)
DAY_START_MIN = {
    "relaxed": 10 * 60,  # 10:00
    "moderate": 9 * 60,  # 09:00  (UI label: balanced)
    "balanced": 9 * 60,
    "packed": 8 * 60 + 30,  # 08:30
}

# Soft ranges on real POI stops only (not relax fillers).
# packed morning/afternoon: (min, None) = floor only
SOFT_RANGES: dict[str, dict[str, tuple[int, int | None]]] = {
    "relaxed": {"morning": (1, 1), "afternoon": (1, 1), "evening": (0, 1)},
    "moderate": {"morning": (2, 2), "afternoon": (2, 2), "evening": (0, 1)},
    "balanced": {"morning": (2, 2), "afternoon": (2, 2), "evening": (0, 1)},
    "packed": {"morning": (3, None), "afternoon": (3, None), "evening": (0, 1)},
}

# Evening stops must be lifestyle categories (heritage/temple/museum never OK).
EVENING_OK_CATEGORIES = frozenset(
    {"food", "cafe", "restaurant", "park", "garden", "viewpoint", "market", "shopping"}
)

BLOCKS = ("morning", "afternoon", "evening")
PF = {"PASS": "PASS", "FAIL": "FAIL", "NA": ""}


def normalize_pace(raw: Any) -> str:
    p = str(raw or "moderate").strip().lower()
    if p in {"balanced", "balance"}:
        return "moderate"
    if p in DAY_START_MIN:
        return p
    return "moderate"


def pace_label(pace: str) -> str:
    return "balanced" if pace == "moderate" else pace


def parse_clock_min(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def fmt_clock(mins: int) -> str:
    h, m = divmod(int(mins), 60)
    return f"{h:02d}:{m:02d}"


def fmt_clock_ampm(mins: int) -> str:
    """Human clock for CSV reasons (ASCII only — avoids mojibake in Excel)."""
    h, m = divmod(int(mins), 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12}:00 {suffix}"


def fmt_duration(mins: int) -> str:
    """e.g. 435 -> '7h 15m (435 min)'."""
    mins = int(mins)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m ({mins} min)"
    if h:
        return f"{h} hour{'s' if h != 1 else ''} ({mins} min)"
    return f"{mins} min"


def _block_label(bname: str) -> str:
    return {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}.get(
        bname, bname
    )


def fmt_block_counts(count_m: int, count_a: int, count_e: int) -> str:
    """Excel-safe stop counts (avoid '2-2-1' which Excel turns into a date)."""
    return f"M{count_m} A{count_a} E{count_e}"


def is_relax_note(notes: Any) -> bool:
    text = (notes or "").strip().lower()
    if not text:
        return False
    return (
        text.startswith("relax")
        or "free time" in text
        or "free morning" in text
        or "free afternoon" in text
        or "free evening" in text
        or "downtime" in text
        or "leftover" in text
    )


def block_present(block: dict | None) -> bool:
    if not isinstance(block, dict):
        return False
    stops = block.get("stops") or []
    if stops:
        return True
    return is_relax_note(block.get("notes"))


def iter_day_stops(day: dict) -> list[dict]:
    out: list[dict] = []
    for bname in BLOCKS:
        block = day.get(bname) or {}
        for s in block.get("stops") or []:
            if isinstance(s, dict):
                out.append(s)
    return out


def is_evening_ok_stop(stop: dict) -> bool:
    cat = str(stop.get("category") or "").strip().lower()
    return cat in EVENING_OK_CATEGORIES


def _pf(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _range_desc(lo: int, hi: int | None) -> str:
    if hi is None:
        return f">={lo}"
    if lo == hi:
        return str(lo)
    return f"{lo}-{hi}"


def _join_reasons(parts: list[str]) -> str:
    return "; ".join(p for p in parts if p)


# Pass/Fail column stem → paired reason column (emitted beside each PF check).
_DAY_PF_STEMS = (
    "slots",
    "M",
    "A",
    "E",
    "pace_ok",
    "start_ok",
    "end_ok",
    "load_ok",
    "travel_ok",
    "ok",
)


@dataclass
class DayCheckResult:
    """Structured Pass/Fail for one day — matches planner rules for that day's pace."""

    day_index: int
    pace: str
    present: bool = True
    # Block presence
    m_present: bool = False
    a_present: bool = False
    e_present: bool = False
    slots_ok: bool = False
    m_reason: str = ""
    a_reason: str = ""
    e_reason: str = ""
    slots_reason: str = ""
    # Counts vs soft ranges for this pace
    count_m: int = 0
    count_a: int = 0
    count_e: int = 0
    counts: str = ""
    pace_ok: bool = False
    pace_ok_reason: str = ""
    # Clocks / load / travel (window = day_start → 21:00 for this pace)
    start_ok: bool = False
    end_ok: bool = False
    load_ok: bool = False
    travel_ok: bool = False
    start_reason: str = ""
    end_reason: str = ""
    load_reason: str = ""
    travel_reason: str = ""
    load_min: int = 0
    window_min: int = 0
    day_start_min: int = 0
    first_arrive: str = ""
    last_end: str = ""
    day_ok: bool = False
    day_ok_reason: str = ""
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_columns(self) -> dict[str, Any]:
        p = f"D{self.day_index}"
        if not self.present:
            keys: list[str] = [
                f"{p}_pace",
                f"{p}_counts",
            ]
            for stem in _DAY_PF_STEMS:
                keys.append(f"{p}_{stem}")
                keys.append(f"{p}_{stem}_reason")
            return {k: "" for k in keys}
        # Pass/Fail + reason side-by-side; pace/counts are informational only.
        return {
            f"{p}_slots": _pf(self.slots_ok),
            f"{p}_slots_reason": self.slots_reason,
            f"{p}_M": _pf(self.m_present),
            f"{p}_M_reason": self.m_reason,
            f"{p}_A": _pf(self.a_present),
            f"{p}_A_reason": self.a_reason,
            f"{p}_E": _pf(self.e_present),
            f"{p}_E_reason": self.e_reason,
            f"{p}_pace": pace_label(self.pace),
            f"{p}_counts": self.counts,
            f"{p}_pace_ok": _pf(self.pace_ok),
            f"{p}_pace_ok_reason": self.pace_ok_reason,
            f"{p}_start_ok": _pf(self.start_ok),
            f"{p}_start_ok_reason": self.start_reason,
            f"{p}_end_ok": _pf(self.end_ok),
            f"{p}_end_ok_reason": self.end_reason,
            f"{p}_load_ok": _pf(self.load_ok),
            f"{p}_load_ok_reason": self.load_reason,
            f"{p}_travel_ok": _pf(self.travel_ok),
            f"{p}_travel_ok_reason": self.travel_reason,
            f"{p}_ok": _pf(self.day_ok),
            f"{p}_ok_reason": self.day_ok_reason,
        }


@dataclass
class ItineraryCheckResult:
    ok: bool
    day_results: list[DayCheckResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def evaluate_day(day: dict, pace: str) -> DayCheckResult:
    day_i = int(day.get("day_index") or 0) or 0
    label = f"day{day_i}"
    start_min = DAY_START_MIN.get(pace, DAY_START_MIN["moderate"])
    window_min = DAY_END_MIN - start_min
    ranges = SOFT_RANGES.get(pace, SOFT_RANGES["moderate"])
    pace_ui = pace_label(pace)

    out = DayCheckResult(
        day_index=day_i,
        pace=pace,
        day_start_min=start_min,
        window_min=window_min,
    )
    failures: list[str] = []
    notes: list[str] = []
    pace_fail_bits: list[str] = []
    travel_fail_bits: list[str] = []

    # --- Block presence + soft stop ranges ---
    block_pace_ok = True
    counts: dict[str, int] = {}
    for bname in BLOCKS:
        block = day.get(bname) if isinstance(day.get(bname), dict) else None
        present = block_present(block)
        n = len((block or {}).get("stops") or []) if present else 0
        counts[bname] = n
        lo, hi = ranges[bname]
        target = _range_desc(lo, hi)

        if bname == "morning":
            out.m_present = present
        elif bname == "afternoon":
            out.a_present = present
        else:
            out.e_present = present

        if not present:
            block_pace_ok = False
            msg = (
                f"{_block_label(bname)} is missing "
                f"(need at least one stop, or a relax/free-time note)"
            )
            failures.append(f"{label}: {msg}")
            pace_fail_bits.append(msg)
            if bname == "morning":
                out.m_reason = msg
            elif bname == "afternoon":
                out.a_reason = msg
            else:
                out.e_reason = msg
            continue

        # Presence reason (PASS path)
        relax_only = n == 0 and is_relax_note((block or {}).get("notes"))
        if relax_only:
            present_reason = (
                f"{_block_label(bname)} is present as free/relax time "
                "(no POI stops)"
            )
        else:
            present_reason = (
                f"{_block_label(bname)} is present with {n} stop"
                f"{'s' if n != 1 else ''}"
            )
        if bname == "morning":
            out.m_reason = present_reason
        elif bname == "afternoon":
            out.a_reason = present_reason
        else:
            out.e_reason = present_reason

        if relax_only:
            if lo > 0:
                block_pace_ok = False
                msg = (
                    f"{_block_label(bname)} is only free/relax time, but a "
                    f"{pace_ui} day needs {target} stop(s) there"
                )
                failures.append(f"{label}: {msg}")
                pace_fail_bits.append(msg)
            else:
                notes.append(f"{label} {bname}: relax filler (0 stops) ok")
            continue

        if n < lo:
            block_pace_ok = False
            msg = (
                f"{_block_label(bname)} has {n} stop{'s' if n != 1 else ''}, "
                f"but a {pace_ui} day needs {target} "
                f"(too few stops)"
            )
            failures.append(f"{label}: {msg}")
            pace_fail_bits.append(msg)
        elif hi is not None and n > hi:
            block_pace_ok = False
            msg = (
                f"{_block_label(bname)} has {n} stops, "
                f"but a {pace_ui} day allows at most {hi} "
                f"(too many stops)"
            )
            failures.append(f"{label}: {msg}")
            pace_fail_bits.append(msg)
        else:
            notes.append(f"{label} {bname}: {n} stops ok (need {target})")

        if bname == "evening" and n > 0:
            for s in (block or {}).get("stops") or []:
                if not is_evening_ok_stop(s):
                    block_pace_ok = False
                    msg = (
                        f"Evening stop '{s.get('name')}' is category "
                        f"'{s.get('category')}', but evenings may only be "
                        f"food/park/market (or free/relax time)"
                    )
                    failures.append(f"{label}: {msg}")
                    pace_fail_bits.append(msg)

        if bname == "afternoon" and pace == "packed" and n > 0:
            for s in (block or {}).get("stops") or []:
                dep = parse_clock_min(s.get("depart_time"))
                arr = parse_clock_min(s.get("arrive_time"))
                if dep is not None and arr is not None and dep < arr:
                    block_pace_ok = False
                    msg = (
                        f"Afternoon stop '{s.get('name')}' runs past midnight; "
                        f"on a packed day afternoon must finish by 6:00 PM"
                    )
                    failures.append(f"{label}: {msg}")
                    pace_fail_bits.append(msg)
                elif dep is not None and dep > PACKED_AFTERNOON_END_MIN:
                    block_pace_ok = False
                    msg = (
                        f"Afternoon stop '{s.get('name')}' departs at "
                        f"{fmt_clock_ampm(dep)}, after the packed-day "
                        f"afternoon cutoff of 6:00 PM"
                    )
                    failures.append(f"{label}: {msg}")
                    pace_fail_bits.append(msg)

    out.count_m = counts.get("morning", 0)
    out.count_a = counts.get("afternoon", 0)
    out.count_e = counts.get("evening", 0)
    out.counts = fmt_block_counts(out.count_m, out.count_a, out.count_e)
    out.slots_ok = out.m_present and out.a_present and out.e_present
    if out.slots_ok:
        out.slots_reason = (
            f"Morning, afternoon, and evening are all present "
            f"(stop counts {out.counts} on a {pace_ui} day)"
        )
    else:
        missing = [
            _block_label(b)
            for b, ok in (
                ("morning", out.m_present),
                ("afternoon", out.a_present),
                ("evening", out.e_present),
            )
            if not ok
        ]
        out.slots_reason = (
            f"Missing time-of-day slot(s): {', '.join(missing)}"
        )

    out.pace_ok = block_pace_ok and out.slots_ok
    if out.pace_ok:
        out.pace_ok_reason = (
            f"Stop counts {out.counts} match a {pace_ui} day "
            f"(morning {_range_desc(*ranges['morning'])}, "
            f"afternoon {_range_desc(*ranges['afternoon'])}, "
            f"evening {_range_desc(*ranges['evening'])} "
            f"food/park/market or free time)"
        )
    else:
        out.pace_ok_reason = _join_reasons(pace_fail_bits) or (
            out.slots_reason
            if not out.slots_ok
            else "Stop counts or evening categories do not match this day's pace"
        )

    stops = iter_day_stops(day)

    # --- Travel: each leg <= 90 min ---
    travel_ok = True
    max_leg = 0
    for s in stops:
        name = s.get("name") or "?"
        leg = s.get("travel_to_next_min")
        if leg is None or leg == "":
            continue
        try:
            leg_i = int(leg)
        except (TypeError, ValueError):
            travel_ok = False
            msg = f"Travel time after '{name}' is not a valid number"
            failures.append(f"{label}: {msg}")
            travel_fail_bits.append(msg)
            continue
        max_leg = max(max_leg, leg_i)
        if leg_i < 0:
            travel_ok = False
            msg = f"Travel time after '{name}' is negative ({leg_i} min)"
            failures.append(f"{label}: {msg}")
            travel_fail_bits.append(msg)
        elif leg_i > MAX_LEG_TRAVEL_MIN:
            travel_ok = False
            msg = (
                f"Travel after '{name}' is {leg_i} min, "
                f"over the {MAX_LEG_TRAVEL_MIN}-min limit per leg"
            )
            failures.append(f"{label}: {msg}")
            travel_fail_bits.append(msg)
    out.travel_ok = travel_ok
    if travel_ok:
        if not stops:
            out.travel_reason = "No travel legs to check"
        else:
            out.travel_reason = (
                f"Every trip between stops is at most {MAX_LEG_TRAVEL_MIN} min "
                f"(longest leg: {max_leg} min)"
            )
    else:
        out.travel_reason = _join_reasons(travel_fail_bits)

    # --- Load vs pace window (start -> 21:00), not a flat 9h ---
    load = 0
    for s in stops:
        load += max(0, int(s.get("duration_min") or 0))
        leg = s.get("travel_to_next_min")
        if leg is not None and leg != "":
            try:
                load += max(0, int(leg))
            except (TypeError, ValueError):
                pass
    out.load_min = load
    start_ampm = fmt_clock_ampm(start_min)
    end_ampm = fmt_clock_ampm(DAY_END_MIN)
    window_human = (
        f"{fmt_duration(window_min)} available "
        f"({start_ampm} to {end_ampm} for a {pace_ui} day)"
    )
    if load > window_min:
        out.load_ok = False
        out.load_reason = (
            f"Total visit + travel time is {fmt_duration(load)}, "
            f"which is more than the {window_human}"
        )
        failures.append(f"{label}: {out.load_reason}")
    else:
        out.load_ok = True
        out.load_reason = (
            f"Total visit + travel time is {fmt_duration(load)}, "
            f"which fits in the {window_human}"
        )
        notes.append(f"{label}: {out.load_reason}")

    arrives = [parse_clock_min(s.get("arrive_time")) for s in stops]
    departs = [parse_clock_min(s.get("depart_time")) for s in stops]
    arrives = [a for a in arrives if a is not None]
    departs = [d for d in departs if d is not None]

    # Start: first arrive >= pace day start
    if not stops:
        out.start_ok = out.slots_ok
        out.start_reason = (
            f"No stamped arrival times to check "
            f"(expected start for {pace_ui}: {start_ampm})"
        )
        notes.append(f"{label}: no stamped stops for start check")
    elif arrives:
        first = min(arrives)
        out.first_arrive = fmt_clock(first)
        if first < start_min:
            out.start_ok = False
            out.start_reason = (
                f"First stop starts at {fmt_clock_ampm(first)}, "
                f"earlier than the {pace_ui} day start of {start_ampm}"
            )
            failures.append(f"{label}: {out.start_reason}")
        else:
            out.start_ok = True
            out.start_reason = (
                f"First stop starts at {fmt_clock_ampm(first)}, "
                f"on or after the {pace_ui} day start of {start_ampm}"
            )
            notes.append(f"{label}: {out.start_reason}")
    else:
        out.start_ok = True
        out.start_reason = (
            f"No arrival timestamps on stops; treated as OK "
            f"(expected start for {pace_ui}: {start_ampm})"
        )
        notes.append(f"{label}: {out.start_reason}")

    last_end: int | None = None
    if departs:
        last_end = max(departs)
    elif arrives:
        last_stop = None
        last_a = -1
        for s in stops:
            a = parse_clock_min(s.get("arrive_time"))
            if a is not None and a >= last_a:
                last_a = a
                last_stop = s
        if last_stop is not None:
            last_end = last_a + max(0, int(last_stop.get("duration_min") or 0))

    past_midnight = False
    midnight_bits: list[str] = []
    for s in stops:
        a = parse_clock_min(s.get("arrive_time"))
        d = parse_clock_min(s.get("depart_time"))
        if a is not None and d is not None and d < a:
            past_midnight = True
            msg = (
                f"Stop '{s.get('name')}' looks past midnight "
                f"({s.get('arrive_time')} to {s.get('depart_time')})"
            )
            midnight_bits.append(msg)
            failures.append(f"{label}: {msg}")

    if last_end is not None:
        out.last_end = fmt_clock(last_end)
        if last_end > DAY_END_MIN or past_midnight:
            out.end_ok = False
            bits = list(midnight_bits)
            if last_end > DAY_END_MIN:
                bits.append(
                    f"Last activity ends at {fmt_clock_ampm(last_end)}, "
                    f"after the hard day end of {end_ampm}"
                )
                failures.append(f"{label}: {bits[-1]}")
            out.end_reason = _join_reasons(bits)
        else:
            out.end_ok = True
            out.end_reason = (
                f"Last activity ends at {fmt_clock_ampm(last_end)}, "
                f"on or before {end_ampm}"
            )
            notes.append(f"{label}: {out.end_reason}")
    elif stops:
        cursor = start_min
        for i, s in enumerate(stops):
            cursor += max(0, int(s.get("duration_min") or 0))
            if i < len(stops) - 1:
                leg = s.get("travel_to_next_min")
                try:
                    cursor += max(0, int(leg or 0))
                except (TypeError, ValueError):
                    pass
        out.last_end = fmt_clock(cursor)
        if cursor > DAY_END_MIN or past_midnight:
            out.end_ok = False
            bits = list(midnight_bits)
            if cursor > DAY_END_MIN:
                bits.append(
                    f"Estimated day end is {fmt_clock_ampm(cursor)} "
                    f"(no clock stamps), after hard end {end_ampm}"
                )
                failures.append(f"{label}: {bits[-1]}")
            out.end_reason = _join_reasons(bits)
        else:
            out.end_ok = True
            out.end_reason = (
                f"Estimated day end is {fmt_clock_ampm(cursor)} "
                f"(no clock stamps), on or before {end_ampm}"
            )
            notes.append(f"{label}: {out.end_reason}")
    else:
        out.end_ok = out.slots_ok
        out.end_reason = "No stops to check for day-end time"

    out.failures = failures
    out.notes = notes
    out.day_ok = (
        out.slots_ok
        and out.pace_ok
        and out.start_ok
        and out.end_ok
        and out.load_ok
        and out.travel_ok
    )
    if out.day_ok:
        out.day_ok_reason = (
            f"Day {day_i} passes all checks for a {pace_ui} day "
            f"(stops {out.counts}; busy time {fmt_duration(load)} "
            f"within {fmt_duration(window_min)} window)"
        )
    else:
        # Expand each failed check with its human reason (not "failed: pace_ok").
        fail_parts: list[str] = []
        if not out.slots_ok:
            fail_parts.append(f"Slots: {out.slots_reason}")
        if not out.pace_ok:
            fail_parts.append(f"Pace/stop counts: {out.pace_ok_reason}")
        if not out.start_ok:
            fail_parts.append(f"Start time: {out.start_reason}")
        if not out.end_ok:
            fail_parts.append(f"End time: {out.end_reason}")
        if not out.load_ok:
            fail_parts.append(f"Day length: {out.load_reason}")
        if not out.travel_ok:
            fail_parts.append(f"Travel between stops: {out.travel_reason}")
        out.day_ok_reason = _join_reasons(fail_parts) or "One or more day checks failed"
    return out


def resolve_day_pace(
    day: dict,
    trip_pace: str,
    day_paces_override: dict[str, str] | None = None,
) -> str:
    day_i = str(day.get("day_index", ""))
    if day_paces_override and day_i in day_paces_override:
        return normalize_pace(day_paces_override[day_i])
    if day.get("pace"):
        return normalize_pace(day.get("pace"))
    return trip_pace


def evaluate_itinerary(
    itin: dict, day_paces_override: dict[str, str] | None = None
) -> ItineraryCheckResult:
    trip = itin.get("trip") or {}
    trip_pace = normalize_pace(trip.get("pace"))
    days = itin.get("days") or []
    if not days:
        return ItineraryCheckResult(ok=False, failures=["no days in itinerary"])

    merged = ItineraryCheckResult(ok=True)
    merged.notes.append(f"trip_pace={trip_pace}")
    for day in days:
        if not isinstance(day, dict):
            merged.ok = False
            merged.failures.append("invalid day object")
            continue
        day_pace = resolve_day_pace(day, trip_pace, day_paces_override)
        day_i = day.get("day_index", "?")
        merged.notes.append(f"day{day_i}_pace={day_pace}")
        day_res = evaluate_day(day, day_pace)
        merged.day_results.append(day_res)
        if not day_res.day_ok:
            merged.ok = False
        merged.failures.extend(day_res.failures)
        merged.notes.extend(day_res.notes)
    return merged


def empty_day_columns(day_index: int) -> dict[str, Any]:
    return DayCheckResult(day_index=day_index, pace="", present=False).to_columns()


def itinerary_row_columns(res: ItineraryCheckResult) -> dict[str, Any]:
    """Wide D1–D4 columns; missing days blank."""
    by_idx = {d.day_index: d for d in res.day_results if d.day_index}
    cols: dict[str, Any] = {}
    for i in range(1, MAX_DAYS + 1):
        if i in by_idx:
            cols.update(by_idx[i].to_columns())
        else:
            cols.update(empty_day_columns(i))
    return cols


def parse_day_paces_cell(raw: Any) -> dict[str, str] | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "—", "-"}:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    out: dict[str, str] = {}
    for k, v in obj.items():
        if v is None:
            continue
        out[str(k)] = normalize_pace(v)
    return out or None


def parse_itinerary_cell(raw: Any) -> dict | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "—", "-"}:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if "days" not in obj:
        return None
    return obj


def day_paces_label(
    itin: dict,
    day_paces: dict[str, str] | None,
) -> str:
    trip_pace = normalize_pace((itin.get("trip") or {}).get("pace"))
    bits: list[str] = []
    for day in itin.get("days") or []:
        if not isinstance(day, dict):
            continue
        idx = day.get("day_index")
        if idx is None:
            continue
        p = resolve_day_pace(day, trip_pace, day_paces)
        bits.append(f"D{idx}:{pace_label(p)}")
    return ",".join(bits) if bits else pace_label(trip_pace)


print("Rules loaded.")
print(
    "Day starts:",
    {k: fmt_clock(v) for k, v in DAY_START_MIN.items() if k != "balanced"},
)
print(
    "Day end:",
    fmt_clock(DAY_END_MIN),
    "| packed PM end:",
    fmt_clock(PACKED_AFTERNOON_END_MIN),
    "| max leg travel:",
    MAX_LEG_TRAVEL_MIN,
    "min",
)
print(
    "Windows:",
    {
        k: f"{fmt_clock(DAY_START_MIN[k])}–21:00 ({DAY_END_MIN - DAY_START_MIN[k]}m)"
        for k in ("relaxed", "moderate", "packed")
    },
)
print("Soft ranges:", SOFT_RANGES)

# %%
# Cell 2 — load Eval CSV (upload or path)
#
# Set LOAD_MODE:
#   "upload" — pick rag_eval.csv with a file chooser (default; Colab or Jupyter)
#   "path"   — read from CSV_PATH (or type a path when prompted)

LOAD_MODE = "upload"  # "upload" | "path"
CSV_PATH = ""  # e.g. r"C:\path\to\rag_eval.csv" when LOAD_MODE == "path"
# Persist FileUpload widget across re-runs (local Jupyter).
if "_CSV_UPLOADER" not in globals():
    _CSV_UPLOADER = None


def _load_csv_from_upload() -> pd.DataFrame:
    """Open a file picker and return the uploaded Eval CSV as a DataFrame."""
    # Google Colab
    try:
        from google.colab import files  # type: ignore

        print("Click Choose Files and select rag_eval.csv from the Eval tab…")
        uploaded = files.upload()
        if not uploaded:
            raise SystemExit("No file uploaded.")
        name = next(iter(uploaded))
        print(f"Uploaded: {name}")
        return pd.read_csv(name)
    except ImportError:
        pass

    # Local Jupyter / VS Code — ipywidgets FileUpload
    try:
        import io

        import ipywidgets as widgets
        from IPython.display import display

        # Keep widget across re-runs so the second Run picks up the chosen file.
        global _CSV_UPLOADER  # noqa: PLW0603
        if _CSV_UPLOADER is None:
            _CSV_UPLOADER = widgets.FileUpload(
                accept=".csv",
                multiple=False,
                description="Upload CSV",
            )
        display(_CSV_UPLOADER)
        value = _CSV_UPLOADER.value
        if not value:
            raise SystemExit(
                "No file yet — click Upload CSV, choose rag_eval.csv, "
                "then re-run this cell."
            )

        # ipywidgets v7: dict keyed by name; v8: tuple of UploadedFile
        if isinstance(value, dict):
            name, meta = next(iter(value.items()))
            content = meta["content"]
        else:
            item = value[0]
            name = getattr(item, "name", None) or item["name"]
            content = getattr(item, "content", None) or item["content"]
        print(f"Uploaded: {name}")
        return pd.read_csv(io.BytesIO(content))
    except ImportError:
        raise SystemExit(
            "Upload requires Google Colab or ipywidgets. "
            "Install with: pip install ipywidgets\n"
            'Or set LOAD_MODE = "path" and CSV_PATH to your file.'
        ) from None


def _load_csv_from_path() -> pd.DataFrame:
    path = (CSV_PATH or "").strip().strip('"')
    if not path:
        path = input("Path to rag_eval.csv: ").strip().strip('"')
    if not path:
        raise SystemExit("No CSV path provided.")
    print(f"Reading: {path}")
    return pd.read_csv(path)


mode = (LOAD_MODE or "upload").strip().lower()
if mode == "upload":
    df = _load_csv_from_upload()
elif mode == "path":
    df = _load_csv_from_path()
else:
    raise SystemExit(f'Unknown LOAD_MODE={LOAD_MODE!r}. Use "upload" or "path".')

print(f"Loaded {len(df)} rows, columns={list(df.columns)}")

if "itinerary_json" not in df.columns:
    print(
        "\n⚠️  CSV has no itinerary_json column.\n"
        "This usually means the file was downloaded before that column shipped,\n"
        "or from a cached old Eval tab build.\n\n"
        "Fix on the live site (https://itinerary-planner-web-seven.vercel.app):\n"
        "  1. Hard refresh (Ctrl+Shift+R)\n"
        "  2. Open Eval — confirm you see an itinerary_json column (may be empty)\n"
        "  3. Go back to the planner and create/update a plan (so a plan turn logs JSON)\n"
        "  4. Eval → Download again, then re-upload here\n"
    )
    df["itinerary_json"] = ""
    print(
        "Added empty itinerary_json so you can inspect the table — "
        "feasibility needs real plan JSON.\n"
    )

plan_mask = df["itinerary_json"].astype(str).str.strip().str.len() > 0
plan_mask &= ~df["itinerary_json"].astype(str).str.strip().str.lower().isin(
    {"nan", "none", "—", "-"}
)
plans_df = df.loc[plan_mask].copy()
print(f"Plan rows (non-empty itinerary_json): {len(plans_df)} / {len(df)}")
if plans_df.empty:
    raise SystemExit(
        "No plan rows with itinerary_json yet.\n"
        "Hard-refresh the Eval UI, generate or edit a plan, re-download rag_eval.csv, "
        "and upload again. Tip/RAG-only turns leave itinerary_json blank (expected)."
    )

# %%
# Cell 3 — run feasibility checks (wide per-day Pass/Fail columns)
rows_out: list[dict[str, Any]] = []

# Identity + overall (+ reason), then D1…D4 check columns for the summary table.
SUMMARY_PREFIX = [
    "Session_Id",
    "num_days",
    "day_paces",
    "overall",
    "overall_reason",
]
# Informational stems first; each Pass/Fail stem is followed by its _reason.
DAY_COL_PAIRS: list[tuple[str, bool]] = [
    ("slots", True),
    ("M", True),
    ("A", True),
    ("E", True),
    ("pace", False),
    ("counts", False),
    ("pace_ok", True),
    ("start_ok", True),
    ("end_ok", True),
    ("load_ok", True),
    ("travel_ok", True),
    ("ok", True),
]


def _summary_cols_for(num_days: int) -> list[str]:
    cols = list(SUMMARY_PREFIX)
    n = max(1, min(int(num_days or MAX_DAYS), MAX_DAYS))
    for i in range(1, n + 1):
        for suf, has_reason in DAY_COL_PAIRS:
            cols.append(f"D{i}_{suf}")
            if has_reason:
                cols.append(f"D{i}_{suf}_reason")
    return cols


def _overall_reason(res: ItineraryCheckResult) -> str:
    if res.ok:
        days = ", ".join(
            f"Day {d.day_index} ({pace_label(d.pace)}, stops {d.counts})"
            for d in res.day_results
        )
        return (
            f"Every day passed all feasibility checks"
            + (f": {days}" if days else "")
        )
    failed_days = [
        f"Day {d.day_index}: {d.day_ok_reason}"
        for d in res.day_results
        if not d.day_ok
    ]
    if failed_days:
        return " | ".join(failed_days)
    return _join_reasons(res.failures) or "Itinerary failed feasibility checks"



for idx, row in plans_df.iterrows():
    itin = parse_itinerary_cell(row.get("itinerary_json"))
    session = row.get("Session_Id", "")
    question = str(row.get("question", ""))[:120]
    ts = row.get("Timestamp_R") or row.get("Timestamp_UQ") or ""

    base: dict[str, Any] = {
        "row_index": idx,
        "Session_Id": session,
        "Timestamp_R": ts,
        "question": question,
        "day_paces_json": row.get("day_paces_json") or "",
    }
    # Pre-fill blank D1–D4 so schema is stable.
    for i in range(1, MAX_DAYS + 1):
        base.update(empty_day_columns(i))

    if itin is None:
        base.update(
            {
                "num_days": "",
                "day_paces": "",
                "overall": "FAIL",
                "overall_reason": "could not parse itinerary_json",
                "failures_detail": "could not parse itinerary_json",
                "notes": "",
            }
        )
        rows_out.append(base)
        continue

    trip = itin.get("trip") or {}
    day_paces = parse_day_paces_cell(row.get("day_paces_json"))
    res = evaluate_itinerary(itin, day_paces_override=day_paces)
    num_days = trip.get("num_days") or len(itin.get("days") or [])
    base.update(itinerary_row_columns(res))
    base.update(
        {
            "num_days": num_days,
            "day_paces": day_paces_label(itin, day_paces),
            "overall": "PASS" if res.ok else "FAIL",
            "overall_reason": _overall_reason(res),
            "failures_detail": " | ".join(res.failures),
            "notes": " | ".join(res.notes),
        }
    )
    rows_out.append(base)

results = pd.DataFrame(rows_out)
n_pass = int((results["overall"] == "PASS").sum())
n_fail = int((results["overall"] == "FAIL").sum())
print("\n=== Feasibility summary ===")
print(f"Plans checked: {len(results)}  |  PASS: {n_pass}  |  FAIL: {n_fail}")
print()

# Print a readable slice: identity + columns for max days in this batch.
max_days_batch = int(pd.to_numeric(results["num_days"], errors="coerce").max() or 2)
display_cols = _summary_cols_for(max_days_batch)
display_cols = [c for c in display_cols if c in results.columns]
print(results[display_cols].to_string(index=False))

fails = results[results["overall"] == "FAIL"]
if len(fails):
    print("\n--- First FAIL detail ---")
    r0 = fails.iloc[0]
    print("Session:", r0["Session_Id"])
    print("Question:", r0["question"])
    print("day_paces:", r0["day_paces"])
    print("overall_reason:", r0["overall_reason"])
    print("Failures:", r0["failures_detail"])
    for i in range(1, MAX_DAYS + 1):
        ok_col = f"D{i}_ok"
        if ok_col not in r0 or r0[ok_col] == "":
            continue
        print(
            f"  D{i}: ok={r0[ok_col]} ({r0.get(f'D{i}_ok_reason', '')}) "
            f"pace={r0[f'D{i}_pace']} counts={r0[f'D{i}_counts']} "
            f"pace_ok={r0[f'D{i}_pace_ok']} ({r0.get(f'D{i}_pace_ok_reason', '')})"
        )

# %%
# Cell 4 — download results CSV
out_name = "feasibility_results.csv"
results.to_csv(out_name, index=False, encoding="utf-8-sig")
print(f"Wrote {out_name} (utf-8-sig for Excel)")

try:
    from google.colab import files  # type: ignore

    files.download(out_name)
except ImportError:
    print(f"Saved locally: {Path(out_name).resolve()}")

# %%
# Cell 5 (optional) — smoke-test against a golden fixture without the Eval CSV
# Uncomment and set FIXTURE_PATH to a repo fixture JSON.

# FIXTURE_PATH = r"..\fixtures\jaipur_2day_culture.json"
# with open(FIXTURE_PATH, encoding="utf-8") as f:
#     sample = json.load(f)
# smoke = evaluate_itinerary(sample)
# print("Fixture overall:", "PASS" if smoke.ok else "FAIL")
# for d in smoke.day_results:
#     print(d.day_index, d.to_columns())
# print("Failures:", smoke.failures)
