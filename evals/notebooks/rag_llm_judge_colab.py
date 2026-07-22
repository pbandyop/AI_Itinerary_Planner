# %% [markdown]
# # RAG LLM-as-a-Judge (Faithfulness + Relevance)
#
# **Faithfulness** is scored against the **selected / winner document only** —
# not the live web, not the itinerary, and **not** the full retrieval pool.
#
# | Metric | Uses |
# |--------|------|
# | faithfulness | `question` + selected grounding text + `actual_output` |
# | relevance | `question` + `actual_output` |
#
# **Selected grounding text (preferred order):**
# 1. `retrieval_context` — full selected grounding text from the live app
#    (after deploy: not the truncated UI snippet)
# 2. Else `retrieved_documents` rows with `selected: true` → full `text`
# 3. Else legacy truncated `retrieval_context` if that is all the CSV has
#
# The full pool in `retrieved_documents` is for debugging Hit@k / sibling pages;
# faithfulness never sees non-selected candidates.
#
# **Empty retrieval rules (no Gemini needed):**
# - Cite-or-refuse answer → **PASS** (faithful refusal)
# - Any other answer → **FAIL** (ungrounded)
#
# Rows are filtered to `source_channel` in {RAG, MIXED} when that column exists.
#
# Paste into Colab top-to-bottom, or run with `# %%` cell support.

# %%
# Cell 1 — deps + load CSV
!pip -q install pandas google-genai

import ast
import getpass
import json
import os
import re
import time

import numpy as np
import pandas as pd
from google.colab import files
from google import genai
from google.genai.errors import ClientError, ServerError

# Reuse prior frame if present
if "trad_df" in globals() and isinstance(trad_df, pd.DataFrame) and len(trad_df):
    eval_df = trad_df.copy()
else:
    print("Upload rag_eval.csv")
    uploaded = files.upload()
    path = list(uploaded.keys())[0]
    try:
        eval_df = pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        eval_df = pd.read_csv(path, encoding="latin1")

print(f"Loaded {len(eval_df)} rows, columns={list(eval_df.columns)}")

if "source_channel" in eval_df.columns:
    filtered = eval_df[
        eval_df["source_channel"].astype(str).str.upper().isin(["RAG", "MIXED"])
    ]
    if len(filtered):
        eval_df = filtered.copy()
        print(f"Filtered to RAG/MIXED: {len(eval_df)} rows")
    else:
        print("No RAG/MIXED rows — keeping full sheet (check source_channel values)")

# %%
# Cell 2 — retrieval formatting + empty-context heuristics

_REFUSAL_RE = re.compile(
    r"(won't invent|will not invent|don't invent|do not invent|"
    r"don't have cited|do not have cited|no cited|"
    r"does not list|do not list|not list opening|"
    r"not in (the )?sources|not in (our|the) (guide|corpus)|"
    r"cannot find|couldn't find|could not find|"
    r"missing_data|i won't make up)",
    re.I,
)


def _parse_cell_value(val):
    """Parse CSV cell that may be JSON, Py' as a reason.thon-literal, or plain text."""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    try:
        if not isinstance(val, (list, tuple, dict, np.ndarray, pd.Series)) and pd.isna(
            val
        ):
            return None
    except (ValueError, TypeError):
        pass

    if isinstance(val, pd.Series):
        return val.tolist()
    if isinstance(val, np.ndarray):
        return val.tolist()
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in {"nan", "none"}:
            return None
        if s == "[]":
            return []
        try:
            return json.loads(s)
        except Exception:
            pass
        try:
            return ast.literal_eval(s)
        except Exception:
            return s
    return val


def format_retrieval_context(val) -> str:
    """Normalize a list/dict/string of snippets into numbered judge context."""
    parsed = _parse_cell_value(val)
    if parsed is None:
        return ""

    chunks: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    chunks.append(text)
                else:
                    chunks.append(str(item))
            else:
                chunks.append(str(item))
    elif isinstance(parsed, dict):
        chunks = [str(parsed.get("text") or parsed)]
    else:
        chunks = [str(parsed)]

    chunks = [c.strip() for c in chunks if c and str(c).strip()]
    if not chunks:
        return ""
    return "\n".join(f"[{i + 1}] {c}" for i, c in enumerate(chunks))


def _is_selected_flag(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val == 1
    return str(val).strip().lower() in {"true", "1", "yes"}


def selected_docs_from_retrieved(val) -> list[dict]:
    """Keep only retrieved_documents entries marked selected=true."""
    parsed = _parse_cell_value(val)
    if not isinstance(parsed, list):
        return []
    selected: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not _is_selected_flag(item.get("selected")):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        selected.append(item)
    return selected


def retrieval_is_empty(formatted: str) -> bool:
    t = (formatted or "").strip().lower()
    return (not t) or t in {
        "(no retrieved snippets)",
        "[]",
        "nan",
        "none",
    }


def faithfulness_context_for_row(row: pd.Series) -> tuple[str, str]:
    """Build faithfulness context: full retrieval_context preferred, else selected docs.

    Returns (formatted_context, source_label).
    """
    # Live app now logs full selected grounding into retrieval_context.
    raw = row.get("context_parsed", row.get("retrieval_context", ""))
    formatted = format_retrieval_context(raw)
    if formatted and not retrieval_is_empty(formatted):
        # Prefer this unless it looks like a truncated UI card (ends with ellipsis
        # mid-hours). In that case try selected retrieved_documents full text.
        looks_truncated = bool(
            re.search(r"(Thur|Wednes|Tues|Mon|Fri|Satur|Sun)…\s*—", formatted)
        ) or bool(re.search(r"\bA…\s*—", formatted))
        if not looks_truncated:
            return formatted, "retrieval_context"

    if "retrieved_documents" in row.index:
        selected = selected_docs_from_retrieved(row.get("retrieved_documents"))
        if selected:
            chunks = []
            for i, doc in enumerate(selected, start=1):
                title = str(doc.get("title") or "source").strip()
                url = str(doc.get("url") or "").strip()
                text = str(doc.get("text") or "").strip()
                head = f"{title}" + (f" — {url}" if url else "")
                chunks.append(f"[{i}] {head}\n{text}")
            return "\n\n".join(chunks), "retrieved_documents.selected"

    if formatted:
        return formatted, "retrieval_context"
    return "", "retrieval_context"


def looks_like_refusal(answer: str) -> bool:
    return bool(_REFUSAL_RE.search(answer or ""))


def empty_context_faithfulness(answer: str) -> tuple[str, str]:
    """Deterministic faithfulness when selected grounding context is empty."""
    if looks_like_refusal(answer):
        return (
            "PASS",
            "Retrieval context is empty and the answer is a cite-or-refuse "
            "(does not invent facts outside sources).",
        )
    return (
        "FAIL",
        "Retrieval context is empty, but the answer adds details that are not "
        "grounded in retrieved snippets (ungrounded / not a clear refusal).",
    )


print("Helpers ready.")

# %%
# Cell 3 — Gemini client + prompts (faithfulness = selected grounding only)

key = getpass.getpass("Paste AI Studio API key: ").strip()
os.environ["GOOGLE_API_KEY"] = key
# gemini-2.5-flash returns 404 for many new AI Studio keys — use 3.5+ Flash.
MODEL = os.environ.get("GEMINI_JUDGE_MODEL", "gemini-3.5-flash")
client = genai.Client(api_key=key)

# Optional: print available flash models if this id 404s.
# for m in client.models.list():
#     name = getattr(m, "name", "") or ""
#     if "flash" in name.lower():
#         print(name)



def call_judge_model(prompt: str, retries: int = 6) -> str:
    last = None
    # Prefer configured model; fall back if Google returns 404 for deprecated ids.
    model_candidates = [MODEL]
    for alt in ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"):
        if alt not in model_candidates:
            model_candidates.append(alt)

    for model_id in model_candidates:
        for attempt in range(retries):
            try:
                r = client.models.generate_content(model=model_id, contents=prompt)
                if model_id != MODEL:
                    print(f"  Using fallback model: {model_id}")
                return (r.text or "").strip()
            except ClientError as e:
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    print(f"  Model {model_id} not available — trying next…")
                    break  # next model candidate
                raise RuntimeError(f"Model/key error ({model_id}): {e}") from e
            except ServerError as e:
                last = e
                wait = min(240, 15 * (2**attempt))
                print(f"  503/busy — sleep {wait}s (try {attempt + 1}/{retries})")
                time.sleep(wait)
    raise RuntimeError(
        f"Gemini unavailable after trying {model_candidates}. Last error: {last}"
    )


def build_metric_prompt(
    metric: str, question: str, retrieval_context: str, actual_output: str
) -> str:
    metric = metric.lower().strip()

    if metric == "faithfulness":
        ctx = retrieval_context.strip() or "(no retrieved snippets)"
        return f"""You are an expert AI Quality Assurance Judge. Evaluate FAITHFULNESS.

Faithfulness means: every factual claim in the GENERATED ANSWER must be supported
by the RETRIEVAL CONTEXT below. Do NOT use outside knowledge. Do NOT use an itinerary
unless that text appears inside the retrieval context.

The retrieval context is the selected grounding document(s) for this answer.
Page title mismatch is OK if the claims are supported by the context text.
Do NOT fail only because the source title differs from the place named in the question.

[DATA TO EVALUATE]
- USER QUESTION: {question}
- RETRIEVAL CONTEXT: {ctx}
- GENERATED ANSWER: {actual_output}

[RULES]
1. Score ONLY against RETRIEVAL CONTEXT.
2. Any claim not stated or clearly implied in the context is FAIL (hallucination),
   even if true in the real world.
3. If context is empty/(no retrieved snippets) and the answer refuses to invent
   (cite-or-refuse), Verdict is PASS.
4. If context is empty and the answer still asserts tips, hours, Day N, itinerary
   details, or other facts, Verdict is FAIL.
5. Paraphrases of the same facts are OK.

[OUTPUT — exactly two lines, no markdown]
Verdict: PASS
Justification: <short text>
OR
Verdict: FAIL
Justification: <short text>
"""

    if metric == "relevance":
        return f"""You are an expert AI Quality Assurance Judge. Evaluate RELEVANCE.

[DATA TO EVALUATE]
- USER QUESTION: {question}
- GENERATED ANSWER: {actual_output}

[RULES]
1. Relevance = the answer addresses what the user asked (on-topic).
2. Do NOT judge factual grounding here.
3. A clear refusal that responds to the question is RELEVANT (PASS).
4. Off-topic answers are FAIL.

[OUTPUT — exactly two lines, no markdown]
Verdict: PASS
Justification: <short text>
OR
Verdict: FAIL
Justification: <short text>
"""

    raise ValueError(f"Unknown metric: {metric}")


def parse_verdict(content: str) -> tuple[str, str]:
    verdict_match = re.search(r"Verdict:\s*(PASS|FAIL)", content, re.I)
    justification_match = re.search(
        r"Justification:\s*(.*)", content, re.I | re.DOTALL
    )
    if verdict_match:
        verdict = verdict_match.group(1).upper()
    else:
        low = content.lower()
        verdict = "PASS" if "pass" in low and "fail" not in low[: low.find("pass") + 4] else "FAIL"
    justification = (
        justification_match.group(1).strip()
        if justification_match
        else content.strip()
    )
    # Keep justification to one paragraph for CSV readability
    justification = re.sub(r"\s+", " ", justification).strip()
    return verdict, justification


print(f"Judge model: {MODEL}")

# %%
# Cell 4 — run judge


def judge_llm(rows: pd.DataFrame, metrics: list[str]) -> list[dict]:
    results: list[dict] = []
    print(f"Evaluating {len(rows)} rows x {len(metrics)} metrics")

    for idx, row in rows.iterrows():
        question = str(row.get("question", "") or "")
        actual_output = str(row.get("actual_output", "") or "")
        # Prefer full selected text from retrieved_documents; else retrieval_context.
        retrieval_context, ctx_source = faithfulness_context_for_row(row)
        empty_ctx = retrieval_is_empty(retrieval_context)
        display_ctx = (
            "(no retrieved snippets)" if empty_ctx else retrieval_context
        )

        print(f"\n--- row {idx}: {question[:100]}")
        print(f"    context_source={ctx_source} empty={empty_ctx}")

        for metric in metrics:
            metric_l = metric.lower().strip()

            # Faithfulness with empty retrieval: deterministic (grounded in context rules)
            if metric_l == "faithfulness" and empty_ctx:
                verdict, justification = empty_context_faithfulness(actual_output)
                print(f"  [{metric}] empty-context rule -> {verdict}")
            else:
                prompt = build_metric_prompt(
                    metric=metric_l,
                    question=question,
                    retrieval_context=display_ctx,
                    actual_output=actual_output,
                )
                content = call_judge_model(prompt)
                verdict, justification = parse_verdict(content)
                print(f"  [{metric}] LLM -> {verdict}")
                time.sleep(1)

            results.append(
                {
                    "row_index": idx,
                    "question": question,
                    "actual_output": actual_output,
                    "retrieval_context": display_ctx,
                    "context_source": ctx_source,
                    "retrieval_empty": empty_ctx,
                    "evaluation_metric_name": metric_l,
                    "LLM_Judge_Response": verdict == "PASS",
                    "verdict": verdict,
                    "justification": justification,
                    "source_channel": row.get("source_channel", ""),
                }
            )

    return results


METRICS = ["faithfulness", "relevance"]
MAX_ROWS = 5  # raise after smoke works
sample_rows = eval_df.head(MAX_ROWS)

judge_results = judge_llm(sample_rows, METRICS)
judge_results_df = pd.DataFrame(judge_results)

print("\n" + "=" * 50)
print("LLM-AS-A-JUDGE (faithfulness vs selected grounding)")
print("=" * 50)
for m in METRICS:
    sub = judge_results_df[judge_results_df["evaluation_metric_name"] == m]
    rate = float(sub["LLM_Judge_Response"].mean()) if len(sub) else 0.0
    print(f"{m} pass rate: {rate:.2%} ({int(sub['LLM_Judge_Response'].sum())}/{len(sub)})")
if "context_source" in judge_results_df.columns:
    print("context_source counts:")
    print(
        judge_results_df.drop_duplicates("row_index")["context_source"]
        .value_counts()
        .to_string()
    )
print("=" * 50)
display(judge_results_df)

out_name = "custom_llm_judge_pass_fail.csv"
judge_results_df.to_csv(out_name, index=False, encoding="utf-8-sig")
files.download(out_name)
