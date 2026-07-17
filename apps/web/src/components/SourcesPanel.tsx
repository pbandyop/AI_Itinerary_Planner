"use client";

import { useState } from "react";

import type { Dataset, Itinerary, Source } from "@/types/itinerary";
import SourceLink from "./SourceLink";
import styles from "./sources-panel.module.css";

const DATASET_LABEL: Record<Dataset, string> = {
  openstreetmap: "OpenStreetMap",
  wikivoyage: "Wikivoyage",
  wikipedia: "Wikipedia",
  "open-meteo": "Open-Meteo",
  other: "Other",
};

function sourceKey(s: Source): string {
  return (
    s.source_id ||
    `${s.title}|${s.url || ""}|${s.dataset}|${(s.snippet || "").slice(0, 40)}`
  );
}

/** Prefer API sources; fall back to itinerary + stop citations. */
export function collectSources(
  apiSources: Source[] | null | undefined,
  itinerary: Itinerary | null | undefined
): Source[] {
  const seen = new Set<string>();
  const out: Source[] = [];

  const push = (s: Source | null | undefined) => {
    if (!s?.title) return;
    const key = sourceKey(s);
    if (seen.has(key)) return;
    seen.add(key);
    out.push(s);
  };

  for (const s of apiSources || []) push(s);
  for (const s of itinerary?.sources || []) push(s);
  if (itinerary) {
    for (const day of itinerary.days) {
      for (const block of [day.morning, day.afternoon, day.evening]) {
        for (const stop of block?.stops || []) {
          for (const c of stop.citations || []) push(c);
        }
      }
    }
  }
  return out;
}

export default function SourcesPanel({ sources }: { sources: Source[] }) {
  const [expanded, setExpanded] = useState(false);

  if (!sources.length) return null;

  const shown = sources.slice(0, 14);

  return (
    <section className={styles.wrap} aria-label="References">
      <button
        type="button"
        className={styles.toggle}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <header className={styles.header}>
          <h2>References</h2>
          <p className={styles.sub}>
            {expanded
              ? "Stops on this plan (OSM), weather, and travel times — or guide tips when you ask about a place."
              : `${sources.length} source${sources.length === 1 ? "" : "s"} · click to expand`}
          </p>
        </header>
        <span className={styles.panelChev} aria-hidden>
          {expanded ? "▾" : "▸"}
        </span>
      </button>
      {expanded ? (
        <>
          <ol className={styles.list}>
            {shown.map((s, i) => {
              const label = DATASET_LABEL[s.dataset] || s.dataset;
              return (
                <li key={`${sourceKey(s)}-${i}`} className={styles.item}>
                  <div className={styles.row}>
                    <span className={styles.title}>{s.title}</span>
                    <span className={styles.meta}>
                      <SourceLink url={s.url} datasetOrName={s.dataset} />
                      <span className={styles.badge} data-dataset={s.dataset}>
                        {label}
                      </span>
                    </span>
                  </div>
                  {s.snippet ? (
                    <p className={styles.snippet}>{s.snippet}</p>
                  ) : null}
                </li>
              );
            })}
          </ol>
          {sources.length > shown.length ? (
            <p className={styles.more}>
              +{sources.length - shown.length} more place citations from
              OpenStreetMap
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
