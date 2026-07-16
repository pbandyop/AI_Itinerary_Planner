/** Shared itinerary types mirroring services/agent schemas (Phase 1). */

export type Pace = "relaxed" | "moderate" | "packed";
export type TimeOfDay = "morning" | "afternoon" | "evening";
export type OsmType = "node" | "way" | "relation";
export type Dataset =
  | "openstreetmap"
  | "wikivoyage"
  | "wikipedia"
  | "open-meteo"
  | "other";

export interface Source {
  title: string;
  url?: string | null;
  dataset: Dataset;
  snippet?: string | null;
  source_id?: string | null;
}

export interface Stop {
  name: string;
  osm_type: OsmType;
  osm_id: number;
  lat?: number | null;
  lon?: number | null;
  category?: string | null;
  duration_min: number;
  travel_to_next_min?: number | null;
  travel_to_next_km?: number | null;
  travel_to_next_mode?: "walk" | "car" | "bus" | null;
  /** Estimated arrival HH:MM (24h), stamped by itinerary builder. */
  arrive_time?: string | null;
  /** Estimated departure HH:MM (24h) = arrive + duration. */
  depart_time?: string | null;
  reason: string;
  citations: Source[];
  uncertainty?: string | null;
}

export interface TimeBlock {
  time_of_day: TimeOfDay;
  stops: Stop[];
  notes?: string | null;
}

export interface DayPlan {
  day_index: number;
  calendar_date?: string | null;
  theme?: string | null;
  morning: TimeBlock;
  afternoon: TimeBlock;
  evening: TimeBlock;
}

export interface TripConstraints {
  city: string; // Currently scoped to Jaipur
  country: "India";
  /** Set once the user states it; null while clarifying. */
  num_days?: 2 | 3 | 4 | null;
  start_date?: string | null;
  end_date?: string | null;
  interests: string[];
  /** Required before generation; null until the user states it. */
  pace?: Pace | null;
  traveler_profile?: string | null;
  constraints: string[];
  daily_time_window_min: number;
  confirmed: boolean;
  clarify_turns?: number;
  days_known?: boolean;
  pace_known?: boolean;
  interests_known?: boolean;
  dates_known?: boolean;
}

export interface Itinerary {
  schema_version: "1.0";
  trip: TripConstraints;
  days: DayPlan[];
  sources: Source[];
  summary?: string | null;
  uncertainty_notes: string[];
  /** Merger synthesis decisions (weather / travel / knowledge conflict resolution). */
  reasoning?: string[];
}

export type ReviewStatus = "approve" | "revise";

export interface ReviewIssue {
  code: string;
  message: string;
  section?: string | null;
}

export interface ReviewerVerdict {
  status: ReviewStatus;
  reason?: string | null;
  target_agent?: string | null;
  constraints?: string[];
  issues: ReviewIssue[];
  affected_sections: string[];
  notes?: string | null;
}
