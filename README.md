# Weather vs NYC traffic

This repository is the start of the CSC46000 weather-and-traffic project. It turns the API checks in `api_test.ipynb` into a pipeline that downloads live data, joins weather to traffic, and loads MySQL for Tableau.

The working question for this load is: for the selected NYC DOT links, how do hourly rain and temperature line up with average speed and travel time?

## What was done

`api_test.ipynb` confirmed two APIs and cleaned one week of traffic for a single road:

- NYC DOT real-time traffic speeds, dataset `i4gi-tjb9`
- Open-Meteo hourly weather at the selected Bronx road's midpoint
- Link `4616253`, CBE E Taylor Avenue – Castle Hill Avenue in the Bronx, 19–25 October 2026

That notebook pull is not the saved dataset. The pipeline in `src/` repeats the extract, then goes further:

1. `src/extract.py` downloads traffic one day at a time and downloads hourly temperature, rain, and precipitation for the same local dates.
2. `src/transform.py` keeps readings whose status is `0`, drops bad timestamps, and averages the roughly 5-minute speeds and travel times up to the hour.
3. NYC DOT's unoffset `data_as_of` values are treated as `America/New_York`
   local time. Open-Meteo is requested in that same timezone.
4. Open-Meteo stamps hourly rain at the end of its accumulation interval, so
   rainfall stamped 3 PM is stored and joined to traffic from 2–3 PM.
5. `src/load_mysql.py` writes separate weather and traffic tables and exposes the join as `vw_hourly_weather_traffic`.

The default window is **1–25 October 2026**, not only the notebook week. That week was dry. The longer window includes earlier October rain, including about 61 mm on 11 October, so rain hours and dry hours can be compared.

The command output reports the extracted, cleaned, joined, and rain-hour counts
for each run. Those values can change as the source APIs revise historical data.
The rainy-versus-dry comparison only checks the join; it is not a regression or
an A/B test.

Weather is fetched per borough, not per road. `WEATHER_LOCATIONS` in
`src/config.py` defines one Open-Meteo point for each of the five boroughs, and
each link uses its borough's point based on the `borough` field NYC DOT returns.
The pipeline only requests weather for the boroughs that the selected links are
in. The Bronx point is still the midpoint of link `4616253` (latitude `40.83242`,
longitude `-73.85833`). A link with a missing or unrecognised borough gets
`NULL` weather instead of borrowing another borough's. Missing rain remains unknown (`NULL` / nullable
`is_raining`) rather than being treated as dry. Some hours have weather but no
traffic reading, so the weather and joined-row counts can differ.

## Where the data lives

The rows are in a local MySQL database named `weather_traffic`. They are not committed in this repository. Anyone else who clones the project needs MySQL and has to run the pipeline to download the data onto their own machine.

| Object | Contents |
|---|---|
| `locations` | One weather point per borough |
| `road_links` | Link id, road name, borough, and its weather `location_id` |
| `weather_hourly` | Temperature (°C) and rain (mm) |
| `traffic_hourly` | Average speed (mph), average travel time (seconds), and reading count |
| `vw_hourly_weather_traffic` | The join Tableau should use, including `weather_location`, `temperature_f`, and `is_raining` |

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

Several roads in different boroughs (each gets its own borough's weather):

```bash
python -m src.pipeline --links 4616253,4616328,4616257 --skip-load
```

To list every link the API currently reports, with its borough:

```bash
curl -s 'https://data.cityofnewyork.us/resource/i4gi-tjb9.json?$select=distinct%20link_id,link_name,borough&$where=data_as_of%3E%3D%272026-10-01T00:00:00%27&$limit=1000'
```

Coverage is about 125 links, mostly highways, bridges, and tunnels rather than
neighborhood streets. Not all of them are usable: for 1–25 October 2026 only
about 79 had a status `0` reading at least half the time. Many others report
nothing but `-101` sensor flags, which cleaning drops. That includes every link
near Harlem (RFK/Triborough Bridge at E 116th St and Major Deegan at 142nd St).
Check the `status` of a link before you rely on it. Traffic is downloaded one link and one day at a time, so a
run over many links and weeks makes thousands of requests and takes a while.

Databases created before per-borough weather are upgraded on the next load: the
loader adds `road_links.location_id` and recreates the view.

`--skip-load` still calls the APIs and prints the join summary, without writing to MySQL. A full run replaces the requested date window and leaves rows outside that window in place.

## Full-year load (all links)

`src/bulk_load.py` loads every DOT link, not just the selected ones, into a
separate set of tables defined in `sql/full_year_schema.sql`. It does not read
or write the tables above.

New to MySQL? Follow [docs/MYSQL_SETUP.md](docs/MYSQL_SETUP.md) for the
step-by-step Workbench, Python and Tableau setup.

| Object | Contents |
|---|---|
| `weather_points` | Open-Meteo request points (the five borough points for now; `point_type = 'link'` is reserved for segment midpoints) and the grid cell each one snapped to |
| `dot_links` | Every link that reported: name, borough, owner, raw `link_points`, a midpoint for maps, its weather point, first/last seen |
| `dot_traffic_hourly` | Per link-hour: avg/median/min/max speed (mph), avg/max travel time (s), reading count. Valid readings only (`status = '0'`, speed > 0) |
| `weather_point_hourly` | Per point-hour: temperature (°C), precipitation/rain (mm), snowfall (cm), WMO weather code, wind at 10 m (km/h) |
| `load_chunks` | Which date chunks are loaded, so a stopped run resumes |
| `vw_traffic_weather_hourly` | The joined view for Tableau, with °F, inches, mph, weekday and rain/snow flags |

```bash
mysql -u root < sql/setup_database.sql      # once: database + weather user
mysql -u root < sql/full_year_schema.sql    # once (the loader also applies it)
python -m src.bulk_load                     # 2025-10-01 .. today minus 5 days
```

- Traffic is aggregated to the hour by NYC Open Data's server, one request per
  week plus one for link details, so a year is about 115 requests. Expect
  roughly 20-30 minutes and about 550-600k link-hours.
- Weather comes from the Open-Meteo archive in 92-day chunks. It is requested
  in UTC and converted to New York time, so DST days have 23 or 24 hours with
  no duplicates. Precipitation, rain and snowfall are moved back one hour
  (Open-Meteo stamps them at the end of the hour they cover); temperature,
  wind and weather code are the values at the start of the hour.
- The end date defaults to five days ago because the weather archive lags.
- Every chunk is committed on its own. Re-running skips loaded chunks; add
  `--force` to re-fetch them.
- Earlier years later: `python -m src.bulk_load --start 2024-10-01 --end 2025-09-30`.
- `--dry-run` fetches without touching MySQL. `--traffic-only` and
  `--weather-only` do one side.
- Optional: put a free NYC Open Data app token in `.env` as `NYC_APP_TOKEN`
  to avoid throttling.

## Tableau

1. Tableau Desktop → Connect → MySQL.
2. Server `127.0.0.1`, port `3306`, database `weather_traffic`.
3. Username `weather`, password `weather`, unless `.env` changes them.
4. Open `vw_hourly_weather_traffic` as a live connection.
5. A first sheet can put `hour_start` on columns, `avg_travel_time_sec` on rows, and color by `is_raining`.

Refreshing Tableau after another pipeline run reads the updated MySQL rows. The dashboards themselves are not in this repository.

## What is still open

The course project still needs a locked research question, a regression of weather against delay or speed, an A/B comparison against a model that also uses yesterday’s traffic, three Tableau dashboards, and a recommendation. This branch stops at the joined hourly dataset.
