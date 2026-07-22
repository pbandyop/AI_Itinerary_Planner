# %% [markdown]
# # Edit Correctness eval (Colab / Jupyter)
#
# **Rule-based** (no LLM judge). Capstone Phase 7b checks:
#
# | Check | Pass means |
# |--------|------------|
# | **Intended section changed** | Named day (and block, if spoken) actually changed |
# | **No unintended changes** | Other days (and other blocks on the target day) stay identical |
# | **POIs map to dataset** | Every stop has `osm_type` ∈ {node,way,relation} and `osm_id` > 0 |
#
# **Inputs**
# - **Live Eval CSV** (`rag_eval.csv`): pairs each **edit** turn’s `itinerary_json`
#   with the previous plan/edit snapshot in the same `Session_Id` (before → after).
# - **Optional fixtures**: JSON under `evals/fixtures/edits/` that already include
#   `before` + `after` (or `expect_changed_days` + `before`/`after`).
#
# Tip / RAG-only rows (empty `itinerary_json`) are ignored.
#
# Paste cells into Colab top-to-bottom, or run with `# %%` cell support.

# %%
# Cell 1 — imports + rules
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

BLOCKS = ("morning", "afternoon", "evening")
OSM_TYPES = frozenset({"node", "way", "relation"})
MAX_DAYS = 4
PF = {"PASS": "PASS", "FAIL": "FAIL", "NA": ""}

# Utterances that look like voice itinerary edits (not tip/plan asks).
_EDIT_HINT_RE = re.compile(
    r"\b("
    r"make day|day\s+\d|day\s+(one|two|three|four|1st|2nd|3rd|4th)|"
    r"relax(?:ed|ing)?|pack(?:ed)?|balance|trim|remove|swap|add |"
    r"less (?:food|stops|travel)|more (?:relaxed|packed|stops)|"
    r"indoor|what if it rains|rain(?:y)? day|fewer stops|"
    r"skip |drop |cut "
    r")\b",
    re.I,
)

_DAY_WORD = {
    "one": 1,
    "1st": 1,
    "first": 1,
    "two": 2,
    "2nd": 2,
    "second": 2,
    "three": 3,
    "3rd": 3,
    "third": 3,
    "four": 4,
    "4th": 4,
    "fourth": 4,
}


def _pf(ok: bool | None) -> str:
    if ok is None:
        return PF["NA"]
    return PF["PASS"] if ok else PF["FAIL"]


def parse_json_cell(val: Any) -> Any | None:
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, (dict, list)):
        return val
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "—", "-", "[]", "{}"}:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def looks_like_edit_utterance(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    # Plan kickoffs are not edits
    if re.search(
        r"\b(plan (?:a |my )?trip|i (?:want|need) (?:a )?(?:2|3|4)[\s-]?day|"
        r"book (?:a )?trip|create (?:an )?itinerary)\b",
        q,
        re.I,
    ):
        return False
    return bool(_EDIT_HINT_RE.search(q))


def parse_intended_days(question: str) -> set[int]:
    """Day indexes mentioned in the edit utterance (1-based)."""
    q = (question or "").lower()
    found: set[int] = set()
    for m in re.finditer(r"\bday\s*(\d+)\b", q):
        d = int(m.group(1))
        if 1 <= d <= MAX_DAYS:
            found.add(d)
    for word, num in _DAY_WORD.items():
        if re.search(rf"\bday\s+{re.escape(word)}\b", q) or re.search(
            rf"\b{re.escape(word)}\s+day\b", q
        ):
            found.add(num)
    return found


def parse_intended_block(question: str) -> str | None:
    q = (question or "").lower()
    # Prefer explicit block words; rain edits are whole-day.
    if re.search(r"\bwhat if it rains\b|\brain(?:y)?\b", q):
        return None
    for b in BLOCKS:
        if re.search(rf"\b{b}\b", q):
            return b
    return None


def iter_days(itin: dict) -> list[dict]:
    days = itin.get("days") if isinstance(itin, dict) else None
    if not isinstance(days, list):
        return []
    return [d for d in days if isinstance(d, dict)]


def day_by_index(itin: dict, day_index: int) -> dict | None:
    for d in iter_days(itin):
        if int(d.get("day_index") or 0) == day_index:
            return d
    return None


def block_stops(day: dict, block: str) -> list[dict]:
    raw = day.get(block) if isinstance(day, dict) else None
    if isinstance(raw, dict):
        stops = raw.get("stops") or []
    elif isinstance(raw, list):
        stops = raw
    else:
        stops = []
    return [s for s in stops if isinstance(s, dict)]


def stop_key(stop: dict) -> str:
    name = str(stop.get("name") or "").strip()
    cat = str(stop.get("category") or "").strip()
    osm = stop.get("osm_id")
    return f"{name}|{cat}|{osm}"


def block_fingerprint(day: dict, block: str) -> tuple[str, ...]:
    return tuple(stop_key(s) for s in block_stops(day, block))


def day_fingerprint(day: dict) -> tuple[str, ...]:
    parts: list[str] = []
    pace = str(day.get("pace") or "").strip().lower()
    if pace:
        parts.append(f"pace:{pace}")
    for b in BLOCKS:
        for k in block_fingerprint(day, b):
            parts.append(f"{b}:{k}")
    return tuple(parts)


def all_stops(itin: dict) -> list[dict]:
    out: list[dict] = []
    for d in iter_days(itin):
        for b in BLOCKS:
            out.extend(block_stops(d, b))
    return out


def check_poi_grounding(itin: dict) -> tuple[bool, str, list[str]]:
    """Every POI must map to an OSM-style dataset record."""
    bad: list[str] = []
    stops = all_stops(itin)
    if not stops:
        return False, "No stops in itinerary to ground.", ["no_stops"]

    for s in stops:
        name = str(s.get("name") or "?").strip() or "?"
        osm_type = str(s.get("osm_type") or "").strip().lower()
        osm_id = s.get("osm_id")
        try:
            oid = int(osm_id)
        except (TypeError, ValueError):
            oid = 0
        if osm_type not in OSM_TYPES:
            bad.append(f"{name}: bad osm_type={osm_type!r}")
        if oid <= 0:
            bad.append(f"{name}: osm_id must be > 0 (got {osm_id!r})")

    if bad:
        preview = "; ".join(bad[:4])
        more = f" (+{len(bad) - 4} more)" if len(bad) > 4 else ""
        return False, f"{len(bad)} stop(s) not dataset-grounded: {preview}{more}", bad
    return True, f"All {len(stops)} stop(s) have osm_type + osm_id > 0.", []


@dataclass
class EditCheckResult:
    ok: bool
    intended_days: list[int] = field(default_factory=list)
    intended_block: str | None = None
    intended_changed_ok: bool | None = None
    intended_reason: str = ""
    unintended_ok: bool | None = None
    unintended_reason: str = ""
    block_scope_ok: bool | None = None
    block_scope_reason: str = ""
    poi_ok: bool | None = None
    poi_reason: str = ""
    days_changed: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return {
            "intended_days": ",".join(str(d) for d in self.intended_days) or "",
            "intended_block": self.intended_block or "(whole day)",
            "days_changed": ",".join(str(d) for d in self.days_changed) or "",
            "intended_changed_ok": _pf(self.intended_changed_ok),
            "intended_changed_reason": self.intended_reason,
            "unintended_ok": _pf(self.unintended_ok),
            "unintended_reason": self.unintended_reason,
            "block_scope_ok": _pf(self.block_scope_ok),
            "block_scope_reason": self.block_scope_reason,
            "poi_grounded_ok": _pf(self.poi_ok),
            "poi_grounded_reason": self.poi_reason,
            "overall": "PASS" if self.ok else "FAIL",
            "overall_reason": (
                "All edit-correctness checks passed."
                if self.ok
                else (" | ".join(self.failures) if self.failures else "Failed.")
            ),
            "failures_detail": " | ".join(self.failures),
            "notes": " | ".join(self.notes),
        }


def evaluate_edit(
    before: dict,
    after: dict,
    question: str,
    *,
    expect_changed_days: set[int] | None = None,
    expect_block: str | None = None,
) -> EditCheckResult:
    """Compare before/after itineraries for scoped edit correctness + POI grounding."""
    intended = set(expect_changed_days or ())
    if not intended:
        intended = parse_intended_days(question)
    block = expect_block if expect_block is not None else parse_intended_block(question)

    before_days = {int(d.get("day_index") or 0): d for d in iter_days(before)}
    after_days = {int(d.get("day_index") or 0): d for d in iter_days(after)}
    all_idxs = sorted(set(before_days) | set(after_days))
    all_idxs = [i for i in all_idxs if i > 0]

    changed: list[int] = []
    for idx in all_idxs:
        b = before_days.get(idx)
        a = after_days.get(idx)
        if b is None or a is None:
            changed.append(idx)
            continue
        if day_fingerprint(b) != day_fingerprint(a):
            changed.append(idx)

    res = EditCheckResult(
        ok=True,
        intended_days=sorted(intended),
        intended_block=block,
        days_changed=changed,
    )

    # --- Intended section must change ---
    if not intended:
        res.intended_changed_ok = False
        res.intended_reason = (
            "Could not parse a target day from the utterance "
            "(e.g. 'Day 2') and no expect_changed_days provided."
        )
        res.failures.append(res.intended_reason)
    else:
        missing_targets = [d for d in sorted(intended) if d not in changed]
        if missing_targets:
            res.intended_changed_ok = False
            res.intended_reason = (
                f"Expected Day(s) {missing_targets} to change; "
                f"changed={changed or 'none'}."
            )
            res.failures.append(res.intended_reason)
        else:
            res.intended_changed_ok = True
            res.intended_reason = (
                f"Target Day(s) {sorted(intended)} changed as intended."
            )

    # --- No unintended day changes ---
    unintended = [d for d in changed if d not in intended] if intended else list(changed)
    if not intended:
        res.unintended_ok = False
        res.unintended_reason = "Skipped strict unintended check (no target day)."
        # already failed intended
    elif unintended:
        res.unintended_ok = False
        res.unintended_reason = (
            f"Non-target Day(s) {unintended} also changed "
            f"(targets were {sorted(intended)})."
        )
        res.failures.append(res.unintended_reason)
    else:
        res.unintended_ok = True
        res.unintended_reason = (
            f"Only target Day(s) {sorted(intended)} changed; "
            f"other days unchanged."
        )

    # --- Block scope (if user named morning/afternoon/evening) ---
    if block and intended:
        block_leaks: list[str] = []
        target_block_changed = False
        for day_i in sorted(intended):
            bday = before_days.get(day_i)
            aday = after_days.get(day_i)
            if not bday or not aday:
                continue
            if block_fingerprint(bday, block) != block_fingerprint(aday, block):
                target_block_changed = True
            for other in BLOCKS:
                if other == block:
                    continue
                if block_fingerprint(bday, other) != block_fingerprint(aday, other):
                    block_leaks.append(f"Day {day_i} {other}")
        if block_leaks:
            res.block_scope_ok = False
            res.block_scope_reason = (
                f"Utterance targeted {block}, but also changed: "
                + ", ".join(block_leaks)
            )
            res.failures.append(res.block_scope_reason)
        else:
            res.block_scope_ok = True
            if any(d in changed for d in intended) and not target_block_changed:
                res.block_scope_reason = (
                    f"Other blocks on target day(s) unchanged; named block "
                    f"'{block}' unchanged (possible pace-only / non-stop edit)."
                )
                res.notes.append(res.block_scope_reason)
            else:
                res.block_scope_reason = (
                    f"Non-target blocks unchanged; scope held to '{block}'."
                )
    else:
        res.block_scope_ok = None
        res.block_scope_reason = (
            "No block named in utterance (whole-day edit) — block scope N/A."
        )

    # --- POI dataset grounding on AFTER itinerary ---
    poi_ok, poi_reason, _bad = check_poi_grounding(after)
    res.poi_ok = poi_ok
    res.poi_reason = poi_reason
    if not poi_ok:
        res.failures.append(poi_reason)

    res.ok = not res.failures
    return res


print("Edit-correctness helpers ready.")

# %%
# Cell 2 — load Eval CSV (upload or path)
#
# LOAD_MODE:
#   "upload" — Colab file picker (default)
#   "path"   — set CSV_PATH below

LOAD_MODE = "upload"  # "upload" | "path"
CSV_PATH = ""  # e.g. r"C:\Users\...\rag_eval.csv"


def _load_csv_from_upload() -> pd.DataFrame:
    try:
        from google.colab import files  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            'LOAD_MODE="upload" needs Google Colab. '
            'Or set LOAD_MODE="path" and CSV_PATH.'
        ) from exc
    print("Upload rag_eval.csv from the companion UI Eval tab")
    uploaded = files.upload()
    if not uploaded:
        raise SystemExit("No file uploaded.")
    path = list(uploaded.keys())[0]
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin1")


def _load_csv_from_path() -> pd.DataFrame:
    path = (CSV_PATH or "").strip().strip('"')
    if not path:
        path = input("Path to rag_eval.csv: ").strip().strip('"')
    if not path:
        raise SystemExit("No CSV path provided.")
    print(f"Reading: {path}")
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin1")


mode = (LOAD_MODE or "upload").strip().lower()
if mode == "upload":
    df = _load_csv_from_upload()
elif mode == "path":
    df = _load_csv_from_path()
else:
    raise SystemExit(f'Unknown LOAD_MODE={LOAD_MODE!r}. Use "upload" or "path".')

print(f"Loaded {len(df)} rows, columns={list(df.columns)}")

for col in ("Session_Id", "question", "itinerary_json"):
    if col not in df.columns:
        df[col] = ""

# %%
# Cell 3 — pair before/after edit turns from the live CSV

rows = df.reset_index(drop=True)
edit_cases: list[dict[str, Any]] = []

# Track last non-empty itinerary per session (plan or prior edit).
last_itin: dict[str, dict] = {}

for i, row in rows.iterrows():
    sid = str(row.get("Session_Id") or "").strip() or "_nosession"
    question = str(row.get("question") or "")
    itin = parse_json_cell(row.get("itinerary_json"))
    if not isinstance(itin, dict) or not iter_days(itin):
        continue

    is_edit = looks_like_edit_utterance(question)
    before = last_itin.get(sid)

    if is_edit and before is not None:
        edit_cases.append(
            {
                "row_index": int(i),
                "Session_Id": sid,
                "Timestamp_UQ": str(row.get("Timestamp_UQ") or ""),
                "Timestamp_R": str(row.get("Timestamp_R") or ""),
                "question": question,
                "before": before,
                "after": itin,
                "source": "live_csv",
            }
        )
    elif is_edit and before is None:
        edit_cases.append(
            {
                "row_index": int(i),
                "Session_Id": sid,
                "Timestamp_UQ": str(row.get("Timestamp_UQ") or ""),
                "Timestamp_R": str(row.get("Timestamp_R") or ""),
                "question": question,
                "before": None,
                "after": itin,
                "source": "live_csv_missing_before",
                "skip_reason": (
                    "Edit-like utterance but no prior itinerary_json in this "
                    "Session_Id (need a plan turn before the edit)."
                ),
            }
        )

    # Always refresh snapshot after a logged plan/edit turn
    last_itin[sid] = itin

print(f"Edit candidate turns: {len(edit_cases)}")
if not edit_cases:
    print(
        "\nNo edit pairs found. Tips for a useful CSV:\n"
        "  1. Plan a trip (logs itinerary_json)\n"
        "  2. Voice-edit e.g. 'Make Day 2 relaxed' (logs new itinerary_json)\n"
        "  3. Download Eval CSV and re-upload\n"
        "Optional: run Cell 5 against fixture JSON with before+after.\n"
    )

# %%
# Cell 4 — score edit correctness

out_rows: list[dict[str, Any]] = []

for case in edit_cases:
    base = {
        "Session_Id": case.get("Session_Id", ""),
        "row_index": case.get("row_index", ""),
        "Timestamp_UQ": case.get("Timestamp_UQ", ""),
        "Timestamp_R": case.get("Timestamp_R", ""),
        "question": case.get("question", ""),
        "source": case.get("source", ""),
    }
    if case.get("before") is None or case.get("skip_reason"):
        base.update(
            {
                "intended_days": ",".join(
                    str(d) for d in sorted(parse_intended_days(case.get("question", "")))
                ),
                "intended_block": parse_intended_block(case.get("question", ""))
                or "(whole day)",
                "days_changed": "",
                "intended_changed_ok": "FAIL",
                "intended_changed_reason": case.get("skip_reason")
                or "Missing before itinerary.",
                "unintended_ok": "",
                "unintended_reason": "N/A — no before snapshot.",
                "block_scope_ok": "",
                "block_scope_reason": "N/A",
                "poi_grounded_ok": "",
                "poi_grounded_reason": "N/A",
                "overall": "FAIL",
                "overall_reason": case.get("skip_reason") or "Missing before itinerary.",
                "failures_detail": case.get("skip_reason") or "Missing before itinerary.",
                "notes": "",
            }
        )
        # Still score POI grounding on after alone when present
        after = case.get("after")
        if isinstance(after, dict):
            poi_ok, poi_reason, _ = check_poi_grounding(after)
            base["poi_grounded_ok"] = _pf(poi_ok)
            base["poi_grounded_reason"] = poi_reason
            if not poi_ok:
                base["failures_detail"] = (
                    str(base["failures_detail"]) + " | " + poi_reason
                ).strip(" |")
                base["overall_reason"] = base["failures_detail"]
        out_rows.append(base)
        continue

    result = evaluate_edit(
        case["before"],
        case["after"],
        case.get("question") or "",
    )
    base.update(result.to_row())
    out_rows.append(base)

results = pd.DataFrame(out_rows)
if results.empty:
    print("No scored rows.")
else:
    n_pass = int((results["overall"] == "PASS").sum())
    n_fail = int((results["overall"] == "FAIL").sum())
    print("\n=== Edit Correctness summary ===")
    print(f"Edit turns: {len(results)}  |  PASS: {n_pass}  |  FAIL: {n_fail}")
    show = [
        c
        for c in [
            "Session_Id",
            "question",
            "intended_days",
            "intended_block",
            "days_changed",
            "intended_changed_ok",
            "unintended_ok",
            "block_scope_ok",
            "poi_grounded_ok",
            "overall",
            "overall_reason",
        ]
        if c in results.columns
    ]
    print()
    print(results[show].to_string(index=False))

    fails = results[results["overall"] == "FAIL"]
    if len(fails):
        print("\n--- First FAIL detail ---")
        r0 = fails.iloc[0]
        for k in (
            "question",
            "intended_changed_reason",
            "unintended_reason",
            "block_scope_reason",
            "poi_grounded_reason",
            "failures_detail",
        ):
            print(f"{k}: {r0.get(k, '')}")

# %%
# Cell 5 (optional) — score fixture JSON with before + after
#
# Set FIXTURE_PATH to a file that contains:
#   { "utterance": "...", "expect_changed_days": [2], "before": {...}, "after": {...} }
# Or a folder of such files.
#
# Note: CLI `python -m evals --suite edit` applies patches via the agent.
# This Colab path compares snapshots only (no apply_edit_patches).

FIXTURE_PATH = ""  # e.g. r"..\fixtures\edits\my_before_after.json"


def _score_fixture_obj(data: dict, name: str) -> dict[str, Any]:
    before = data.get("before")
    after = data.get("after")
    utterance = str(data.get("utterance") or data.get("question") or name)
    expect = data.get("expect_changed_days")
    expect_set = {int(x) for x in expect} if isinstance(expect, list) else None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {
            "Session_Id": name,
            "question": utterance,
            "source": "fixture",
            "overall": "FAIL",
            "overall_reason": "Fixture needs both before and after itinerary objects.",
            "failures_detail": "missing before/after",
            "intended_changed_ok": "FAIL",
            "intended_changed_reason": "missing before/after",
            "unintended_ok": "",
            "unintended_reason": "",
            "block_scope_ok": "",
            "block_scope_reason": "",
            "poi_grounded_ok": "",
            "poi_grounded_reason": "",
            "intended_days": "",
            "intended_block": "",
            "days_changed": "",
            "notes": "",
            "row_index": "",
            "Timestamp_UQ": "",
            "Timestamp_R": "",
        }
    result = evaluate_edit(
        before,
        after,
        utterance,
        expect_changed_days=expect_set,
        expect_block=data.get("expect_block"),
    )
    row = {
        "Session_Id": name,
        "row_index": "",
        "Timestamp_UQ": "",
        "Timestamp_R": "",
        "question": utterance,
        "source": "fixture",
    }
    row.update(result.to_row())
    return row


if FIXTURE_PATH.strip():
    fpath = Path(FIXTURE_PATH.strip().strip('"'))
    fixture_rows: list[dict[str, Any]] = []
    paths: list[Path]
    if fpath.is_dir():
        paths = sorted(fpath.glob("*.json"))
    else:
        paths = [fpath]
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"Skip {p.name}: {exc}")
            continue
        if isinstance(data, dict):
            fixture_rows.append(_score_fixture_obj(data, p.name))
    if fixture_rows:
        fix_df = pd.DataFrame(fixture_rows)
        print("\n=== Fixture edit scores ===")
        print(fix_df[["Session_Id", "question", "overall", "overall_reason"]].to_string(index=False))
        results = (
            pd.concat([results, fix_df], ignore_index=True)
            if not results.empty
            else fix_df
        )

# %%
# Cell 6 — download results CSV
out_name = "edit_correctness_results.csv"
if "results" in dir() and isinstance(results, pd.DataFrame) and len(results):
    results.to_csv(out_name, index=False, encoding="utf-8-sig")
    print(f"Wrote {out_name} (utf-8-sig for Excel)")
    try:
        from google.colab import files  # type: ignore

        files.download(out_name)
    except ImportError:
        print(f"Saved locally: {Path(out_name).resolve()}")
else:
    print("No results to download.")
