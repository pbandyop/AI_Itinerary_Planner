# %% [markdown]
# # RAG LLM-as-a-Judge (Faithfulness + Relevance)
#
# **Faithfulness** is scored **only against `retrieval_context`** (not the live web,
# not the itinerary JSON). That is the intended Capstone definition.
#
# | Metric | Uses |
# |--------|------|
# | faithfulness | `question` + `retrieval_context` + `actual_output` |
# | relevance | `question` + `actual_output` |
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


def format_retrieval_context(val) -> str:
    """Normalize Eval CSV retrieval_context into numbered snippet text for the judge."""
    if val is None:
        return ""
    if isinstance(val, float) and np.isnan(val):
        return ""
    try:
        if not isinstance(val, (list, tuple, dict, np.ndarray, pd.Series)) and pd.isna(
            val
        ):
            return ""
    except (ValueError, TypeError):
        pass

    chunks: list[str] = []

    if isinstance(val, pd.Series):
        val = val.tolist()
    if isinstance(val, np.ndarray):
        val = val.tolist()

    if isinstance(val, list):
        chunks = [
            str(i.get("text") or i) if isinstance(i, dict) else str(i) for i in val
        ]
    elif isinstance(val, dict):
        chunks = [str(val.get("text") or val)]
    elif isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in {"nan", "none", "[]"}:
            return ""
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                chunks = [
                    str(i.get("text") or i) if isinstance(i, dict) else str(i)
                    for i in parsed
                ]
            elif isinstance(parsed, dict):
                chunks = [str(parsed.get("text") or parsed)]
            else:
                chunks = [str(parsed)]
        except Exception:
            chunks = [s]
    else:
        chunks = [str(val)]

    chunks = [c.strip() for c in chunks if c and str(c).strip()]
    if not chunks:
        return ""
    return "\n".join(f"[{i + 1}] {c}" for i, c in enumerate(chunks))


def retrieval_is_empty(formatted: str) -> bool:
    t = (formatted or "").strip().lower()
    return (not t) or t in {
        "(no retrieved snippets)",
        "[]",
        "nan",
        "none",
    }


def looks_like_refusal(answer: str) -> bool:
    return bool(_REFUSAL_RE.search(answer or ""))


def empty_context_faithfulness(answer: str) -> tuple[str, str]:
    """Deterministic faithfulness when retrieval_context has no snippets."""
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
# Cell 3 — Gemini client + prompts (faithfulness = retrieval only)

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
        retrieval_raw = row.get("context_parsed", row.get("retrieval_context", ""))
        retrieval_context = format_retrieval_context(retrieval_raw)
        empty_ctx = retrieval_is_empty(retrieval_context)
        display_ctx = (
            "(no retrieved snippets)" if empty_ctx else retrieval_context
        )

        print(f"\n--- row {idx}: {question[:100]}")
        print(f"    retrieval empty={empty_ctx}")

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
print("LLM-AS-A-JUDGE (faithfulness vs retrieval_context)")
print("=" * 50)
for m in METRICS:
    sub = judge_results_df[judge_results_df["evaluation_metric_name"] == m]
    rate = float(sub["LLM_Judge_Response"].mean()) if len(sub) else 0.0
    print(f"{m} pass rate: {rate:.2%} ({int(sub['LLM_Judge_Response'].sum())}/{len(sub)})")
print("=" * 50)
display(judge_results_df)

out_name = "custom_llm_judge_pass_fail.csv"
judge_results_df.to_csv(out_name, index=False, encoding="utf-8-sig")
files.download(out_name)
