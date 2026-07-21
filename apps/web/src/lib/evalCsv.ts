/** CSV helpers + live RAG eval log from real UI interactions. */

import type { Source } from "@/types/itinerary";

export const EVAL_LOG_KEY = "vocalvoyage.ragEvalLog.v1";

export const CORE_COLUMNS = [
  "Session_Id",
  "Timestamp_UQ",
  "Timestamp_R",
  "question",
  "retrieval_context",
  "source_channel",
  "actual_output",
  "expected_output",
  "itinerary_json",
  "day_paces_json",
] as const;

export type SourceChannel = "RAG" | "MCP" | "mixed" | "none";

export type CoreColumn = (typeof CORE_COLUMNS)[number];

export type EvalRow = Record<string, string>;

export type EvalSheet = { columns: string[]; rows: EvalRow[] };

/** Ensure every CORE_COLUMNS entry exists (older logs / uploaded CSVs). */
export function ensureCoreColumns(sheet: EvalSheet): EvalSheet {
  const preferredIndex: Record<string, number> = {
    Session_Id: 0,
    Timestamp_UQ: 1,
    Timestamp_R: 2,
    question: 3,
    retrieval_context: 4,
    source_channel: 5,
    actual_output: 6,
    expected_output: 7,
    itinerary_json: 8,
    day_paces_json: 9,
  };
  const columns = [...(sheet.columns || [])];
  for (const c of CORE_COLUMNS) {
    if (!columns.includes(c)) {
      const prefer = preferredIndex[c] ?? columns.length;
      columns.splice(Math.min(prefer, columns.length), 0, c);
    }
  }
  const rows = (sheet.rows || []).map((r) => {
    const row: EvalRow = { ...r };
    for (const c of columns) {
      if (row[c] == null) row[c] = "";
    }
    return row;
  });
  return { columns, rows };
}

/** Empty sheet — no dummy rows; filled only by live UI turns. */
export function emptyEvalSheet(): EvalSheet {
  return { columns: [...CORE_COLUMNS], rows: [] };
}

export function loadEvalSheet(): EvalSheet {
  if (typeof window === "undefined") return emptyEvalSheet();
  try {
    const raw = window.localStorage.getItem(EVAL_LOG_KEY);
    if (!raw) return emptyEvalSheet();
    const parsed = JSON.parse(raw) as Partial<EvalSheet>;
    const columns =
      Array.isArray(parsed.columns) && parsed.columns.length
        ? parsed.columns
        : [...CORE_COLUMNS];
    const rows = Array.isArray(parsed.rows) ? parsed.rows : [];
    const sheet = ensureCoreColumns({ columns, rows: rows as EvalRow[] });
    // Persist migration so downloads / reloads keep itinerary_json.
    const before = Array.isArray(parsed.columns) ? parsed.columns.join("\0") : "";
    const after = sheet.columns.join("\0");
    if (before !== after) {
      saveEvalSheet(sheet);
    }
    return sheet;
  } catch {
    return emptyEvalSheet();
  }
}

export function saveEvalSheet(sheet: EvalSheet): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(EVAL_LOG_KEY, JSON.stringify(sheet));
  } catch {
    /* quota */
  }
}

export function clearEvalSheet(): EvalSheet {
  const empty = emptyEvalSheet();
  saveEvalSheet(empty);
  return empty;
}

/** True for tip / POI / hours turns that should log knowledge citations. */
export function isItineraryExplainTurn(
  question: string | null | undefined,
  agentTrace?: Array<Record<string, unknown>> | null
): boolean {
  const q = (question || "").toLowerCase();
  if (
    /\bwhy\b[\s\S]{0,48}\b(pick|choose|include)\b/.test(q) ||
    /\b(doable|feasible|too (?:much|packed)|can (?:i|we) (?:do|finish))\b/.test(q)
  ) {
    return true;
  }
  for (const entry of agentTrace || []) {
    const mode = String(entry.mode || "").toLowerCase();
    if (mode === "planner_why" || mode === "doability") return true;
  }
  return false;
}

/** True for tip / POI / hours turns that should log knowledge citations. */
export function isKnowledgeTurn(
  intent: string | null | undefined,
  agentTrace?: Array<Record<string, unknown>> | null,
  question?: string | null
): boolean {
  // Plan-why / doability answers are itinerary-owned, not RAG — even if intent=explain.
  if (isItineraryExplainTurn(question, agentTrace)) return false;
  if ((intent || "").toLowerCase() === "explain") return true;
  for (const entry of agentTrace || []) {
    const tool = String(entry.tool || entry.action || "").toLowerCase();
    if (
      tool.includes("knowledge_rag") ||
      tool.includes("knowledge_qa") ||
      (tool.includes("rag") && !tool.includes("frag"))
    ) {
      return true;
    }
  }
  return false;
}

/** Map a Source.dataset (or similar) to RAG vs MCP. */
export function channelFromDataset(dataset: string | null | undefined): "RAG" | "MCP" | "other" {
  const ds = (dataset || "").toLowerCase().trim();
  // Guide / corpus retrieval (including Places cards in the RAG index)
  if (
    ds === "wikivoyage" ||
    ds === "wikipedia" ||
    ds === "google_places" ||
    ds === "curated_places" ||
    ds === "tourism"
  ) {
    return "RAG";
  }
  // Live MCP tools / itinerary OSM attribution
  if (ds === "open-meteo") return "MCP";
  if (ds === "openstreetmap") return "MCP";
  return "other";
}

function channelFromSource(
  s: Source,
  opts?: { knowledgeTurn?: boolean }
): "RAG" | "MCP" | "other" {
  const id = (s.source_id || "").toLowerCase();
  if (id.includes("travel-time") || id === "open-meteo") return "MCP";
  // Knowledge RAG may retrieve OSM listing cards from the corpus — still retrieval.
  if (opts?.knowledgeTurn) {
    if (id.includes("travel-time") || s.dataset === "open-meteo") return "MCP";
    return "RAG";
  }
  const fromDs = channelFromDataset(s.dataset);
  if (fromDs !== "other") return fromDs;
  // OSM-shaped ids → MCP POI grounding (plan References)
  if (/^(node|way|relation)\/\d+$/i.test(s.source_id || "")) return "MCP";
  return "other";
}

function channelsFromAgentTrace(
  trace: Array<Record<string, unknown>> | null | undefined
): Set<"RAG" | "MCP"> {
  const found = new Set<"RAG" | "MCP">();
  for (const entry of trace || []) {
    const tool = String(entry.tool || entry.source || entry.action || "").toLowerCase();
    if (!tool) continue;
    if (
      tool.includes("knowledge_rag") ||
      tool.includes("knowledge_qa") ||
      (tool.includes("rag") && !tool.includes("frag"))
    ) {
      found.add("RAG");
    }
    if (
      tool.includes("_mcp") ||
      tool.includes("mcp") ||
      tool.includes("poi_search") ||
      tool.includes("travel_time") ||
      tool.includes("weather") ||
      tool.includes("itinerary_builder")
    ) {
      found.add("MCP");
    }
  }
  return found;
}

/** Turn-level channel: RAG, MCP, mixed, or none. */
export function inferSourceChannel(
  sources: Source[] | null | undefined,
  agentTrace?: Array<Record<string, unknown>> | null,
  question?: string | null
): SourceChannel {
  // Why-pick / doability: itinerary justification path — not RAG.
  if (isItineraryExplainTurn(question, agentTrace)) {
    return "none";
  }
  const knowledgeTurn = isKnowledgeTurn(null, agentTrace, question);
  // Tip / knowledge turns are RAG even if a corpus OSM card was retrieved.
  if (
    knowledgeTurn ||
    (agentTrace || []).some((e) =>
      String(e.action || e.tool || "")
        .toLowerCase()
        .includes("knowledge_qa")
    )
  ) {
    const fromTrace = channelsFromAgentTrace(agentTrace);
    if (fromTrace.has("RAG") && !fromTrace.has("MCP")) return "RAG";
    if (sources && sources.length) return "RAG";
  }
  const fromSources = new Set<"RAG" | "MCP">();
  for (const s of sources || []) {
    const ch = channelFromSource(s, { knowledgeTurn });
    if (ch === "RAG" || ch === "MCP") fromSources.add(ch);
  }
  const fromTrace = channelsFromAgentTrace(agentTrace);
  const all = new Set<"RAG" | "MCP">([...fromSources, ...fromTrace]);
  if (all.has("RAG") && all.has("MCP")) return "mixed";
  if (all.has("RAG")) return "RAG";
  if (all.has("MCP")) return "MCP";
  return "none";
}

export function sourcesToRetrievalContext(
  sources: Source[] | null | undefined,
  opts?: { knowledgeTurn?: boolean }
): string {
  const chunks: Array<{
    channel: string;
    dataset: string;
    text: string;
  }> = [];
  for (const s of sources || []) {
    const text = [s.title, s.snippet, s.url].filter(Boolean).join(" — ");
    if (!text.trim()) continue;
    chunks.push({
      channel: channelFromSource(s, opts),
      dataset: s.dataset || "other",
      text: text.trim(),
    });
  }
  // Always emit JSON (including []) so the faithfulness judge can tell
  // "no snippets retrieved" from a missing column.
  return JSON.stringify(chunks);
}

/**
 * Sources for eval CSV / tip grounding.
 * Explain turns: RAG (or turn-level) sources only — never the itinerary MCP dump.
 * Why-pick / doability: no retrieval sources (itinerary-owned answer).
 */
export function sourcesForEvalLog(input: {
  intent: string | null | undefined;
  sources: Source[] | null | undefined;
  itinerarySources?: Source[] | null | undefined;
  agentTrace?: Array<Record<string, unknown>> | null;
  question?: string | null;
}): Source[] {
  const top = Array.isArray(input.sources) ? input.sources : [];
  const itin = Array.isArray(input.itinerarySources)
    ? input.itinerarySources
    : [];
  if (isItineraryExplainTurn(input.question, input.agentTrace)) {
    return [];
  }
  const knowledge =
    isKnowledgeTurn(input.intent, input.agentTrace, input.question) ||
    (input.agentTrace || []).some((e) =>
      String(e.action || e.tool || "")
        .toLowerCase()
        .includes("knowledge_qa")
    );
  if (knowledge) {
    // Prefer guide prose datasets when present.
    const guide = top.filter((s) =>
      ["wikivoyage", "wikipedia", "tourism", "curated_places"].includes(
        (s.dataset || "").toLowerCase()
      )
    );
    if (guide.length) return guide;
    if (top.length) return top;
    return [];
  }
  return top.length ? top : itin;
}

/**
 * Append one live turn. Always loads the current store first so rows from
 * earlier sessions / New Trip remain; never clears on Session_Id change.
 */
export function appendLiveEvalRow(input: {
  sessionId: string;
  timestampUq: string;
  timestampR: string;
  question: string;
  retrievalContext: string;
  sourceChannel: string;
  actualOutput: string;
  /** Full merged itinerary JSON when a plan was created/updated this turn. */
  itineraryJson?: string;
  /** Per-day pace map JSON, e.g. {"1":"relaxed","2":"moderate"}. */
  dayPacesJson?: string;
}): EvalRow {
  const sheet = loadEvalSheet();
  // Keep any extra columns the user added in the Eval UI.
  if (!sheet.columns.includes("source_channel")) {
    const idx = sheet.columns.indexOf("retrieval_context");
    sheet.columns.splice(idx >= 0 ? idx + 1 : sheet.columns.length, 0, "source_channel");
  }
  if (!sheet.columns.includes("itinerary_json")) {
    sheet.columns.push("itinerary_json");
  }
  if (!sheet.columns.includes("day_paces_json")) {
    sheet.columns.push("day_paces_json");
  }
  const row: EvalRow = {};
  for (const c of sheet.columns) row[c] = "";
  row.Session_Id = input.sessionId;
  row.Timestamp_UQ = input.timestampUq;
  row.Timestamp_R = input.timestampR;
  row.question = input.question;
  row.retrieval_context = input.retrievalContext;
  row.source_channel = input.sourceChannel;
  row.actual_output = input.actualOutput;
  row.expected_output = "";
  row.itinerary_json = input.itineraryJson || "";
  row.day_paces_json = input.dayPacesJson || "";
  sheet.rows = [...sheet.rows, row];
  saveEvalSheet(sheet);
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("vocalvoyage-eval-log"));
  }
  return row;
}

/**
 * Patch editable cells without dropping rows appended since the UI last rendered
 * (e.g. new session turns while Eval tab was open).
 */
export function patchEvalRow(
  rowIdx: number,
  column: string,
  value: string
): EvalSheet {
  const sheet = loadEvalSheet();
  if (rowIdx < 0 || rowIdx >= sheet.rows.length) return sheet;
  if (isReadOnlyColumn(column)) return sheet;
  const nextRows = sheet.rows.map((r, i) =>
    i === rowIdx ? { ...r, [column]: value } : r
  );
  const next = { columns: sheet.columns, rows: nextRows };
  saveEvalSheet(next);
  return next;
}

export function addEvalColumn(name: string): EvalSheet | { error: string } {
  const sheet = loadEvalSheet();
  if (!name) return { error: "Enter a column name first." };
  if (sheet.columns.includes(name)) {
    return { error: `Column “${name}” already exists.` };
  }
  const next = {
    columns: [...sheet.columns, name],
    rows: sheet.rows.map((r) => ({ ...r, [name]: "" })),
  };
  saveEvalSheet(next);
  return next;
}

function escapeCell(value: string): string {
  if (/[",\n\r]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/** Minimal RFC4180-ish parser (handles quotes and commas). */
export function parseCsv(text: string): EvalSheet {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let i = 0;
  let inQuotes = false;
  const src = text.replace(/^\uFEFF/, "");

  while (i < src.length) {
    const ch = src[i];
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          cell += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        continue;
      }
      cell += ch;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      i += 1;
      continue;
    }
    if (ch === ",") {
      row.push(cell);
      cell = "";
      i += 1;
      continue;
    }
    if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i += 1;
      row.push(cell);
      cell = "";
      if (row.some((c) => c.trim() !== "") || rows.length === 0) {
        rows.push(row);
      }
      row = [];
      i += 1;
      continue;
    }
    cell += ch;
    i += 1;
  }
  if (cell.length || row.length) {
    row.push(cell);
    rows.push(row);
  }

  if (!rows.length) {
    return emptyEvalSheet();
  }

  const columns = rows[0].map((c) => c.trim() || "column");
  const dataRows: EvalRow[] = [];
  for (const raw of rows.slice(1)) {
    if (raw.every((c) => !String(c).trim())) continue;
    const obj: EvalRow = {};
    columns.forEach((col, idx) => {
      obj[col] = raw[idx] ?? "";
    });
    dataRows.push(obj);
  }
  // Always keep core eval columns (including itinerary_json) after upload.
  return ensureCoreColumns({ columns, rows: dataRows });
}

export function toCsv(columns: string[], rows: EvalRow[]): string {
  const sheet = ensureCoreColumns({ columns, rows });
  const lines = [sheet.columns.map(escapeCell).join(",")];
  for (const r of sheet.rows) {
    lines.push(sheet.columns.map((c) => escapeCell(r[c] ?? "")).join(","));
  }
  return lines.join("\n") + "\n";
}

export function downloadCsv(
  filename: string,
  columns: string[],
  rows: EvalRow[]
): void {
  const sheet = ensureCoreColumns({ columns, rows });
  // Persist so the Eval table / next download keep itinerary_json.
  saveEvalSheet(sheet);
  // BOM so Excel opens UTF-8 correctly (avoids â€" / CafÃ© mojibake).
  const blob = new Blob(["\uFEFF" + toCsv(sheet.columns, sheet.rows)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function formatRetrievalContext(raw: string): string {
  const trimmed = (raw || "").trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (Array.isArray(parsed)) {
      if (!parsed.length) return "(no retrieved snippets)";
      return parsed
        .map((x, i) => {
          if (x && typeof x === "object" && "text" in x) {
            const o = x as {
              channel?: string;
              dataset?: string;
              text?: string;
            };
            const tag = [o.channel, o.dataset].filter(Boolean).join("/");
            return `${i + 1}. [${tag || "?"}] ${String(o.text || "")}`;
          }
          return `${i + 1}. ${String(x)}`;
        })
        .join("\n");
    }
  } catch {
    /* plain text */
  }
  return trimmed;
}

/** Columns that stay read-only in the Eval table (captured from live traffic). */
export function isReadOnlyColumn(col: string): boolean {
  return (
    col === "Session_Id" ||
    col === "Timestamp_UQ" ||
    col === "Timestamp_R" ||
    col === "question" ||
    col === "retrieval_context" ||
    col === "source_channel" ||
    col === "actual_output" ||
    col === "itinerary_json" ||
    col === "day_paces_json"
  );
}

/**
 * Intent gate (option B): only plan/edit turns log itinerary structure.
 * Tip / hours / rain / clarify turns keep sticky merged_itinerary in the UI
 * but must not flood the Eval CSV.
 */
export function shouldLogItineraryJson(
  intent: string | null | undefined
): boolean {
  const i = (intent || "").toLowerCase().trim();
  return i === "plan" || i === "edit";
}

/**
 * Per-day pace map for feasibility, e.g. {"1":"relaxed","2":"moderate"}.
 * Prefers day.pace when set (single-day voice edits); else trip.pace.
 */
export function dayPacesFromItinerary(
  itinerary: {
    trip?: { pace?: string | null } | null;
    days?: Array<{ day_index?: number; pace?: string | null }> | null;
  } | null | undefined
): string {
  if (!itinerary?.days?.length) return "";
  const tripPace = itinerary.trip?.pace || null;
  const map: Record<string, string> = {};
  for (const d of itinerary.days) {
    const idx = d.day_index;
    if (idx == null) continue;
    const p = (d.pace || tripPace || "").toString().toLowerCase().trim();
    if (!p) continue;
    map[String(idx)] = p === "balanced" ? "moderate" : p;
  }
  return Object.keys(map).length ? JSON.stringify(map) : "";
}
