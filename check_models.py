#!/usr/bin/env python3
"""Fetch today's HRRR, NAM, and GFS forecasts for a point and push a
plain-language summary of their agreement (or disagreement) to ntfy.

Config via env vars:
  NTFY_TOPIC           required  ntfy topic to publish to
  LAT, LON             required  point of interest (CONUS for HRRR/NAM)
  LOCATION_NAME        optional  human label shown in the notification
  TEMP_TOLERANCE_F     optional  max allowed spread of daily high/low (F)
  PRECIP_TOLERANCE_IN  optional  max allowed spread of daily precip total (in)
  NTFY_SERVER          optional  override for self-hosted ntfy (default ntfy.sh)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MODELS = ("ncep_hrrr_conus", "ncep_nam_conus", "gfs_seamless")
MODEL_LABEL = {
    "ncep_hrrr_conus": "HRRR",
    "ncep_nam_conus": "NAM ",
    "gfs_seamless": "GFS ",
}

# An hour counts as "raining" above this threshold.
HOURLY_RAIN_THRESHOLD_IN = 0.01
# A day counts as "rainy" above this threshold (total precip).
DAILY_RAIN_THRESHOLD_IN = 0.05
DEFAULT_TEMP_TOL_F = 3.0
DEFAULT_PRECIP_TOL_IN = 0.1


@dataclass
class DayForecast:
    high_f: float
    low_f: float
    precip_in: float
    hours: list[int] = field(default_factory=list)        # local hour of day (0-23)
    hourly_precip: list[float] = field(default_factory=list)
    hourly_pop: list[int | None] = field(default_factory=list)

    @property
    def has_rain(self) -> bool:
        return self.precip_in >= DAILY_RAIN_THRESHOLD_IN

    @property
    def max_pop(self) -> int | None:
        vals = [p for p in self.hourly_pop if p is not None]
        return max(vals) if vals else None


def http_get_json(url: str, params: dict) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=30) as r:
        return json.loads(r.read())


def fetch(lat: float, lon: float, model: str) -> DayForecast:
    data = http_get_json(
        OPEN_METEO_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "models": model,
            "hourly": "temperature_2m,precipitation,precipitation_probability",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "timezone": "auto",
            "forecast_days": 1,
            "past_days": 0,
        },
    )
    h = data["hourly"]
    times = h["time"]
    temps = [t for t in h["temperature_2m"] if t is not None]
    precs = [p if p is not None else 0.0 for p in h["precipitation"]]
    pops = h.get("precipitation_probability") or [None] * len(times)
    if not temps:
        raise RuntimeError(f"{model}: no hourly temperature data returned")
    hours = [datetime.fromisoformat(t).hour for t in times]
    return DayForecast(
        high_f=max(temps),
        low_f=min(temps),
        precip_in=sum(precs),
        hours=hours,
        hourly_precip=precs,
        hourly_pop=pops,
    )


def consensus_rain_hours(forecasts: dict[str, DayForecast]) -> list[int]:
    """Hours (0-23) where at least half the models expect rain."""
    if not forecasts:
        return []
    threshold = (len(forecasts) + 1) // 2  # majority, rounding up
    by_hour: dict[int, int] = {}
    for fc in forecasts.values():
        for hr, p in zip(fc.hours, fc.hourly_precip):
            if p >= HOURLY_RAIN_THRESHOLD_IN:
                by_hour[hr] = by_hour.get(hr, 0) + 1
    return sorted(h for h, n in by_hour.items() if n >= threshold)


def fmt_hour_12h(h: int) -> str:
    suffix = "AM" if h < 12 else "PM"
    hr12 = 12 if h % 12 == 0 else h % 12
    return f"{hr12}{suffix}"


def fmt_rain_window(hours: list[int]) -> str:
    if not hours:
        return ""
    if len(hours) >= 20:
        return "all day"
    hours = sorted(hours)
    runs: list[tuple[int, int]] = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            runs.append((start, prev))
            start = prev = h
    runs.append((start, prev))
    # Display: hour H means rain "during the hour starting at H",
    # so a run [14, 15, 16] renders as "2PM-5PM" (end is last+1).
    return " & ".join(f"{fmt_hour_12h(s)}-{fmt_hour_12h((e + 1) % 24)}" for s, e in runs)


def pick_bottom_line(
    mean_precip_in: float,
    precip_spread_in: float,
    categorical_agree: bool,
    precip_tol_in: float,
) -> str:
    if mean_precip_in < DAILY_RAIN_THRESHOLD_IN:
        return "Dry day. Skip the umbrella."
    if mean_precip_in < 0.25:
        base = "Light rain - grab an umbrella."
    elif mean_precip_in < 0.75:
        base = "Real rain - plan for it."
    elif mean_precip_in < 2.0:
        base = "Heavy rain - reschedule outdoor stuff."
    else:
        base = "Major rain - stay in if you can."
    if not categorical_agree:
        base += " Models split on whether it'll rain at all."
    elif precip_spread_in > precip_tol_in:
        base += " Models differ on amount - hedge high."
    return base


def build_notification(
    forecasts: dict[str, DayForecast],
    failures: list[str],
    location: str,
    temp_tol: float,
    precip_tol: float,
) -> tuple[str, str, list[str], str]:
    highs = [f.high_f for f in forecasts.values()]
    lows = [f.low_f for f in forecasts.values()]
    precips = [f.precip_in for f in forecasts.values()]
    rains = [f.has_rain for f in forecasts.values()]

    high_spread = max(highs) - min(highs)
    low_spread = max(lows) - min(lows)
    precip_spread = max(precips) - min(precips)
    mean_precip = sum(precips) / len(precips)
    mean_high = sum(highs) / len(highs)
    mean_low = sum(lows) / len(lows)

    temp_aligned = max(high_spread, low_spread) <= temp_tol
    categorical_aligned = len(set(rains)) == 1
    magnitude_aligned = precip_spread <= precip_tol
    precip_aligned = categorical_aligned and magnitude_aligned
    any_rain = any(rains)

    # Title - ASCII-safe enough, but we POST via JSON so unicode is fine too.
    if any_rain:
        if mean_precip < 0.25:
            rain_word = "light rain"
        elif mean_precip < 0.75:
            rain_word = "rain"
        else:
            rain_word = "heavy rain"
        title = f"{location} today: {rain_word}, {mean_high:.0f}°F"
    else:
        title = f"{location} today: clear, {mean_high:.0f}°F"

    # Body.
    lines: list[str] = []
    if temp_aligned:
        max_spread = max(high_spread, low_spread)
        qualifier = "all match" if max_spread < 0.5 else f"agree within {max_spread:.0f}°F"
        lines.append(
            f"High {mean_high:.0f}°F · Low {mean_low:.0f}°F ({qualifier})"
        )
    else:
        lines.append(
            f"High {min(highs):.0f}-{max(highs):.0f}°F · "
            f"Low {min(lows):.0f}-{max(lows):.0f}°F "
            f"(spread {high_spread:.0f}°F - models disagree)"
        )

    rain_hours = consensus_rain_hours(forecasts)
    window = fmt_rain_window(rain_hours)
    pops = [f.max_pop for f in forecasts.values() if f.max_pop is not None]
    peak_pop = max(pops) if pops else None

    if any_rain and window:
        extra = f" · peak chance {peak_pop}%" if peak_pop is not None else ""
        lines.append(f"Rain {window}{extra}")
    elif any_rain:
        extra = f" (peak chance {peak_pop}%)" if peak_pop is not None else ""
        lines.append(f"Rain expected, timing unclear{extra}")
    else:
        lines.append("No rain expected - all models dry")

    lines.append("")
    lines.append(pick_bottom_line(mean_precip, precip_spread, categorical_aligned, precip_tol))

    lines.append("")
    for model, fc in forecasts.items():
        lines.append(
            f"{MODEL_LABEL[model]}  {fc.high_f:4.0f}/{fc.low_f:.0f}°F  "
            f"{fc.precip_in:.2f}\""
        )
    if failures:
        lines.append("")
        lines.append(f"(failed: {', '.join(failures)})")

    body = "\n".join(lines)

    # Tags -> ntfy maps these to emoji in the notification.
    tags: list[str] = []
    if any_rain:
        tags.append("cloud_with_rain" if mean_precip >= 0.25 else "umbrella")
    else:
        tags.append("sunny")
    if not (temp_aligned and precip_aligned) or failures:
        tags.append("warning")

    priority = 3 if (temp_aligned and precip_aligned and not failures) else 4
    return title, body, tags, priority


def send_ntfy(
    server: str, topic: str, title: str, body: str, tags: list[str], priority: int
) -> None:
    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "tags": tags,
        "priority": priority,
    }
    req = urllib.request.Request(
        server.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main() -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("ERROR: NTFY_TOPIC env var is required", file=sys.stderr)
        return 2

    lat_s = os.environ.get("LAT", "").strip()
    lon_s = os.environ.get("LON", "").strip()
    if not lat_s or not lon_s:
        print("ERROR: LAT and LON env vars are required", file=sys.stderr)
        return 2
    lat = float(lat_s)
    lon = float(lon_s)

    location = (os.environ.get("LOCATION_NAME") or "").strip() or f"{lat:.2f},{lon:.2f}"
    temp_tol = float(os.environ.get("TEMP_TOLERANCE_F") or DEFAULT_TEMP_TOL_F)
    precip_tol = float(os.environ.get("PRECIP_TOLERANCE_IN") or DEFAULT_PRECIP_TOL_IN)
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").strip()

    forecasts: dict[str, DayForecast] = {}
    failures: list[str] = []
    for model in MODELS:
        try:
            forecasts[model] = fetch(lat, lon, model)
        except Exception as e:
            print(f"WARN: {model} failed: {e}", file=sys.stderr)
            failures.append(MODEL_LABEL[model].strip())

    if len(forecasts) < 2:
        title = f"{location}: weather check failed"
        body = f"Could not fetch enough models today ({len(forecasts)}/3)."
        if failures:
            body += f"\nFailures: {', '.join(failures)}"
        send_ntfy(server, topic, title, body, ["warning"], 4)
        print(title)
        print(body)
        return 1

    title, body, tags, priority = build_notification(
        forecasts, failures, location, temp_tol, precip_tol
    )
    print(title)
    print(body)
    send_ntfy(server, topic, title, body, tags, priority)
    return 0


if __name__ == "__main__":
    sys.exit(main())
