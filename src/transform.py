"""Clean traffic readings and align them to hourly weather."""

from __future__ import annotations

import pandas as pd

# NYC DOT uses status 0 for a normal reading. -101 showed up as a sensor flag
# with zero speed and zero travel time in the sample week.


def clean_traffic(
    traffic: pd.DataFrame, timezone: str = "America/New_York"
) -> pd.DataFrame:
    """Clean NYC DOT readings whose unoffset timestamps are New York local time."""
    cleaned = traffic.copy()
    cleaned["data_as_of"] = pd.to_datetime(cleaned["data_as_of"], errors="coerce")
    # Validate the source timestamps against the documented local timeline while
    # retaining naive local values for MySQL DATETIME and the Open-Meteo join.
    localized = cleaned["data_as_of"].dt.tz_localize(
        timezone, ambiguous="NaT", nonexistent="shift_forward"
    )
    cleaned["data_as_of"] = localized.dt.tz_localize(None)
    cleaned["speed"] = pd.to_numeric(cleaned["speed"], errors="coerce")
    cleaned["travel_time"] = pd.to_numeric(cleaned["travel_time"], errors="coerce")
    status = pd.to_numeric(cleaned["status"], errors="coerce")

    cleaned = cleaned.loc[status.eq(0)].copy()
    cleaned = cleaned.dropna(subset=["data_as_of", "speed", "link_id"])
    cleaned = cleaned.loc[cleaned["speed"] >= 0]
    cleaned = cleaned.drop_duplicates(subset=["link_id", "data_as_of"])
    cleaned = cleaned.sort_values(["link_id", "data_as_of"])
    return cleaned.reset_index(drop=True)


def aggregate_traffic(traffic: pd.DataFrame) -> pd.DataFrame:
    """Roll ~5 minute readings up to the hour so they match hourly weather."""
    hourly_source = traffic.copy()
    hourly_source["hour_start"] = hourly_source["data_as_of"].dt.floor("h")
    hourly = (
        hourly_source.groupby(["link_id", "hour_start"], as_index=False)
        .agg(
            link_name=("link_name", "first"),
            borough=("borough", "first"),
            avg_speed_mph=("speed", "mean"),
            avg_travel_time_sec=("travel_time", "mean"),
            reading_count=("speed", "size"),
        )
        .sort_values(["link_id", "hour_start"])
        .reset_index(drop=True)
    )
    hourly["avg_speed_mph"] = hourly["avg_speed_mph"].round(2)
    hourly["avg_travel_time_sec"] = hourly["avg_travel_time_sec"].round(2)
    return hourly


def join_weather(traffic_hourly: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Left-join weather accumulated during each road-link hour."""
    weather_columns = weather[
        ["hour_start", "temperature_c", "rain_mm", "precipitation_mm"]
    ]
    joined = traffic_hourly.merge(weather_columns, on="hour_start", how="left")
    # Nullable integer preserves missing rainfall as unknown rather than dry.
    joined["is_raining"] = (
        joined["rain_mm"]
        .gt(0)
        .where(joined["rain_mm"].notna())
        .astype("Int64")
    )
    joined["temperature_f"] = (joined["temperature_c"] * 9 / 5 + 32).round(2)
    return joined
