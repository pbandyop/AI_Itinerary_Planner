"use client";

import styles from "./source-link.module.css";

const KNOWN_SOURCE_HREFS: Record<string, string> = {
  "open-meteo": "https://open-meteo.com/",
  "open meteo": "https://open-meteo.com/",
  openstreetmap: "https://www.openstreetmap.org/",
  osm: "https://www.openstreetmap.org/",
  wikivoyage: "https://en.wikivoyage.org/",
  wikipedia: "https://en.wikipedia.org/",
};

/** Resolve a clickable href from an explicit URL or a known dataset/source name. */
export function resolveSourceHref(
  url?: string | null,
  datasetOrName?: string | null
): string | null {
  const direct = (url || "").trim();
  if (/^https?:\/\//i.test(direct)) return direct;
  for (const candidate of [direct, (datasetOrName || "").trim()]) {
    const key = candidate.toLowerCase();
    if (key && KNOWN_SOURCE_HREFS[key]) return KNOWN_SOURCE_HREFS[key];
  }
  return null;
}

/** Compact clickable “Source” label — never shows the raw URL text. */
export default function SourceLink({
  href,
  url,
  datasetOrName,
  className,
  label = "Source",
}: {
  href?: string | null;
  url?: string | null;
  datasetOrName?: string | null;
  className?: string;
  label?: string;
}) {
  const resolved =
    (href && /^https?:\/\//i.test(href) ? href : null) ||
    resolveSourceHref(url, datasetOrName);
  if (!resolved) return null;
  return (
    <a
      href={resolved}
      target="_blank"
      rel="noopener noreferrer"
      className={`${styles.link}${className ? ` ${className}` : ""}`}
    >
      {label}
    </a>
  );
}
