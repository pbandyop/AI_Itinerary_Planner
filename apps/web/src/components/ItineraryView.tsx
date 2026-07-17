"use client";

import type { Itinerary, Source, TimeBlock } from "@/types/itinerary";
import type { DayWeather, TravelTimeResult, WeatherResult } from "@/types/mcp";
import SourceLink from "./SourceLink";
import EmailItineraryForm from "./EmailItineraryForm";
import styles from "./itinerary-view.module.css";

const BLOCKS: {
  key: keyof Pick<
    Itinerary["days"][0],
    "morning" | "afternoon" | "evening"
  >;
  label: string;
}[] = [
  { key: "morning", label: "Morning" },
  { key: "afternoon", label: "Afternoon" },
  { key: "evening", label: "Evening" },
];

function blockStops(block: TimeBlock) {
  return block?.stops ?? [];
}

function spendDurationLabel(minutes: number): string {
  if (!minutes || minutes <= 0) return "spend a short time in this place";
  const hours = Math.round(minutes / 60);
  if (hours <= 0) return `spend about ${minutes} minutes in this place`;
  if (hours === 1) return "spend about 1 hour in this place";
  return `spend about ${hours} hours in this place`;
}

/** Convert builder HH:MM (24h) to a short 12-hour label. */
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

/** Match agent geo heuristics so both modes can be shown from distance. */
function minutesForMode(km: number, mode: "walk" | "car"): number {
  if (km < 0.05) return 0;
  if (mode === "walk") return Math.max(5, Math.round((km / 4.5) * 60 + 3));
  return Math.max(8, Math.round((km / 18.0) * 60 + 5));
}

function normalizeMode(
  mode?: string | null,
): "walk" | "car" | "bus" | null {
  if (mode === "walk") return "walk";
  if (mode === "city" || mode === "car") return "car";
  if (mode === "bus") return "bus";
  return null;
}

type ModeTravel = { mode: "walk" | "car" | "bus"; minutes: number; km?: number };

function travelModesForStop(stop: {
  travel_to_next_min?: number | null;
  travel_to_next_km?: number | null;
  travel_to_next_mode?: "walk" | "car" | "bus" | null;
}): ModeTravel[] {
  const km =
    stop.travel_to_next_km != null && stop.travel_to_next_km >= 0.05
      ? stop.travel_to_next_km
      : null;

  // When distance is known, show both walk and car estimates.
  if (km != null) {
    return [
      { mode: "walk", minutes: minutesForMode(km, "walk"), km },
      { mode: "car", minutes: minutesForMode(km, "car"), km },
    ];
  }

  if (stop.travel_to_next_min == null || stop.travel_to_next_min <= 0) {
    return [];
  }
  const mode = normalizeMode(stop.travel_to_next_mode) ?? "car";
  return [{ mode, minutes: stop.travel_to_next_min }];
}

function formatModeLine(entry: ModeTravel): string {
  const kmBit =
    entry.km != null && entry.km >= 0.05
      ? ` (${entry.km.toFixed(1)} km)`
      : "";
  const label =
    entry.mode === "walk" ? "Walk" : entry.mode === "bus" ? "Bus" : "Car";
  return `${label}: about ${entry.minutes} minutes${kmBit}`;
}

function legModeLabel(mode?: string | null): string | null {
  return normalizeMode(mode);
}

function dayWeatherLabel(w?: DayWeather | null): string | null {
  if (!w) return null;
  const bits: string[] = [];
  if (w.weather_label) bits.push(w.weather_label);
  if (w.rain_risk) bits.push(`rain risk ${w.rain_risk}`);
  if (w.precip_probability_max != null) {
    bits.push(`~${Math.round(w.precip_probability_max)}% rain`);
  }
  if (w.temp_min_c != null && w.temp_max_c != null) {
    bits.push(`${Math.round(w.temp_min_c)}–${Math.round(w.temp_max_c)}°C`);
  }
  return bits.length ? bits.join(" · ") : null;
}

function categoryLabel(category?: string | null): string | null {
  const key = (category || "").trim().toLowerCase();
  if (!key) return null;
  const labels: Record<string, string> = {
    food: "Food",
    heritage: "Heritage",
    museum: "Museum",
    temple: "Temple",
    park: "Park",
    garden: "Garden",
    market: "Market",
    shopping: "Shopping",
    nightlife: "Nightlife",
    viewpoint: "Viewpoint",
    art: "Art",
    attraction: "Attraction",
  };
  return (
    labels[key] ||
    key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

function formatWeatherDate(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso.includes("T") ? iso : `${iso}T12:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export default function ItineraryView({
  itinerary,
  travel,
  weather,
  sources,
}: {
  itinerary: Itinerary;
  travel?: TravelTimeResult | null;
  weather?: WeatherResult | null;
  sources?: Source[];
}) {
  const pace = itinerary.trip.pace;
  const paceLabel =
    pace === "moderate" ? "balanced" : pace ?? "—";

  const realLegs = (travel?.legs || []).filter((leg) => {
    const same =
      (leg.from_name || "").trim().toLowerCase() ===
      (leg.to_name || "").trim().toLowerCase();
    const zero =
      leg.distance_km != null && leg.distance_km < 0.05;
    return !same && !zero && (leg.duration_min || 0) > 0;
  });

  const weatherByDay = new Map<number, DayWeather>();
  (weather?.days || []).forEach((w, i) => {
    weatherByDay.set(i + 1, w);
  });

  return (
    <section className={styles.wrap} aria-label="Itinerary">
      <header className={styles.header}>
        <h2>
          {itinerary.trip.num_days ?? "?"}‑day {paceLabel} plan for{" "}
          {itinerary.trip.city}
        </h2>
        <p className={styles.sub}>
          {itinerary.trip.interests?.length
            ? `Focus: ${itinerary.trip.interests.join(", ")}`
            : "Your customized day plan"}
          . Each day has morning and afternoon. Say what you’d like to change.
        </p>
      </header>

      <div className={styles.days}>
        {itinerary.days.map((day) => {
          const flat = [
            ...blockStops(day.morning),
            ...blockStops(day.afternoon),
            ...blockStops(day.evening),
          ];
          const lastKey = flat.length
            ? `${flat[flat.length - 1].osm_type}/${flat[flat.length - 1].osm_id}`
            : null;
          const dayWx = weatherByDay.get(day.day_index);
          const predicted = dayWeatherLabel(dayWx);
          const weatherDate = formatWeatherDate(dayWx?.calendar_date);
          return (
            <article key={day.day_index} className={styles.day}>
              <div className={styles.dayHeading}>
                <h3>Day {day.day_index}</h3>
                {predicted || weatherDate ? (
                  <div className={styles.weatherBlock}>
                    {predicted ? (
                      <p className={styles.dayWeather}>
                        Predicted weather: {predicted}
                      </p>
                    ) : null}
                    {weatherDate ? (
                      <p className={styles.weatherDate}>{weatherDate}</p>
                    ) : null}
                    <p className={styles.weatherSource}>
                      <SourceLink
                        url={weather?.source}
                        datasetOrName={weather?.source || "open-meteo"}
                      />
                    </p>
                  </div>
                ) : null}
              </div>
              {BLOCKS.map(({ key, label }) => {
                const block = day[key];
                const stops = blockStops(block);
                const blockNotes = (block?.notes || "").trim();
                if (!stops.length && !blockNotes) return null;
                return (
                  <div key={key} className={styles.block}>
                    <h4>{label}</h4>
                    {stops.length ? (
                    <ol className={styles.stopList}>
                      {stops.map((stop, i) => {
                        const stopKey = `${stop.osm_type}/${stop.osm_id}`;
                        const modes =
                          stopKey !== lastKey ? travelModesForStop(stop) : [];
                        const category = categoryLabel(stop.category);
                        return (
                          <li key={`${stopKey}-${i}`}>
                            <div className={styles.stopCard}>
                              <div className={styles.stopRow}>
                                <div className={styles.stopMain}>
                                  {formatClockAmPm(stop.arrive_time) ? (
                                    <span className={styles.stopClock}>
                                      {formatClockAmPm(stop.arrive_time)}
                                    </span>
                                  ) : null}
                                  <span className={styles.stopName}>
                                    {stop.name}
                                  </span>
                                  {category ? (
                                    <span className={styles.categoryTag}>
                                      {category}
                                    </span>
                                  ) : null}
                                </div>
                                <span className={styles.stopDuration}>
                                  {spendDurationLabel(stop.duration_min)}
                                  {formatClockAmPm(stop.depart_time)
                                    ? ` · until ${formatClockAmPm(stop.depart_time)}`
                                    : ""}
                                </span>
                              </div>
                              {modes.length > 0 ? (
                                <div className={styles.travelHint}>
                                  <span className={styles.travelLead}>
                                    Travel to next destination:
                                  </span>
                                  {modes.length === 1 ? (
                                    <span>
                                      {" "}
                                      about {modes[0].minutes} minutes
                                      {modes[0].km != null
                                        ? ` (${modes[0].km.toFixed(1)} km by ${modes[0].mode})`
                                        : modes[0].mode
                                          ? ` by ${modes[0].mode}`
                                          : ""}
                                    </span>
                                  ) : (
                                    <ul className={styles.modeList}>
                                      {modes.map((m) => (
                                        <li key={m.mode}>{formatModeLine(m)}</li>
                                      ))}
                                    </ul>
                                  )}
                                </div>
                              ) : null}
                            </div>
                          </li>
                        );
                      })}
                    </ol>
                    ) : null}
                    {blockNotes ? (
                      <p className={styles.blockNote}>{blockNotes}</p>
                    ) : null}
                  </div>
                );
              })}
            </article>
          );
        })}
      </div>

      <aside className={styles.travelPanel} aria-label="Travel times">
        <h3>Travel between stops</h3>
        {realLegs.length ? (
          <>
            <ul className={styles.legList}>
              {realLegs.slice(0, 14).map((leg, i) => {
                const mode = legModeLabel(leg.mode);
                const extras = [
                  leg.distance_km != null
                    ? `${leg.distance_km.toFixed(1)} km`
                    : null,
                  mode ? `by ${mode}` : null,
                ].filter(Boolean);
                return (
                  <li key={`${leg.from_name}-${leg.to_name}-${i}`}>
                    <span>
                      {leg.from_name} → {leg.to_name}
                    </span>
                    <span>
                      about {leg.duration_min} min
                      {extras.length ? ` · ${extras.join(" ")}` : ""}
                    </span>
                  </li>
                );
              })}
            </ul>
            <p className={styles.travelTotal}>
              Total travel time: about{" "}
              {realLegs.reduce((n, l) => n + (l.duration_min || 0), 0)} minutes
            </p>
          </>
        ) : (
          <p className={styles.empty}>
            {travel?.missing_data
              ? "Travel time data not available."
              : "Travel times appear after planning."}
          </p>
        )}
      </aside>

      <EmailItineraryForm itinerary={itinerary} sources={sources} />
    </section>
  );
}
