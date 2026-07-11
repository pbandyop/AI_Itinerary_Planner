"""Weather Adjustment MCP — Open-Meteo forecasts for Indian cities."""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any, Literal

import httpx

from agent.mcp.geo import city_center, resolve_city
from agent.schemas.specialists import DayWeather, WeatherAdjustment, WeatherResult

logger = logging.getLogger(__name__)

OPEN_METEO_URL = os.getenv(
    "OPEN_METEO_API_URL",
    "https://api.open-meteo.com/v1/forecast",
)

# WMO weather interpretation codes (subset)
WEATHER_LABELS: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _label_for_code(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WEATHER_LABELS.get(code, f"Code {code}")


def _rain_risk(precip_prob: float | None, precip_mm: float | None, code: int | None) -> Literal["low", "moderate", "high"]:
    prob = precip_prob or 0.0
    mm = precip_mm or 0.0
    if (code is not None and code >= 80) or prob >= 70 or mm >= 8:
        return "high"
    if (code is not None and code >= 51) or prob >= 40 or mm >= 2:
        return "moderate"
    return "low"


def _recommendation(risk: str, label: str) -> str:
    if risk == "high":
        return (
            f"{label}: high rain risk — prefer indoor museums/cafes for outdoor blocks; "
            "keep a covered backup for evening viewpoints."
        )
    if risk == "moderate":
        return (
            f"{label}: moderate rain chance — schedule outdoor forts earlier, "
            "add indoor buffer in the afternoon."
        )
    return f"{label}: low rain risk — outdoor heritage stops are fine."


def fetch_open_meteo(
    *,
    latitude: float,
    longitude: float,
    start: date,
    num_days: int,
    timeout: float = 30.0,
) -> dict[str, Any]:
    end = start + timedelta(days=max(0, num_days - 1))
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(
            [
                "weathercode",
                "precipitation_sum",
                "precipitation_probability_max",
                "temperature_2m_max",
                "temperature_2m_min",
            ]
        ),
        "timezone": "Asia/Kolkata",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    headers = {
        "User-Agent": "AI-Itinerary-Planner/0.2 (capstone; Open-Meteo client)",
        "Accept": "application/json",
    }
    logger.info("Open-Meteo request %s → %s", params, OPEN_METEO_URL)
    with httpx.Client(timeout=timeout, headers=headers) as client:
        resp = client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        return resp.json()


def _build_adjustments(days: list[DayWeather]) -> list[WeatherAdjustment]:
    adjustments: list[WeatherAdjustment] = []
    for i, day in enumerate(days, start=1):
        if day.rain_risk == "high":
            adjustments.append(
                WeatherAdjustment(
                    section=f"day{i}.afternoon",
                    action="prefer_indoor",
                    reason=day.recommendation or "High rain risk",
                )
            )
            adjustments.append(
                WeatherAdjustment(
                    section=f"day{i}.evening",
                    action="prefer_indoor",
                    reason="Evening outdoor viewpoints are poor bets in heavy rain.",
                )
            )
        elif day.rain_risk == "moderate":
            adjustments.append(
                WeatherAdjustment(
                    section=f"day{i}.afternoon",
                    action="add_buffer",
                    reason=day.recommendation or "Moderate rain chance",
                )
            )
            adjustments.append(
                WeatherAdjustment(
                    section=f"day{i}.morning",
                    action="keep",
                    reason="Keep outdoor morning plans; rain more likely later.",
                )
            )
        else:
            adjustments.append(
                WeatherAdjustment(
                    section=f"day{i}",
                    action="keep",
                    reason=day.recommendation or "Low rain risk",
                )
            )
    return adjustments


def weather_adjustment(
    *,
    city: str = "Jaipur",
    start_date: str | None = None,
    num_days: int = 3,
    latitude: float | None = None,
    longitude: float | None = None,
) -> WeatherResult:
    """MCP: fetch India-city forecast and propose indoor/outdoor adjustments."""
    info = resolve_city(city)
    if info is None:
        lat, lon = city_center(city)
        return WeatherResult(
            city=city,
            latitude=lat,
            longitude=lon,
            missing_data=True,
            notes=(
                f"City {city!r} is not in the India catalog "
                "(data/india_cities.json). Weather unavailable."
            ),
        )

    canonical = info.name
    lat = latitude if latitude is not None else info.lat
    lon = longitude if longitude is not None else info.lon
    num_days = max(2, min(4, num_days))

    try:
        start = date.fromisoformat(start_date) if start_date else date.today()
    except ValueError:
        return WeatherResult(
            city=canonical,
            latitude=lat,
            longitude=lon,
            missing_data=True,
            notes=f"Invalid start_date={start_date!r}; expected YYYY-MM-DD.",
        )

    try:
        payload = fetch_open_meteo(
            latitude=lat,
            longitude=lon,
            start=start,
            num_days=num_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Open-Meteo weather fetch failed")
        return WeatherResult(
            city=canonical,
            latitude=lat,
            longitude=lon,
            missing_data=True,
            notes=f"Open-Meteo unavailable ({exc.__class__.__name__}: {exc}). Weather data is missing.",
        )

    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return WeatherResult(
            city=canonical,
            latitude=lat,
            longitude=lon,
            missing_data=True,
            notes="Open-Meteo returned no daily forecast rows — data missing.",
        )

    codes = daily.get("weathercode") or [None] * len(times)
    precip = daily.get("precipitation_sum") or [None] * len(times)
    probs = daily.get("precipitation_probability_max") or [None] * len(times)
    tmax = daily.get("temperature_2m_max") or [None] * len(times)
    tmin = daily.get("temperature_2m_min") or [None] * len(times)

    day_rows: list[DayWeather] = []
    for i, day_str in enumerate(times[:num_days]):
        code = codes[i] if i < len(codes) else None
        mm = precip[i] if i < len(precip) else None
        prob = probs[i] if i < len(probs) else None
        label = _label_for_code(int(code) if code is not None else None)
        risk = _rain_risk(
            float(prob) if prob is not None else None,
            float(mm) if mm is not None else None,
            int(code) if code is not None else None,
        )
        day_rows.append(
            DayWeather(
                calendar_date=day_str,
                weather_code=int(code) if code is not None else None,
                weather_label=label,
                precip_probability_max=float(prob) if prob is not None else None,
                precip_mm_sum=float(mm) if mm is not None else None,
                temp_max_c=float(tmax[i]) if i < len(tmax) and tmax[i] is not None else None,
                temp_min_c=float(tmin[i]) if i < len(tmin) and tmin[i] is not None else None,
                rain_risk=risk,
                recommendation=_recommendation(risk, label),
            )
        )

    adjustments = _build_adjustments(day_rows)
    logger.info(
        "weather_mcp city=%s: %d days, risks=%s",
        canonical,
        len(day_rows),
        [d.rain_risk for d in day_rows],
    )
    return WeatherResult(
        city=canonical,
        latitude=lat,
        longitude=lon,
        days=day_rows,
        adjustments=adjustments,
        missing_data=False,
        notes=(
            f"Forecast for {canonical}, India from Open-Meteo (no API key). "
            "Use adjustments for 'what if it rains?' answers."
        ),
        source="Open-Meteo",
    )
