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
  city: "Jaipur";
  num_days: 2 | 3 | 4;
  start_date?: string | null;
  end_date?: string | null;
  interests: string[];
  pace: Pace;
  constraints: string[];
  daily_time_window_min: number;
  confirmed: boolean;
}

export interface Itinerary {
  schema_version: "1.0";
  trip: TripConstraints;
  days: DayPlan[];
  sources: Source[];
  summary?: string | null;
  uncertainty_notes: string[];
}

export type ReviewStatus = "approve" | "revise";

export interface ReviewIssue {
  code: string;
  message: string;
  section?: string | null;
}

export interface ReviewerVerdict {
  status: ReviewStatus;
  issues: ReviewIssue[];
  affected_sections: string[];
  notes?: string | null;
}
