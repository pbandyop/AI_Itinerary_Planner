"use client";

import type { ReactNode } from "react";

import SourceLink, { resolveSourceHref } from "./SourceLink";

/** Matches agent cites: (Source: Title - https://…) or (Source: Title) */
const SOURCE_CITE_RE =
  /\(Source:\s*([^)]+?)(?:\s*-\s*(https?:\/\/[^)\s]+))?\s*\)/gi;

/**
 * Render assistant text with inline Source links (same pattern as the right panel),
 * instead of dumping the raw URL into the chat bubble.
 */
export default function AssistantReply({ text }: { text: string }) {
  const nodes: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  const re = new RegExp(SOURCE_CITE_RE.source, SOURCE_CITE_RE.flags);
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      nodes.push(text.slice(last, match.index));
    }
    const title = (match[1] || "").trim();
    const url = (match[2] || "").trim();
    const href = resolveSourceHref(url || null, title || null);
    if (href) {
      nodes.push(
        <SourceLink
          key={`src-${key++}`}
          href={href}
          url={url || null}
          datasetOrName={title}
          label="Source"
        />
      );
    } else {
      nodes.push(
        <span key={`src-${key++}`} title={title || undefined}>
          Source
        </span>
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    nodes.push(text.slice(last));
  }
  return <>{nodes.length ? nodes : text}</>;
}

/** Strip citations and URLs so TTS does not read source links aloud. */
export function speakableReply(full: string): string {
  const trimmed = full.trim();
  if (!trimmed) return "";

  const stripCites = (s: string) =>
    s
      .replace(SOURCE_CITE_RE, "")
      .replace(/https?:\/\/\S+/gi, "")
      .replace(/\s{2,}/g, " ")
      .trim();

  if (
    /^From the .+ travel guide/i.test(trimmed) ||
    /^Here's the .+ forecast/i.test(trimmed)
  ) {
    const firstLine =
      trimmed.split("\n").find((l) => l.trim().startsWith("•")) ||
      trimmed.split("\n")[0];
    const plain = stripCites(firstLine.replace(/^•\s*/, ""));
    if (plain.length <= 280) {
      return `${plain} Details and sources are on screen.`;
    }
    return `${plain.slice(0, 260).trim()}… Details and sources are on screen.`;
  }

  const match = trimmed.match(/^(.+?)\.\s*\n/);
  if (match) {
    return stripCites(match[1]) + ". Details and sources are on screen.";
  }

  const intro = stripCites(trimmed.split("\n")[0] || trimmed);
  if (!intro) return "Details and sources are on screen.";
  if (intro.length <= 420) {
    if (/\(Source:/i.test(trimmed) || /https?:\/\//i.test(trimmed)) {
      return `${intro} Source link is on screen.`;
    }
    return intro;
  }
  return `${intro.slice(0, 400).trim()}… Full details and sources are on screen.`;
}
