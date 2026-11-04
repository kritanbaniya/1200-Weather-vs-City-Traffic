"""Runtime settings for the weather-traffic pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

@dataclass(frozen=True)
class WeatherLocation:
    """One Open-Meteo point. Every road link in a borough shares its point."""

    location_id: int
    borough: str
    name: str
    latitude: float
    longitude: float


# One weather point per borough. Open-Meteo's grid is a few kilometres wide, so
# a point per road link would add requests without adding much detail.
# The Bronx point stays at the midpoint of link 4616253 (CBE E Taylor Avenue –
# Castle Hill Avenue) and keeps location_id 1, so rows loaded before
# per-borough weather still line up. The other points sit near the DOT links
# in each borough rather than at the borough's geographic centre.
WEATHER_LOCATIONS: tuple[WeatherLocation, ...] = (
    WeatherLocation(1, "Bronx", "Bronx - Cross Bronx Expwy / Castle Hill Ave", 40.83242, -73.85833),
    WeatherLocation(2, "Manhattan", "Manhattan - Midtown West Side", 40.76000, -73.99000),
    WeatherLocation(3, "Brooklyn", "Brooklyn - Gowanus Expwy / Sunset Park", 40.66000, -74.00000),
    WeatherLocation(4, "Queens", "Queens - Central Queens", 40.72820, -73.79490),
    WeatherLocation(5, "Staten Island", "Staten Island - Central", 40.57950, -74.15020),
)
_LOCATIONS_BY_BOROUGH = {
    location.borough.casefold(): location for location in WEATHER_LOCATIONS
}


def location_for_borough(borough: object) -> WeatherLocation | None:
    """Return the weather point for a NYC DOT borough name, or None if unknown."""
    if not isinstance(borough, str):
        return None
    return _LOCATIONS_BY_BOROUGH.get(" ".join(borough.split()).casefold())


def _link_ids(raw: str) -> tuple[str, ...]:
    links = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not links:
        raise ValueError("LINK_IDS must include at least one NYC DOT link id.")
    return links


@dataclass(frozen=True)
class Settings:
    traffic_url: str = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"
    weather_url: str = "https://archive-api.open-meteo.com/v1/archive"
    # NYC DOT's unoffset data_as_of values are local New York civil time.
    traffic_timezone: str = "America/New_York"
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
