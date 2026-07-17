/** Typed client for the LangGraph agent `/invoke` API (Phase 5). */

import type { Itinerary, Source, TripConstraints } from "@/types/itinerary";
import type {
  POISearchResult,
  TravelTimeResult,
  WeatherResult,
} from "@/types/mcp";

export interface ConversationTurn {
  role: "user" | "assistant";
  content: string;
}

export interface InvokeRequest {
  user_message: string;
  session_id?: string | null;
  conversation?: ConversationTurn[];
  previous_itinerary?: Itinerary | null;
  merged_itinerary?: Itinerary | null;
  trip_constraints?: TripConstraints | null;
}

export interface InvokeResponse {
  user_reply: string;
  intent: string | null;
  safety_status: string | null;
  revision_count: number;
  trip_constraints: TripConstraints | null;
  merged_itinerary: Itinerary | null;
  travel_time_results?: TravelTimeResult | null;
  weather_results?: WeatherResult | null;
  poi_results?: POISearchResult | null;
  sources: Source[] | null;
  agent_trace?: AgentTraceEntry[];
  pipeline_log?: PipelineLogStep[];
  raw_state?: Record<string, unknown> | null;
}

export interface AgentTraceEntry {
  agent?: string;
  action?: string;
  tool?: string;
  source?: string;
  [key: string]: unknown;
}

export interface PipelineLogStep {
  stage: string;
  agent: string;
  summary: string;
  detail?: unknown;
  index?: number;
}

/**
 * Browser: prefer absolute NEXT_PUBLIC_AGENT_BASE_URL (Railway) so long
 * /invoke calls are not killed by Vercel's ~120s external-rewrite timeout.
 * Localhost keeps same-origin `/api/agent` rewrites.
 */
export function agentBaseUrl(): string {
  const publicBase =
    process.env.NEXT_PUBLIC_AGENT_BASE_URL?.replace(/\/$/, "") || "";

  if (typeof window !== "undefined") {
    if (
      publicBase &&
      /^https?:\/\//i.test(publicBase) &&
      !/localhost|127\.0\.0\.1/i.test(publicBase)
    ) {
      return publicBase;
    }
    return "/api/agent";
  }

  return publicBase || "http://localhost:8000";
}

export async function invokeAgent(
  body: InvokeRequest,
  signal?: AbortSignal
): Promise<InvokeResponse> {
  let res: Response;
  try {
    res = await fetch(`${agentBaseUrl()}/invoke`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // NDJSON + keepalive pings — Railway drops silent HTTP after ~5 minutes.
        Accept: "application/x-ndjson",
      },
      body: JSON.stringify({
        user_message: body.user_message,
        session_id: body.session_id ?? undefined,
        conversation: body.conversation ?? undefined,
        previous_itinerary: body.previous_itinerary ?? undefined,
        merged_itinerary:
          body.merged_itinerary ?? body.previous_itinerary ?? undefined,
        trip_constraints: body.trip_constraints ?? undefined,
      }),
      signal,
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") throw err;
    const msg = err instanceof Error ? err.message : String(err);
    if (/failed to fetch|networkerror|load failed/i.test(msg)) {
      throw new Error(
        "Lost connection to the agent while planning (often a long Overpass/LLM run). " +
          "Wait a moment and retry — CORS is usually fine if the mic/health check works."
      );
    }
    throw err;
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Agent error ${res.status}: ${text.slice(0, 200) || res.statusText}`
    );
  }
  return parseInvokeBody(res);
}

async function parseInvokeBody(res: Response): Promise<InvokeResponse> {
  const text = await res.text();
  const trimmed = text.trim();
  // NDJSON stream (pings + final result) — also handles older clients mistaking ctype.
  if (
    trimmed.includes('\n{"type"') ||
    trimmed.startsWith('{"type": "ping"') ||
    trimmed.startsWith('{"type":"ping"') ||
    trimmed.startsWith('{"type": "result"') ||
    trimmed.startsWith('{"type":"result"') ||
    trimmed.startsWith('{"type": "error"') ||
    trimmed.startsWith('{"type":"error"')
  ) {
    let lastError = "";
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      let obj: { type?: string; payload?: InvokeResponse; message?: unknown };
      try {
        obj = JSON.parse(line) as typeof obj;
      } catch {
        continue;
      }
      if (obj.type === "ping") continue;
      if (obj.type === "result" && obj.payload) {
        return obj.payload;
      }
      if (obj.type === "error") {
        lastError =
          typeof obj.message === "string"
            ? obj.message
            : JSON.stringify(obj.message ?? "Agent stream error");
      }
    }
    if (lastError) throw new Error(lastError);
    throw new Error("Agent stream ended without a result. Please retry.");
  }
  return JSON.parse(trimmed) as InvokeResponse;
}
