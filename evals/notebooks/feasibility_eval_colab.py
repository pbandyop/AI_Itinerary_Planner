# %% [markdown]
# # Feasibility eval (Colab / Jupyter)
#
# **Input:** Eval CSV from the companion UI (`rag_eval.csv`) with an `itinerary_json` column
# on plan turns. Tip / RAG-only rows are ignored.
#
# **Load options (Cell 2):** `LOAD_MODE = "upload"` (file picker) or `"path"` (CSV_PATH).
#
# **Rules (Capstone):**
# 1. Daily clock window — pace start → 21:00; load ≤ window minutes
# 2. Travel legs ≤ 90 min; negative travel fails
# 3. Pace: M/A/E present (stops and/or relax notes); soft ranges on real stops only
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
MAX_LEG_TRAVEL_MIN = 90

# Planner day starts (minutes from midnight)
DAY_START_MIN = {
    "relaxed": 10 * 60,  # 10:00
    "moderate": 9 * 60,  # 09:00  (UI label: balanced)
    "balanced": 9 * 60,
    "packed": 8 * 60 + 30,  # 08:30
}

# Soft ranges on real POI stops only (not relax fillers).
# packed: (min, None) = floor only
SOFT_RANGES: dict[str, dict[str, tuple[int, int | None]]] = {
    "relaxed": {"morning": (1, 3), "afternoon": (1, 3), "evening": (1, 2)},
    "moderate": {"morning": (2, 4), "afternoon": (2, 4), "evening": (1, 3)},
    "balanced": {"morning": (2, 4), "afternoon": (2, 4), "evening": (1, 3)},
    "packed": {"morning": (3, None), "afternoon": (3, None), "evening": (2, None)},
}

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
    # Presence: real stops and/or explicit relax/free note.
    # Soft ranges apply to real POI counts only. A relax-only block is present
    # and skips the soft min (relax fills the slot without counting as a stop).
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
        # fallback: last arrive + its duration
        last_stop = None
        last_a = -1
        for s in stops:
            a = parse_clock_min(s.get("arrive_time"))
            if a is not None and a >= last_a:
                last_a = a
                last_stop = s
        if last_stop is not None:
            last_end = last_a + max(0, int(last_stop.get("duration_min") or 0))

    if last_end is not None:
        if last_end > DAY_END_MIN:
            result.ok = False
            result.failures.append(
                f"{label}: last activity ends {fmt_clock(last_end)} > 21:00"
            )
        else:
            result.notes.append(f"{label}: ends {fmt_clock(last_end)} ≤ 21:00 ok")
    elif stops:
        # No clocks — synthetic span from day start using durations + travel
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


def evaluate_itinerary(itin: dict) -> CheckResult:
    trip = itin.get("trip") or {}
    pace = normalize_pace(trip.get("pace"))
    days = itin.get("days") or []
    if not days:
        return CheckResult(ok=False, failures=["no days in itinerary"])

    merged = CheckResult(ok=True)
    merged.notes.append(f"pace={pace}")
    for day in days:
        if not isinstance(day, dict):
            merged.ok = False
            merged.failures.append("invalid day object")
            continue
        day_res = evaluate_day(day, pace)
        if not day_res.ok:
            merged.ok = False
        merged.failures.extend(day_res.failures)
        merged.notes.extend(day_res.notes)
    return merged


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
print("Day end:", fmt_clock(DAY_END_MIN), "| max travel:", MAX_LEG_TRAVEL_MIN, "min")

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
    pace = normalize_pace(trip.get("pace"))
    res = evaluate_itinerary(itin)
    rows_out.append(
        {
            "row_index": idx,
            "Session_Id": session,
            "Timestamp_R": ts,
            "question": question,
            "pace": pace,
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
display_cols = ["Session_Id", "pace", "num_days", "verdict", "failures"]
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
