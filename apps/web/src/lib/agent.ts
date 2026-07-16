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
 * Prefer same-origin `/api/agent` (Next rewrite → agent) to avoid CORS.
 * Fall back to NEXT_PUBLIC_AGENT_BASE_URL for direct calls.
 */
export function agentBaseUrl(): string {
  if (typeof window !== "undefined") {
    return "/api/agent";
  }
  return (
    process.env.NEXT_PUBLIC_AGENT_BASE_URL?.replace(/\/$/, "") ||
    "http://localhost:8000"
  );
}

export async function invokeAgent(
  body: InvokeRequest,
  signal?: AbortSignal
): Promise<InvokeResponse> {
  let res: Response;
  try {
    res = await fetch(`${agentBaseUrl()}/invoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    const msg = err instanceof Error ? err.message : String(err);
    if (/failed to fetch|networkerror|load failed/i.test(msg)) {
      throw new Error(
        "Could not reach the agent (network/CORS). Is it running on port 8000? " +
          "Try refreshing after `python -m agent.main --serve`."
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
  return (await res.json()) as InvokeResponse;
}
