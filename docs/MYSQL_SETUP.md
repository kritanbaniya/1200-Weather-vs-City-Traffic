# MySQL setup guide

How to get the full-year dataset (2025-10-01 onward, every NYC DOT link plus
per-borough weather) into a local MySQL database, look at it in MySQL
Workbench, and connect Tableau to it.

The data is **not** in the repo. Everyone runs the loader once on their own
machine; it takes about 15–30 minutes.

## What you need

- **MySQL Server 8+** running locally on port 3306. You should know its
  `root` password (you set it when you installed MySQL).
- **MySQL Workbench** with a connection to the local instance
  (it usually appears as *Local instance 3306 — root — localhost:3306*).
- **Python 3.10+**
- **Tableau Desktop** for the dashboards (free with Tableau for Students).
  Tableau Public can't connect to MySQL.

You don't need the `mysql` command-line tool. If you type `mysql` in Terminal
and get `command not found`, that's fine: everything below goes through
Workbench.

## 1. Create the database and tables (Workbench)

1. Open Workbench and click **Local instance 3306**. Enter the root password.
2. Go to **File → Open SQL Script…** and open `sql/setup_database.sql` from
   this repo.
3. Click the **lightning bolt ⚡** (Execute all). The Output panel at the
   bottom should show green checks.
   This creates the `weather_traffic` database and a login
   `weather` / `weather` that Python and Tableau use.
4. Open `sql/full_year_schema.sql` the same way and execute it. It creates the
   tables and the view.
5. In the left sidebar, open the **Schemas** tab and click its refresh icon.
   Expand `weather_traffic`. You should see:
   - Tables: `dot_links`, `dot_traffic_hourly`, `weather_points`,
     `weather_point_hourly`, `load_chunks`
   - Views: `vw_traffic_weather_hourly`

   The tables are empty until step 3.

Both scripts are safe to run again; they never delete data.

## 2. Set up Python

From the repo folder in Terminal:

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt "PyMySQL[rsa]"
cp .env.example .env                 # Windows: copy .env.example .env
```

- `PyMySQL[rsa]` is needed because MySQL 8+ uses a login method that PyMySQL
  can't handle without it (otherwise: `cryptography package is required`).
- `.env` holds the connection settings. The defaults (`127.0.0.1`, port
  `3306`, user `weather`, password `weather`, database `weather_traffic`)
  already match step 1, so you don't need to edit it.
- Every new Terminal window needs `source .venv/bin/activate` again before
  running the loader.

## 3. Load the data

Test with one week first:

```bash
python -m src.bulk_load --start 2025-10-01 --end 2025-10-07
```

If it ends with a `DATABASE SUMMARY`, Python is connected. Then load the full
window (2025-10-01 to five days before today):

```bash
caffeinate -i python -m src.bulk_load      # Windows: python -m src.bulk_load
```

- `caffeinate -i` keeps a Mac from sleeping during the run.
- The test week is already recorded as loaded, so it's skipped.
- If the run stops (network error, laptop closed), run the same command
  again. It continues from where it stopped.
- Some weeks take minutes instead of seconds; that's the NYC Open Data server
  being slow, not a problem.
- Later, to add earlier years:
  `python -m src.bulk_load --start 2024-10-01 --end 2025-09-30`

### What a successful full run looks like

Numbers from the first load (2025-10-01 .. 2026-10-30). Yours should be
close; the source data can change a little over time.

| | |
|---|---|
| links | 125 (110 with valid traffic) |
| traffic link-hours | ~775,000 |
| weather point-hours | 47,395 |
| view rows missing weather | 0 |

| Borough | Links | Link-hours |
|---|---|---|
| Bronx | 23 | 165,348 |
| Brooklyn | 8 | 71,979 |
| Manhattan | 19 | 113,126 |
| Queens | 37 | 268,620 |
| Staten Island | 23 | 156,115 |

## 4. Look at the data in Workbench

Open a new query tab (the SQL+ icon) and run these one at a time
(⌘↩ / Ctrl+Enter runs the statement under the cursor):

```sql
USE weather_traffic;

-- Which weeks are loaded
SELECT * FROM load_chunks ORDER BY source, chunk_start;

-- Joined traffic + weather, one row per link per hour
SELECT * FROM vw_traffic_weather_hourly LIMIT 100;

-- Coverage by borough
SELECT borough, COUNT(DISTINCT link_id) AS links, COUNT(*) AS link_hours
FROM vw_traffic_weather_hourly
GROUP BY borough;
```

You can also right-click any table in the sidebar → **Select Rows – Limit
1000**. Workbench shows at most 1,000 rows by default, so use `COUNT(*)` to
check totals.

## 5. Connect Tableau

1. **Connect → To a Server → MySQL.**
2. Server `127.0.0.1`, port `3306`, username `weather`, password `weather`.
   Sign in.
   If Tableau says a driver is missing, it shows a link to the driver page.
   Install that driver and restart Tableau.
3. Pick the `weather_traffic` database and drag `vw_traffic_weather_hourly`
   onto the canvas.
4. Choose **Extract** (not Live). With ~775k rows, extracts make dashboards
   much faster. Refresh the extract after each new load.
5. For maps: set `mid_latitude` / `mid_longitude` to the Latitude /
   Longitude geographic roles, and color by `avg_speed_mph`.

## What's in the tables

| Object | Contents |
|---|---|
| `weather_points` | The five borough weather points and the Open-Meteo grid cell each one snapped to |
| `dot_links` | Every DOT link: name, borough, owner, path (`link_points`), midpoint for maps, its weather point, first/last seen |
| `dot_traffic_hourly` | Per link-hour: avg/median/min/max speed (mph), avg/max travel time (s), number of readings. Only valid readings (`status = 0`, speed > 0) |
| `weather_point_hourly` | Per point-hour: temperature (°C), precipitation and rain (mm), snowfall (cm), WMO weather code, wind at 10 m (km/h) |
| `load_chunks` | Which date chunks have been loaded |
| `vw_traffic_weather_hourly` | **Use this one.** Traffic joined to its borough's weather, plus °F, inches, mph, weekday, weekend, rain/snow flags |

All times are New York local time. A row stamped 14:00 covers 14:00–14:59,
and its precipitation is the total for that hour.

These tables are separate from the original single-link pipeline's tables
(`locations`, `road_links`, `traffic_hourly`, `weather_hourly`,
`vw_hourly_weather_traffic`). The two don't affect each other.

## Troubleshooting

| Error | Fix |
|---|---|
| `zsh: command not found: mysql` | Ignore it and use Workbench (step 1). |
| `Access denied for user 'weather'` | `sql/setup_database.sql` wasn't run. Run it in Workbench. |
| `cryptography package is required` | `pip install "PyMySQL[rsa]"` |
| `Can't connect to MySQL server on '127.0.0.1'` | MySQL isn't running. Mac: **System Settings → MySQL → Start**. Windows: start the *MySQL80* service in Services. |
| `Unknown database 'weather_traffic'` | Run `sql/setup_database.sql`. |
| `No module named src` | Run the command from the repo folder. |
| `No module named pandas` (or pymysql) | Activate the venv: `source .venv/bin/activate`. |
| A week fails with an HTTP 429 or timeout | Run the command again. To avoid throttling, get a free app token at data.cityofnewyork.us and put it in `.env` as `NYC_APP_TOKEN=...`. |
