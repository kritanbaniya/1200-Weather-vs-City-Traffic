"""Pull NYC DOT traffic and matching Open-Meteo weather."""

from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd
import requests

from src.config import Settings

TRAFFIC_COLUMNS = (
    "link_id,link_name,borough,speed,travel_time,status,data_as_of"
)
TRAFFIC_REQUIRED_FIELDS = {"link_id", "speed", "travel_time", "status", "data_as_of"}


def _get_json(url: str, params: dict, timeout: int = 30, attempts: int = 3) -> object:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Request failed for {url}") from last_error


def fetch_traffic(settings: Settings) -> pd.DataFrame:
    """Download one inclusive date range, one road link at a time, with pagination."""
    frames: list[pd.DataFrame] = []

    for link_id in settings.link_ids:
        for day in settings.days():
            next_day = day + timedelta(days=1)
            offset = 0
            while True:
                records = _get_json(
                    settings.traffic_url,
                    {
                        "$select": TRAFFIC_COLUMNS,
                        "$where": (
                            f"link_id = '{link_id}' "
                            f"AND data_as_of >= '{day:%Y-%m-%d}T00:00:00' "
                            f"AND data_as_of < '{next_day:%Y-%m-%d}T00:00:00'"
                        ),
                        "$order": "data_as_of, :id",
                        "$limit": settings.page_size,
                        "$offset": offset,
                    },
                )
                if not isinstance(records, list):
                    raise RuntimeError(
                        "NYC DOT returned an unexpected traffic response; expected a JSON list."
                    )
                if not records:
                    break
                if any(
                    not isinstance(record, dict)
                    or TRAFFIC_REQUIRED_FIELDS.difference(record)
                    for record in records
                ):
                    raise RuntimeError(
                        "NYC DOT returned traffic rows missing required fields: "
                        f"{sorted(TRAFFIC_REQUIRED_FIELDS)}"
                    )
                frames.append(pd.DataFrame(records))
                offset += len(records)
                if len(records) < settings.page_size:
                    break
            print(f"Traffic {link_id} {day:%Y-%m-%d}: {offset} records")

    if not frames:
        raise RuntimeError(
            "NYC DOT returned no traffic rows for the requested links and dates."
        )
    return pd.concat(frames, ignore_index=True)


def fetch_weather(settings: Settings) -> pd.DataFrame:
    """Hourly temperature and rain, labelled with the hour the interval begins."""
    payload = _get_json(
        settings.weather_url,
        {
            "latitude": settings.latitude,
            "longitude": settings.longitude,
            "start_date": settings.start_date,
            "end_date": settings.end_date,
            "hourly": "temperature_2m,rain,precipitation",
            "timezone": settings.timezone,
        },
    )
    if not isinstance(payload, dict) or "hourly" not in payload:
        raise RuntimeError("Open-Meteo did not return hourly weather.")

    weather = pd.DataFrame(payload["hourly"])
    expected = {"time", "temperature_2m", "rain", "precipitation"}
    missing = expected.difference(weather.columns)
    if missing:
        raise RuntimeError(f"Open-Meteo response missing columns: {sorted(missing)}")

    weather = weather.rename(
        columns={
            "time": "hour_start",
            "temperature_2m": "temperature_c",
            "rain": "rain_mm",
            "precipitation": "precipitation_mm",
        }
    )
    # Open-Meteo's value stamped 15:00 is the accumulation over 14:00–15:00.
    # Store it under 14:00 so it joins traffic observed during that same hour.
    weather["hour_start"] = (
        pd.to_datetime(weather["hour_start"]) - pd.Timedelta(hours=1)
    )
    for column in ("temperature_c", "rain_mm", "precipitation_mm"):
        weather[column] = pd.to_numeric(weather[column], errors="coerce")
    weather["location_id"] = settings.location_id
    print(
        f"Weather {settings.start_date} to {settings.end_date}: {len(weather)} hours"
    )
    return weather[
        [
            "location_id",
            "hour_start",
            "temperature_c",
            "rain_mm",
            "precipitation_mm",
        ]
    ]
