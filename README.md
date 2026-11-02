# Weather vs NYC traffic

This repository is the start of the CSC46000 weather-and-traffic project. It turns the API checks in `api_test.ipynb` into a pipeline that downloads live data, joins weather to traffic, and loads MySQL for Tableau.

The working question for this load is: for the selected NYC DOT links, how do hourly rain and temperature line up with average speed and travel time?

## What was done

`api_test.ipynb` confirmed two APIs and cleaned one week of traffic for a single road:

- NYC DOT real-time traffic speeds, dataset `i4gi-tjb9`
- Open-Meteo hourly weather for lower Manhattan
- Link `4616253`, CBE E Taylor Avenue – Castle Hill Avenue in the Bronx, 19–25 October 2026

That notebook pull is not the saved dataset. The pipeline in `src/` repeats the extract, then goes further:

1. `src/extract.py` downloads traffic one day at a time and downloads hourly temperature, rain, and precipitation for the same local dates.
2. `src/transform.py` keeps readings whose status is `0`, drops bad timestamps, and averages the roughly 5-minute speeds and travel times up to the hour.
3. The join key is that local hour in `America/New_York`.
4. `src/load_mysql.py` writes separate weather and traffic tables and exposes the join as `vw_hourly_weather_traffic`.

The default window is **1–25 October 2026**, not only the notebook week. That week was dry. The longer window includes earlier October rain, including about 61 mm on 11 October, so rain hours and dry hours can be compared.

One run of that window produced:

| Step | Rows |
|---|---|
| Raw traffic readings | 6,722 |
| After cleaning | 6,716 |
| Weather hours stored | 600 |
| Joined hourly rows | 562 |
| Rain hours | 42 |

On this one link, rainy hours averaged about 80 seconds of travel time and 34 mph. Dry hours averaged about 76 seconds and 38 mph. That comparison only checks the join. It is not a regression or an A/B test.

Weather is one citywide point (latitude `40.7128`, longitude `-74.0060`). Traffic is still one Bronx segment. Some hours have weather but no traffic reading, which is why 600 weather hours become 562 joined rows.

## Where the data lives

The rows are in a local MySQL database named `weather_traffic`. They are not committed in this repository. Anyone else who clones the project needs MySQL and has to run the pipeline to download the data onto their own machine.

| Object | Contents |
|---|---|
| `locations` | The NYC weather point |
| `road_links` | Link id, road name, and borough |
| `weather_hourly` | Temperature (°C) and rain (mm) |
| `traffic_hourly` | Average speed (mph), average travel time (seconds), and reading count |
| `vw_hourly_weather_traffic` | The join Tableau should use, including `temperature_f` and `is_raining` |

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mysql -u root < sql/setup_database.sql
python -m src.pipeline
```

`sql/setup_database.sql` creates the database and a `weather` user with password `weather`. Connection settings and the default dates live in `.env`.

Other windows or links:

```bash
python -m src.pipeline --start 2026-10-19 --end 2026-10-25 --links 4616253
python -m src.pipeline --skip-load
```

`--skip-load` still calls the APIs and prints the join summary, without writing to MySQL. A full run replaces the requested date window and leaves rows outside that window in place.

## Tableau

1. Tableau Desktop → Connect → MySQL.
2. Server `127.0.0.1`, port `3306`, database `weather_traffic`.
3. Username `weather`, password `weather`, unless `.env` changes them.
4. Open `vw_hourly_weather_traffic` as a live connection.
5. A first sheet can put `hour_start` on columns, `avg_travel_time_sec` on rows, and color by `is_raining`.

Refreshing Tableau after another pipeline run reads the updated MySQL rows. The dashboards themselves are not in this repository.

## What is still open

The course project still needs a locked research question, a regression of weather against delay or speed, an A/B comparison against a model that also uses yesterday’s traffic, three Tableau dashboards, and a recommendation. This branch stops at the joined hourly dataset for one link.
