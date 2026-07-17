"use client";

import { useState } from "react";

import type { PipelineLogStep } from "@/lib/agent";
import styles from "./pipeline-trace.module.css";

function agentTone(agent: string): string {
  const a = agent.toLowerCase();
  if (a === "user") return styles.toneUser;
  if (a.includes("orchestrator")) return styles.toneOrch;
  if (
    a.includes("poi") ||
    a.includes("knowledge") ||
    a.includes("weather") ||
    a.includes("travel")
  )
    return styles.toneSpecialist;
  if (a.includes("itinerary")) return styles.toneItin;
  if (a.includes("synthesis") || a.includes("merger")) return styles.toneSynth;
  if (a.includes("reviewer")) return styles.toneReview;
  return styles.toneDefault;
}

export default function PipelineTrace({
  steps,
  pending,
  userMessage,
}: {
  steps: PipelineLogStep[];
  pending?: boolean;
  userMessage?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [openIdx, setOpenIdx] = useState<number | null>(null);

  const shown =
    steps.length > 0
      ? steps
      : pending && userMessage
        ? [
            {
              stage: "1 · User input",
              agent: "user",
              summary: userMessage,
            },
            {
              stage: "… running",
              agent: "orchestrator",
              summary: "Graph invoke in progress — specialists, MCP, RAG…",
            },
          ]
        : [];

  if (!shown.length) return null;

  return (
    <section className={styles.panel} aria-label="Pipeline log">
      <button
        type="button"
        className={styles.toggle}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <div className={styles.head}>
          <h2>Pipeline log</h2>
          <p className={styles.sub}>
            {expanded
              ? "User → Orchestrator → specialists → itinerary → synthesis → reviewer"
              : `${shown.length} step${shown.length === 1 ? "" : "s"} · click to expand`}
          </p>
        </div>
        <span className={styles.panelChev} aria-hidden>
          {expanded ? "▾" : "▸"}
        </span>
      </button>
      {expanded ? (
        <ol className={styles.list}>
          {shown.map((step, idx) => {
            const open = openIdx === idx;
            const detail =
              step.detail !== undefined
                ? JSON.stringify(step.detail, null, 2)
                : null;
            return (
              <li key={`${step.stage}-${idx}`} className={styles.item}>
                <button
                  type="button"
                  className={`${styles.row} ${agentTone(step.agent)}`}
                  onClick={() => setOpenIdx(open ? null : idx)}
                  aria-expanded={open}
                >
                  <span className={styles.stage}>{step.stage}</span>
                  <span className={styles.summary}>{step.summary}</span>
                  <span className={styles.chev}>{open ? "▾" : "▸"}</span>
                </button>
                {open && detail && (
                  <pre className={styles.detail}>{detail}</pre>
                )}
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}
