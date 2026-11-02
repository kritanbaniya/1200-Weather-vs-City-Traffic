"""Runtime settings for the weather-traffic pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# Same point used in api_test.ipynb (lower Manhattan).
NYC_LATITUDE = 40.7128
NYC_LONGITUDE = -74.0060
NYC_LOCATION_ID = 1
NYC_LOCATION_NAME = "New York City"


def _link_ids(raw: str) -> tuple[str, ...]:
    links = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not links:
        raise ValueError("LINK_IDS must include at least one NYC DOT link id.")
    return links


@dataclass(frozen=True)
class Settings:
    traffic_url: str = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"
    weather_url: str = "https://archive-api.open-meteo.com/v1/archive"
    latitude: float = NYC_LATITUDE
    longitude: float = NYC_LONGITUDE
    location_id: int = NYC_LOCATION_ID
    location_name: str = NYC_LOCATION_NAME
    timezone: str = "America/New_York"
    start_date: str = "2026-10-01"
    end_date: str = "2026-10-25"
    link_ids: tuple[str, ...] = ("4616253",)
    page_size: int = 1000
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "weather"
    mysql_password: str = "weather"
    mysql_database: str = "weather_traffic"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            start_date=os.getenv("START_DATE", "2026-10-01"),
            end_date=os.getenv("END_DATE", "2026-10-25"),
            link_ids=_link_ids(os.getenv("LINK_IDS", "4616253")),
            mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
            mysql_user=os.getenv("MYSQL_USER", "weather"),
            mysql_password=os.getenv("MYSQL_PASSWORD", "weather"),
            mysql_database=os.getenv("MYSQL_DATABASE", "weather_traffic"),
        )

    def validate_dates(self) -> None:
        start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(self.end_date, "%Y-%m-%d").date()
        if end < start:
            raise ValueError("END_DATE must be on or after START_DATE.")

    def days(self) -> list[date]:
        self.validate_dates()
        start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(self.end_date, "%Y-%m-%d").date()
        count = (end - start).days + 1
        return [start + timedelta(days=offset) for offset in range(count)]
