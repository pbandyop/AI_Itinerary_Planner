# %% [markdown]
# # Feasibility eval (Colab / Jupyter)
#
# **Input:** Eval CSV from the companion UI (`rag_eval.csv`) with an `itinerary_json` column
# on plan turns. Tip / RAG-only rows are ignored.
#
# **Load options (Cell 2):** `LOAD_MODE = "upload"` (file picker) or `"path"` (CSV_PATH).
#
# **Pace rules (aligned with live planner):**
# | Pace | Start | Morning | Afternoon | Evening |
# |------|-------|---------|-----------|---------|
# | relaxed | 10:00 | 1 | 1 | 0–1 food/park/market if interest, else relax |
# | balanced | 09:00 | 2 | 2 | same |
# | packed | 08:30 | ≥3 | ≥3, done by **18:00** | same |
# | All | — | — | — | day hard end **21:00**; travel ≤ 90 min |
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


def normalize_pace(raw: Any) -> str:
    p = str(raw or "moderate").strip().lower()
    if p in {"balanced", "balance"}:
        return "moderate"
    if p in DAY_START_MIN:
        return p
    return "moderate"


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


@dataclass
class CheckResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def evaluate_day(day: dict, pace: str) -> CheckResult:
    result = CheckResult(ok=True)
    day_i = day.get("day_index", "?")
    label = f"day{day_i}"
    start_min = DAY_START_MIN.get(pace, DAY_START_MIN["moderate"])
    window_min = DAY_END_MIN - start_min
    ranges = SOFT_RANGES.get(pace, SOFT_RANGES["moderate"])

    # --- Pace: M/A/E presence + soft stop ranges ---
    for bname in BLOCKS:
        block = day.get(bname) if isinstance(day.get(bname), dict) else None
        if not block_present(block):
            result.ok = False
            result.failures.append(
                f"{label} {bname}: missing (need stops or relax/free note)"
            )
            continue
        n = len((block or {}).get("stops") or [])
        lo, hi = ranges[bname]
        relax_only = n == 0 and is_relax_note((block or {}).get("notes"))
        if relax_only:
            result.notes.append(f"{label} {bname}: relax filler (0 stops) ok")
            continue
        if n < lo:
            result.ok = False
            result.failures.append(
                f"{label} {bname}: {n} stops < soft min {lo} ({pace})"
            )
        elif hi is not None and n > hi:
            result.ok = False
            result.failures.append(
                f"{label} {bname}: {n} stops > soft max {hi} ({pace})"
            )
        else:
            result.notes.append(f"{label} {bname}: {n} stops ok")

        # Evening: only food / park / market (etc.)
        if bname == "evening" and n > 0:
            for s in (block or {}).get("stops") or []:
                if not is_evening_ok_stop(s):
                    result.ok = False
                    result.failures.append(
                        f"{label} evening: '{s.get('name')}' category "
                        f"'{s.get('category')}' not allowed "
                        f"(need food/park/market or relax)"
                    )

        # Packed afternoon hard end 18:00
        if bname == "afternoon" and pace == "packed" and n > 0:
            for s in (block or {}).get("stops") or []:
                dep = parse_clock_min(s.get("depart_time"))
                arr = parse_clock_min(s.get("arrive_time"))
                if dep is not None and arr is not None and dep < arr:
                    result.ok = False
                    result.failures.append(
                        f"{label} afternoon: '{s.get('name')}' past midnight "
                        f"(packed PM must end by 18:00)"
                    )
                elif dep is not None and dep > PACKED_AFTERNOON_END_MIN:
                    result.ok = False
                    result.failures.append(
                        f"{label} afternoon: '{s.get('name')}' departs "
                        f"{fmt_clock(dep)} > 18:00 (packed PM hard end)"
                    )

    stops = iter_day_stops(day)

    # --- Travel legs ---
    for s in stops:
        name = s.get("name") or "?"
        leg = s.get("travel_to_next_min")
        if leg is None or leg == "":
            continue
        try:
            leg_i = int(leg)
        except (TypeError, ValueError):
            result.ok = False
            result.failures.append(f"{label}: bad travel_to_next_min on {name}")
            continue
        if leg_i < 0:
            result.ok = False
            result.failures.append(f"{label}: negative travel on {name} ({leg_i})")
        elif leg_i > MAX_LEG_TRAVEL_MIN:
            result.ok = False
            result.failures.append(
                f"{label}: travel {leg_i}m after {name} > {MAX_LEG_TRAVEL_MIN}m"
            )

    # --- Clock window + load ---
    load = 0
    for s in stops:
        load += max(0, int(s.get("duration_min") or 0))
        leg = s.get("travel_to_next_min")
        if leg is not None and leg != "":
            try:
                load += max(0, int(leg))
            except (TypeError, ValueError):
                pass

    if load > window_min:
        result.ok = False
        result.failures.append(
            f"{label}: load {load}m > window {window_min}m "
            f"({fmt_clock(start_min)}–{fmt_clock(DAY_END_MIN)})"
        )
    else:
        result.notes.append(f"{label}: load {load}m ≤ {window_min}m ok")

    arrives = [parse_clock_min(s.get("arrive_time")) for s in stops]
    departs = [parse_clock_min(s.get("depart_time")) for s in stops]
    arrives = [a for a in arrives if a is not None]
    departs = [d for d in departs if d is not None]

    if arrives:
        first = min(arrives)
        if first < start_min:
            result.ok = False
            result.failures.append(
                f"{label}: first arrive {fmt_clock(first)} < day start "
                f"{fmt_clock(start_min)} ({pace})"
            )
        else:
            result.notes.append(f"{label}: first arrive {fmt_clock(first)} ok")

    last_end = None
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

    # Past-midnight depart vs arrive → fail day end
    for s in stops:
        a = parse_clock_min(s.get("arrive_time"))
        d = parse_clock_min(s.get("depart_time"))
        if a is not None and d is not None and d < a:
            result.ok = False
            result.failures.append(
                f"{label}: '{s.get('name')}' past midnight "
                f"({s.get('arrive_time')}→{s.get('depart_time')})"
            )

    if last_end is not None:
        if last_end > DAY_END_MIN:
            result.ok = False
            result.failures.append(
                f"{label}: last activity ends {fmt_clock(last_end)} > 21:00"
            )
        else:
            result.notes.append(f"{label}: ends {fmt_clock(last_end)} ≤ 21:00 ok")
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
        if cursor > DAY_END_MIN:
            result.ok = False
            result.failures.append(
                f"{label}: synthetic end {fmt_clock(cursor)} > 21:00 (no arrive/depart stamps)"
            )
        else:
            result.notes.append(
                f"{label}: synthetic end {fmt_clock(cursor)} ≤ 21:00 ok (no stamps)"
            )

    return result


def evaluate_itinerary(itin: dict, day_paces_override: dict[str, str] | None = None) -> CheckResult:
    trip = itin.get("trip") or {}
    trip_pace = normalize_pace(trip.get("pace"))
    days = itin.get("days") or []
    if not days:
        return CheckResult(ok=False, failures=["no days in itinerary"])

    merged = CheckResult(ok=True)
    merged.notes.append(f"trip_pace={trip_pace}")
    for day in days:
        if not isinstance(day, dict):
            merged.ok = False
            merged.failures.append("invalid day object")
            continue
        day_i = str(day.get("day_index", ""))
        # Prefer CSV day_paces_json → day.pace → trip.pace
        day_pace = trip_pace
        if day_paces_override and day_i in day_paces_override:
            day_pace = normalize_pace(day_paces_override[day_i])
        elif day.get("pace"):
            day_pace = normalize_pace(day.get("pace"))
        merged.notes.append(f"day{day_i}_pace={day_pace}")
        day_res = evaluate_day(day, day_pace)
        if not day_res.ok:
            merged.ok = False
        merged.failures.extend(day_res.failures)
        merged.notes.extend(day_res.notes)
    return merged


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


print("Rules loaded.")
print("Day starts:", {k: fmt_clock(v) for k, v in DAY_START_MIN.items() if k != "balanced"})
print(
    "Day end:",
    fmt_clock(DAY_END_MIN),
    "| packed PM end:",
    fmt_clock(PACKED_AFTERNOON_END_MIN),
    "| max travel:",
    MAX_LEG_TRAVEL_MIN,
    "min",
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
    print("Added empty itinerary_json so you can inspect the table — feasibility needs real plan JSON.\n")

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
# Cell 3 — run feasibility checks
rows_out: list[dict[str, Any]] = []

for idx, row in plans_df.iterrows():
    itin = parse_itinerary_cell(row.get("itinerary_json"))
    session = row.get("Session_Id", "")
    question = str(row.get("question", ""))[:120]
    ts = row.get("Timestamp_R") or row.get("Timestamp_UQ") or ""

    if itin is None:
        rows_out.append(
            {
                "row_index": idx,
                "Session_Id": session,
                "Timestamp_R": ts,
                "question": question,
                "pace": "",
                "num_days": "",
                "verdict": "FAIL",
                "failures": "could not parse itinerary_json",
                "notes": "",
            }
        )
        continue

    trip = itin.get("trip") or {}
    day_paces = parse_day_paces_cell(row.get("day_paces_json"))
    res = evaluate_itinerary(itin, day_paces_override=day_paces)
    pace_label = ""
    if day_paces:
        pace_label = ",".join(f"D{k}:{v}" for k, v in sorted(day_paces.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0))
    else:
        pace_label = normalize_pace(trip.get("pace"))
    rows_out.append(
        {
            "row_index": idx,
            "Session_Id": session,
            "Timestamp_R": ts,
            "question": question,
            "pace": pace_label,
            "day_paces_json": row.get("day_paces_json") or "",
            "num_days": trip.get("num_days") or len(itin.get("days") or []),
            "verdict": "PASS" if res.ok else "FAIL",
            "failures": " | ".join(res.failures),
            "notes": " | ".join(res.notes),
        }
    )

results = pd.DataFrame(rows_out)
n_pass = int((results["verdict"] == "PASS").sum())
n_fail = int((results["verdict"] == "FAIL").sum())
print(f"\n=== Feasibility summary ===")
print(f"Plans checked: {len(results)}  |  PASS: {n_pass}  |  FAIL: {n_fail}")
print()
display_cols = ["Session_Id", "pace", "day_paces_json", "num_days", "verdict", "failures"]
print(results[display_cols].to_string(index=False))

# Show first failing detail
fails = results[results["verdict"] == "FAIL"]
if len(fails):
    print("\n--- First FAIL detail ---")
    r0 = fails.iloc[0]
    print("Session:", r0["Session_Id"])
    print("Question:", r0["question"])
    print("Failures:", r0["failures"])

# %%
# Cell 4 — download results CSV
out_name = "feasibility_results.csv"
results.to_csv(out_name, index=False)
print(f"Wrote {out_name}")

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
# print("Fixture verdict:", "PASS" if smoke.ok else "FAIL")
# print("Failures:", smoke.failures)
# print("Notes:", smoke.notes)
