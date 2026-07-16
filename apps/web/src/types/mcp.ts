/** Travel time + POI MCP envelopes (agent specialist results). */

export interface TravelLeg {
  from_name: string;
  to_name: string;
  from_osm?: string | null;
  to_osm?: string | null;
  distance_km?: number | null;
  duration_min: number;
  mode?: "walk" | "city";
  method?: string;
}

export interface TravelTimeResult {
  legs: TravelLeg[];
  total_duration_min: number;
  missing_data?: boolean;
  notes?: string | null;
}

export interface DayWeather {
  calendar_date: string;
  weather_code?: number | null;
  weather_label?: string | null;
  precip_probability_max?: number | null;
  precip_mm_sum?: number | null;
  temp_max_c?: number | null;
  temp_min_c?: number | null;
  rain_risk?: "low" | "moderate" | "high";
  recommendation?: string | null;
}

export interface WeatherResult {
  city: string;
  days: DayWeather[];
  missing_data?: boolean;
  notes?: string | null;
  source?: string;
}

export interface POICandidate {
  name: string;
  osm_type: string;
  osm_id: number;
  lat?: number | null;
  lon?: number | null;
  category?: string | null;
  rank_score?: number | null;
  matched_interests?: string[];
}

export interface POISearchResult {
  city: string;
  country?: string;
  query_interests: string[];
  query_constraints?: string[];
  pois: POICandidate[];
  missing_data?: boolean;
  notes?: string | null;
}
