import unittest

import pandas as pd

from src.config import WEATHER_LOCATIONS, Settings, location_for_borough
from src.extract import fetch_traffic, fetch_weather
from src.transform import assign_weather_locations, join_weather
from unittest.mock import patch


class JoinWeatherTests(unittest.TestCase):
    def test_missing_rain_is_unknown(self) -> None:
        traffic = pd.DataFrame(
            {
                "link_id": ["4616253", "4616253"],
                "location_id": pd.array([1, 1], dtype="Int64"),
                "hour_start": pd.to_datetime(["2026-10-11 14:00", "2026-10-11 15:00"]),
            }
        )
        weather = pd.DataFrame(
            {
                "location_id": [1, 1],
                "hour_start": pd.to_datetime(["2026-10-11 14:00", "2026-10-11 15:00"]),
                "temperature_c": [10.0, 10.0],
                "rain_mm": [None, 0.0],
                "precipitation_mm": [None, 0.0],
            }
        )

        joined = join_weather(traffic, weather)

        self.assertTrue(pd.isna(joined.loc[0, "is_raining"]))
        self.assertEqual(joined.loc[1, "is_raining"], 0)

    def test_each_link_gets_its_own_boroughs_weather(self) -> None:
        hour = pd.Timestamp("2026-10-11 14:00")
        traffic = assign_weather_locations(
            pd.DataFrame(
                {
                    "link_id": ["4616253", "4456450", "9999999"],
                    "borough": ["Bronx", "Manhattan", None],
                    "hour_start": [hour, hour, hour],
                }
            )
        )
        weather = pd.DataFrame(
            {
                "location_id": [1, 2],
                "hour_start": [hour, hour],
                "temperature_c": [10.0, 12.0],
                "rain_mm": [0.0, 3.5],
                "precipitation_mm": [0.0, 3.5],
            }
        )

        joined = join_weather(traffic, weather).set_index("link_id")

        self.assertEqual(joined.loc["4616253", "rain_mm"], 0.0)
        self.assertEqual(joined.loc["4456450", "rain_mm"], 3.5)
        # Unknown borough stays unknown rather than borrowing another point.
        self.assertTrue(pd.isna(joined.loc["9999999", "rain_mm"]))


class LocationTests(unittest.TestCase):
    def test_every_borough_has_a_unique_point(self) -> None:
        self.assertEqual(len({loc.location_id for loc in WEATHER_LOCATIONS}), 5)
        for name in ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"):
            self.assertIsNotNone(location_for_borough(name))

    def test_borough_lookup_ignores_case_and_spacing(self) -> None:
        self.assertEqual(location_for_borough(" staten  island ").borough, "Staten Island")
        self.assertIsNone(location_for_borough("New Jersey"))


class ExtractTests(unittest.TestCase):
    def test_weather_rain_is_shifted_to_the_interval_start(self) -> None:
        payload = {
            "hourly": {
                "time": ["2026-10-11T15:00"],
                "temperature_2m": [10.0],
                "rain": [1.2],
                "precipitation": [1.2],
            }
        }
        settings = Settings(start_date="2026-10-11", end_date="2026-10-11")
        with patch("src.extract._get_json", return_value=payload) as get_json:
            weather = fetch_weather(settings, WEATHER_LOCATIONS[:2])

        self.assertEqual(weather.loc[0, "hour_start"], pd.Timestamp("2026-10-11 14:00"))
        self.assertEqual(list(weather["location_id"]), [1, 2])
        self.assertEqual(get_json.call_count, 2)

    def test_unexpected_traffic_response_raises(self) -> None:
        settings = Settings(start_date="2026-10-11", end_date="2026-10-11")
        with patch("src.extract._get_json", return_value={"message": "bad gateway"}):
            with self.assertRaisesRegex(RuntimeError, "unexpected traffic response"):
                fetch_traffic(settings)

