/** Build itinerary HTML matching the web UI (for n8n PDF). */

type StopLike = {
  name?: string;
  category?: string | null;
  duration_min?: number;
  arrive_time?: string | null;
  depart_time?: string | null;
  travel_to_next_min?: number | null;
  travel_to_next_km?: number | null;
  travel_to_next_mode?: string | null;
};

type BlockLike = {
  stops?: StopLike[];
  notes?: string | null;
};

type DayLike = {
  day_index?: number;
  theme?: string | null;
  morning?: BlockLike;
  afternoon?: BlockLike;
  evening?: BlockLike;
};

export type ItineraryLike = {
  trip?: {
    city?: string;
    num_days?: number | null;
    pace?: string | null;
    interests?: string[];
  };
  days?: DayLike[];
  summary?: string | null;
};

type SourceLike = {
  title?: string;
  url?: string | null;
};

function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatClockAmPm(hhmm?: string | null): string | null {
  if (!hhmm || !/^\d{1,2}:\d{2}$/.test(hhmm)) return null;
  const [hRaw, mRaw] = hhmm.split(":");
  const hour = Number(hRaw);
  const minute = Number(mRaw);
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return null;
  const suffix = hour < 12 ? "AM" : "PM";
  const hour12 = hour % 12 || 12;
  return `${hour12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

function spendDurationLabel(minutes?: number): string {
  if (!minutes || minutes <= 0) return "Spend a short time in this place";
  const hours = Math.round(minutes / 60);
  if (hours <= 0) return `Spend about ${minutes} minutes in this place`;
  if (hours === 1) return "Spend about 1 hour in this place";
  return `Spend about ${hours} hours in this place`;
}

function travelHint(stop: StopLike): string {
  const mins = stop.travel_to_next_min;
  if (mins == null || mins <= 0) return "";
  const km =
    stop.travel_to_next_km != null && stop.travel_to_next_km >= 0.05
      ? stop.travel_to_next_km
      : null;
  const mode = stop.travel_to_next_mode || "";
  if (km != null) {
    const walk = Math.max(5, Math.round((km / 4.5) * 60 + 3));
    const car = Math.max(8, Math.round((km / 18.0) * 60 + 5));
    return `<div class="travel">Travel to next: Walk ~${walk} min (${km.toFixed(1)} km) · Car ~${car} min</div>`;
  }
  const modeBit = mode ? ` by ${mode}` : "";
  return `<div class="travel">Travel to next: about ${mins} minutes${modeBit}</div>`;
}

export function buildItineraryHtml(opts: {
  city?: string;
  summary?: string | null;
  itinerary: ItineraryLike;
  sources?: SourceLike[];
}): string {
  const itin = opts.itinerary || {};
  const days = Array.isArray(itin.days) ? itin.days : [];
  const blocks: Array<[keyof DayLike, string]> = [
    ["morning", "Morning"],
    ["afternoon", "Afternoon"],
    ["evening", "Evening"],
  ];

  let html = `<!DOCTYPE html><html><head><meta charset="utf-8"/>
<style>
  body { font-family: Georgia, "Times New Roman", serif; color: #1a1a1a; line-height: 1.45; max-width: 720px; margin: 24px auto; padding: 0 16px; }
  h1 { font-size: 1.75rem; margin: 0 0 0.35rem; }
  .meta { color: #555; margin: 0 0 1.25rem; font-size: 0.95rem; }
  h2 { font-size: 1.25rem; margin: 1.5rem 0 0.5rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }
  h3 { font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; color: #666; margin: 1rem 0 0.5rem; }
  ol { padding-left: 0; list-style: none; margin: 0; }
  li.stop { margin: 0 0 0.85rem; padding: 0.65rem 0.75rem; background: #f7f5f2; border-radius: 6px; }
  .row { display: flex; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }
  .clock { font-weight: 700; color: #0b6e4f; min-width: 4.5rem; }
  .name { font-weight: 650; }
  .tag { font-size: 0.75rem; color: #0b6e4f; background: #e6f4ef; padding: 0.1rem 0.45rem; border-radius: 4px; margin-left: 0.35rem; }
  .dur { font-size: 0.85rem; color: #555; }
  .travel { margin-top: 0.4rem; font-size: 0.85rem; color: #444; }
  .note { margin: 0.4rem 0 0.75rem; padding: 0.5rem 0.65rem; font-size: 0.9rem; color: #444;
    border-left: 3px solid #0b6e4f; background: #eef8f4; }
  .sources { font-size: 0.85rem; }
</style></head><body>`;

  const city = opts.city || itin.trip?.city || "Jaipur";
  html += `<h1>${esc(city)} itinerary</h1>`;
  const interests = itin.trip?.interests || [];
  const pace = itin.trip?.pace || "";
  const paceLabel = pace === "moderate" ? "balanced" : pace;
  const daysN = itin.trip?.num_days || days.length;
  html += `<p class="meta">${esc(daysN)}-day ${esc(paceLabel)} plan`;
  if (interests.length) html += ` · ${esc(interests.join(", "))}`;
  html += `</p>`;
  if (opts.summary) html += `<p class="meta">${esc(opts.summary)}</p>`;

  for (const day of days) {
    // Prefer theme from placed stops (categories), not stale day.theme.
    const placedCats: string[] = [];
    for (const key of ["morning", "afternoon", "evening"] as const) {
      for (const s of day[key]?.stops || []) {
        if (s.category) placedCats.push(String(s.category));
      }
    }
    const themeFromStops = [...new Set(placedCats)].join(", ");
    const theme = themeFromStops || day.theme || "";
    html += `<h2>Day ${esc(day.day_index)}${theme ? ` — ${esc(theme)}` : ""}</h2>`;
    for (const [key, label] of blocks) {
      const block = (day[key] as BlockLike | undefined) || {};
      const stops = Array.isArray(block.stops) ? block.stops : [];
      const blockNotes = (block.notes || "").trim();
      if (!stops.length && !blockNotes) continue;
      html += `<h3>${label}</h3>`;
      if (stops.length) {
        html += `<ol>`;
        for (const s of stops) {
          const arrive = formatClockAmPm(s.arrive_time);
          const depart = formatClockAmPm(s.depart_time);
          const cat = (s.category || "").trim();
          let dur = spendDurationLabel(s.duration_min);
          if (depart) dur += ` · until ${depart}`;
          html += `<li class="stop"><div class="row"><div>`;
          if (arrive) html += `<span class="clock">${esc(arrive)}</span> `;
          html += `<span class="name">${esc(s.name)}</span>`;
          if (cat) html += `<span class="tag">${esc(cat)}</span>`;
          html += `</div><div class="dur">${esc(dur)}</div></div>`;
          html += travelHint(s);
          html += `</li>`;
        }
        html += `</ol>`;
      }
      if (blockNotes) html += `<p class="note">${esc(blockNotes)}</p>`;
    }
  }

  const sources = opts.sources || [];
  if (sources.length) {
    html += `<h2>Sources</h2><ul class="sources">`;
    for (const s of sources) {
      html += `<li>${esc(s.title || "Source")}${s.url ? ` — ${esc(s.url)}` : ""}</li>`;
    }
    html += `</ul>`;
  }
  html += `</body></html>`;
  return html;
}
