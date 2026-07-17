import { NextResponse } from "next/server";
import {
  buildItineraryHtml,
  type ItineraryLike,
} from "@/lib/itineraryHtml";

export const runtime = "nodejs";
export const maxDuration = 60;

type EmailBody = {
  email?: string;
  itinerary?: ItineraryLike | null;
  sources?: unknown;
  summary?: string | null;
};

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function webhookUrl(): string | null {
  let raw = (process.env.N8N_WEBHOOK_URL || "").trim();
  // Common Vercel mistake: paste "N8N_WEBHOOK_URL=https://..." as the value.
  if (raw.toUpperCase().startsWith("N8N_WEBHOOK_URL=")) {
    raw = raw.slice("N8N_WEBHOOK_URL=".length).trim();
  }
  raw = raw.replace(/^["']|["']$/g, "").trim();
  if (!raw) return null;
  try {
    // Validate early so we fail with a clear message instead of fetch() TypeError.
    // eslint-disable-next-line no-new
    new URL(raw);
  } catch {
    return null;
  }
  return raw;
}

export async function POST(req: Request) {
  const url = webhookUrl();
  if (!url) {
    return NextResponse.json(
      {
        ok: false,
        error:
          "N8N_WEBHOOK_URL is missing or invalid. In Vercel, set the value to only the https://…/webhook/… URL (do not include N8N_WEBHOOK_URL=).",
      },
      { status: 503 },
    );
  }

  let body: EmailBody;
  try {
    body = (await req.json()) as EmailBody;
  } catch {
    return NextResponse.json(
      { ok: false, error: "Invalid JSON body." },
      { status: 400 },
    );
  }

  const email = String(body.email || "")
    .trim()
    .toLowerCase();
  if (!EMAIL_RE.test(email)) {
    return NextResponse.json(
      { ok: false, error: "Please enter a valid email address." },
      { status: 400 },
    );
  }

  const itinerary = body.itinerary;
  if (!itinerary || !Array.isArray(itinerary.days) || itinerary.days.length < 1) {
    return NextResponse.json(
      { ok: false, error: "No itinerary to send. Generate a plan first." },
      { status: 400 },
    );
  }

  const sources =
    (body.sources as { title?: string; url?: string | null }[]) ?? [];
  const summary = body.summary ?? itinerary.summary ?? null;
  const city = itinerary.trip?.city || "Jaipur";
  // Pre-build UI-matching HTML so n8n does not need a custom formatter.
  const html = buildItineraryHtml({
    city,
    summary,
    itinerary,
    sources,
  });

  const payload = {
    email,
    city,
    summary,
    itinerary,
    sources,
    html,
    sent_at: new Date().toISOString(),
  };

  try {
    const upstream = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(55_000),
    });

    const text = await upstream.text();
    let upstreamJson: unknown = null;
    try {
      upstreamJson = text ? JSON.parse(text) : null;
    } catch {
      upstreamJson = text || null;
    }

    if (!upstream.ok) {
      const n8nMsg =
        typeof upstreamJson === "object" &&
        upstreamJson &&
        "message" in upstreamJson &&
        typeof (upstreamJson as { message: unknown }).message === "string"
          ? (upstreamJson as { message: string }).message
          : null;
      let hint: string;
      if (upstream.status === 404) {
        hint =
          "Webhook not found. Use the production /webhook/<id> URL (not /webhook-test/) and ensure the workflow is Active with HTTP Method POST.";
      } else if (upstream.status >= 500) {
        hint =
          "Webhook was reached, but a node inside the n8n workflow failed. Open n8n → Executions → latest failed run and fix the red node (often Code path, PDF, or Gmail credentials).";
      } else {
        hint = `n8n returned HTTP ${upstream.status}.`;
      }
      return NextResponse.json(
        {
          ok: false,
          error: n8nMsg ? `${hint} n8n: ${n8nMsg}` : hint,
          detail: upstreamJson,
        },
        { status: 502 },
      );
    }

    return NextResponse.json({
      ok: true,
      message: "Itinerary sent — check your inbox shortly.",
      n8n: upstreamJson,
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Failed to reach n8n webhook.";
    return NextResponse.json(
      {
        ok: false,
        error: `Could not reach n8n: ${message}`,
      },
      { status: 502 },
    );
  }
}
