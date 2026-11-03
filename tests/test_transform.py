import unittest

import pandas as pd

from src.config import Settings
from src.extract import fetch_traffic, fetch_weather
from src.transform import join_weather
from unittest.mock import patch


class JoinWeatherTests(unittest.TestCase):
    def test_missing_rain_is_unknown(self) -> None:
        traffic = pd.DataFrame(
            {
                "link_id": ["4616253", "4616253"],
                "hour_start": pd.to_datetime(["2026-10-11 14:00", "2026-10-11 15:00"]),
            }
        )
        weather = pd.DataFrame(
            {
                "hour_start": pd.to_datetime(["2026-10-11 14:00", "2026-10-11 15:00"]),
                "temperature_c": [10.0, 10.0],
                "rain_mm": [None, 0.0],
                "precipitation_mm": [None, 0.0],
            }
        )

        joined = join_weather(traffic, weather)

        self.assertTrue(pd.isna(joined.loc[0, "is_raining"]))
        self.assertEqual(joined.loc[1, "is_raining"], 0)


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
        with patch("src.extract._get_json", return_value=payload):
            weather = fetch_weather(Settings(start_date="2026-10-11", end_date="2026-10-11"))

        self.assertEqual(weather.loc[0, "hour_start"], pd.Timestamp("2026-10-11 14:00"))

    def test_unexpected_traffic_response_raises(self) -> None:
        settings = Settings(start_date="2026-10-11", end_date="2026-10-11")
        with patch("src.extract._get_json", return_value={"message": "bad gateway"}):
            with self.assertRaisesRegex(RuntimeError, "unexpected traffic response"):
                fetch_traffic(settings)

