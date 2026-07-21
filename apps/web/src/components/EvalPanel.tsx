"use client";

import { useCallback, useEffect, useState } from "react";

import {
  addEvalColumn,
  clearEvalSheet,
  CORE_COLUMNS,
  downloadCsv,
  EVAL_LOG_KEY,
  formatRetrievalContext,
  isReadOnlyColumn,
  loadEvalSheet,
  parseCsv,
  patchEvalRow,
  saveEvalSheet,
  type EvalRow,
} from "@/lib/evalCsv";
import styles from "./eval-panel.module.css";

type Props = {
  onBack: () => void;
};

export default function EvalPanel({ onBack }: Props) {
  const [columns, setColumns] = useState<string[]>([...CORE_COLUMNS]);
  const [rows, setRows] = useState<EvalRow[]>([]);
  const [newColName, setNewColName] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  const refreshFromStore = useCallback(() => {
    const sheet = loadEvalSheet();
    setColumns(sheet.columns);
    setRows(sheet.rows);
  }, []);

  useEffect(() => {
    refreshFromStore();
    const onStorage = (e: StorageEvent) => {
      if (e.key === EVAL_LOG_KEY) refreshFromStore();
    };
    const onLocal = () => refreshFromStore();
    window.addEventListener("storage", onStorage);
    window.addEventListener("vocalvoyage-eval-log", onLocal);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("vocalvoyage-eval-log", onLocal);
    };
  }, [refreshFromStore]);

  const flash = useCallback((msg: string) => {
    setStatus(msg);
    window.setTimeout(() => setStatus(null), 3500);
  }, []);

  const updateCell = useCallback(
    (rowIdx: number, column: string, value: string) => {
      if (isReadOnlyColumn(column)) return;
      // Merge into latest store so New Trip / other-session appends are kept.
      const sheet = patchEvalRow(rowIdx, column, value);
      setColumns(sheet.columns);
      setRows(sheet.rows);
    },
    []
  );

  const addColumn = useCallback(() => {
    const name = newColName.trim().replace(/\s+/g, "_");
    const result = addEvalColumn(name);
    if ("error" in result) {
      flash(result.error);
      return;
    }
    setColumns(result.columns);
    setRows(result.rows);
    setNewColName("");
    flash(`Added column “${name}”.`);
  }, [flash, newColName]);

  const handleDownload = useCallback(() => {
    const sheet = loadEvalSheet();
    downloadCsv("rag_eval.csv", sheet.columns, sheet.rows);
    // Re-load after downloadCsv persists ensureCoreColumns (itinerary_json).
    const refreshed = loadEvalSheet();
    setColumns(refreshed.columns);
    setRows(refreshed.rows);
    flash("Downloaded rag_eval.csv");
  }, [flash]);

  const handleClear = useCallback(() => {
    if (
      rows.length &&
      !window.confirm(
        "Clear all logged interactions from this browser? This removes rows from every session."
      )
    ) {
      return;
    }
    const empty = clearEvalSheet();
    setColumns(empty.columns);
    setRows(empty.rows);
    flash("Eval log cleared.");
  }, [flash, rows.length]);

  const handleUpload = useCallback(
    async (file: File) => {
      try {
        const text = await file.text();
        const parsed = parseCsv(text);
        saveEvalSheet(parsed);
        setColumns(parsed.columns);
        setRows(parsed.rows);
        flash(`Loaded ${file.name} · ${parsed.rows.length} rows`);
      } catch (err) {
        flash(err instanceof Error ? err.message : "Failed to parse CSV");
      }
    },
    [flash]
  );

  return (
    <div className={styles.panel}>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>Capstone · Grounding & hallucination</p>
          <h1 className={styles.title}>RAG Eval worksheet</h1>
          <p className={styles.lede}>
            Rows accumulate across every planner session (New Trip included) —
            nothing is wiped until you Clear log.{" "}
            <strong>Session_Id</strong> ties a conversation;{" "}
            <strong>Timestamp_UQ</strong> is when you sent the query;{" "}
            <strong>Timestamp_R</strong> is when the reply appeared.{" "}
            <strong>source_channel</strong> is RAG, MCP, mixed, or none. Fill{" "}
            <strong>expected_output</strong> for labeling; add columns as needed.
          </p>
        </div>
        <button type="button" className={styles.backBtn} onClick={onBack}>
          ← Planner
        </button>
      </header>

      <div className={styles.toolbar}>
        <button type="button" className={styles.primaryBtn} onClick={handleDownload}>
          Download CSV
        </button>
        <button type="button" className={styles.secondaryBtn} onClick={refreshFromStore}>
          Refresh
        </button>
        <label className={styles.uploadBtn}>
          Upload CSV
          <input
            type="file"
            accept=".csv,text/csv"
            className={styles.hiddenFile}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleUpload(f);
              e.target.value = "";
            }}
          />
        </label>
        <button type="button" className={styles.secondaryBtn} onClick={handleClear}>
          Clear log
        </button>
        <div className={styles.addCol}>
          <input
            type="text"
            className={styles.colInput}
            placeholder="New column name"
            value={newColName}
            onChange={(e) => setNewColName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addColumn();
            }}
          />
          <button type="button" className={styles.secondaryBtn} onClick={addColumn}>
            + Column
          </button>
        </div>
      </div>

      {status ? (
        <p className={styles.status} role="status">
          {status}
        </p>
      ) : null}

      <section className={styles.tableWrap} aria-label="RAG eval CSV table">
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th className={styles.rowNum}>#</th>
                {columns.map((col) => (
                  <th key={col}>
                    {col}
                    {col === "expected_output" ? (
                      <span className={styles.editableTag}> editable</span>
                    ) : null}
                    {!isReadOnlyColumn(col) && col !== "expected_output" ? (
                      <span className={styles.extraTag}> extra</span>
                    ) : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td
                    colSpan={columns.length + 1}
                    className={styles.emptyCell}
                  >
                    No interactions yet. Use the planner (ask tips, hours, safety,
                    or plan a trip) — each reply is logged here automatically.
                  </td>
                </tr>
              ) : (
                rows.map((row, rowIdx) => (
                  <tr key={`row-${rowIdx}`}>
                    <td className={styles.rowNum}>{rowIdx + 1}</td>
                    {columns.map((col) => {
                      const value = row[col] ?? "";
                      if (col === "retrieval_context") {
                        return (
                          <td key={col} className={styles.contextCell}>
                            <pre className={styles.contextPre}>
                              {formatRetrievalContext(value)}
                            </pre>
                          </td>
                        );
                      }
                      if (col === "itinerary_json" || col === "day_paces_json") {
                        const preview = value
                          ? `${value.slice(0, 120)}${value.length > 120 ? "…" : ""}`
                          : "";
                        return (
                          <td
                            key={col}
                            className={styles.textCell}
                            title={value ? `${value.length} chars` : undefined}
                          >
                            {preview || "—"}
                          </td>
                        );
                      }
                      if (!isReadOnlyColumn(col)) {
                        return (
                          <td key={col} className={styles.editCell}>
                            <textarea
                              className={styles.cellInput}
                              value={value}
                              placeholder={
                                col === "expected_output"
                                  ? "Add gold / expected answer…"
                                  : ""
                              }
                              rows={col === "expected_output" ? 3 : 2}
                              onChange={(e) =>
                                updateCell(rowIdx, col, e.target.value)
                              }
                            />
                          </td>
                        );
                      }
                      return (
                        <td key={col} className={styles.textCell}>
                          {value}
                        </td>
                      );
                    })}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <p className={styles.footnote}>
        Core columns: <code>Session_Id</code>, <code>Timestamp_UQ</code>,{" "}
        <code>Timestamp_R</code>, <code>question</code>,{" "}
        <code>retrieval_context</code>, <code>source_channel</code>,{" "}
        <code>actual_output</code>, <code>expected_output</code>. Logged in
        this browser only.
      </p>
    </div>
  );
}
