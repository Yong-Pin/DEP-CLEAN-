# DEP Setup: Render Website + Supabase Database

## Final architecture

GitHub
- stores all source code

Nesso devices
- sample IMU at 100 Hz
- send selected samples by HTTPS to Render

Render
- hosts the visible dashboard
- runs the FastAPI backend
- processes IMU windows
- writes data into Supabase

Supabase
- PostgreSQL database
- Table Editor lets you inspect devices, sensor data, features and incidents

## Supabase setup

1. Sign in to Supabase.
2. Create a new project.
3. Choose a project name such as:
   `dep-nesso-safety`
4. Set and save the database password.
5. Wait for the project to finish creating.
6. Open SQL Editor.
7. Open `schema.sql` from this package.
8. Copy all SQL from `schema.sql`.
9. Paste it into Supabase SQL Editor.
10. Click Run.
11. Open Table Editor.

You should now see:
- devices
- sensor_data
- feature_windows
- safety_alerts

## Get the Supabase database connection string

1. In the Supabase project, click `Connect`.
2. Choose the `Session pooler` connection.
3. Copy the URI.
4. It should look similar to:

   postgres://postgres.PROJECT_REF:PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres

5. Keep this URI private.
6. Do not upload it to GitHub.

The Render backend uses this URI as `DATABASE_URL`.

## GitHub setup

1. Create or clean your GitHub repository.
2. Upload every file in this package to the root of the repository.
3. Do not upload `.env` files or database passwords.
4. Commit the files.

## Render setup using Blueprint

1. Sign in to Render.
2. Click New.
3. Choose Blueprint.
4. Select the GitHub DEP repository.
5. Render finds `render.yaml`.
6. Give the Blueprint any name such as:
   `dep-nesso-blueprint`
7. Render will ask for three environment variables.

Use:

DATABASE_URL
- paste the Supabase Session pooler URI

INGEST_API_KEY
- choose a long secret value
- example format:
  `DEP_NESSO_2026_xxxxxxxxx`

ADMIN_PASSWORD
- choose the password used by the dashboard Admin page

8. Deploy the Blueprint.
9. Wait for `dep-nesso-safety` to become Live.

## Test Render

Open:

https://YOUR-RENDER-URL.onrender.com/health

Expected:

{
  "ok": true,
  "service": "dep-nesso-render-supabase"
}

Then open:

https://YOUR-RENDER-URL.onrender.com/

The visible dashboard should load.

## Arduino setup

For each `.ino` file, change:

WIFI_SSID
WIFI_PASSWORD
API_URL
API_KEY

Example:

const char *WIFI_SSID =
    "MyWiFi";

const char *WIFI_PASSWORD =
    "MyPassword";

const char *API_URL =
    "https://dep-nesso-safety-xxxx.onrender.com/api/v1/sensor/batch";

const char *API_KEY =
    "DEP_NESSO_2026_xxxxxxxxx";

The Arduino API_KEY must exactly match Render's INGEST_API_KEY.

Upload the correct sketch to each Nesso:
- Yong_Pin_Nesso_WiFi_Render.ino
- Ryan_Nesso_WiFi_Render.ino
- lucuis_NESSO_WiFi_Render.ino

Open Serial Monitor at 115200.

Successful uploads should show:

Render HTTP: 200

## Check Supabase

After a Nesso starts sending data:

Supabase -> Table Editor -> devices

You should see the device appear.

Then check:
- sensor_data
- feature_windows
- safety_alerts

## Storage design

The Nesso samples its IMU at 100 Hz.

For network processing:
- normal cloud samples are downsampled to about 20 Hz
- abnormal samples are retained more aggressively

For database storage:
- normal raw data is saved at about 1 Hz
- abnormal windows are saved at higher resolution
- processed feature windows are stored about once per second

This reduces database growth while preserving higher-resolution data around possible incidents.

## Prototype event rules

Possible FFH:
- low-g/free-fall <= 0.45 g
- sustained for about 0.12 seconds
- followed by impact >= 2.50 g
- gyroscope peak >= 120 deg/s
- posture/tilt change >= 20 degrees

Possible STF:
- impact >= 2.50 g
- gyroscope peak >= 180 deg/s
- posture/tilt change >= 20 degrees
- without the sustained low-g FFH pattern

Possible near miss:
- no full fall impact
- gyroscope peak >= 220 deg/s
- acceleration peak-to-peak >= 0.80 g
- worker recovers at the end of the window

These are prototype starting thresholds. Tune them with your group's labelled FFH, STF, near-miss and normal-motion datasets.
