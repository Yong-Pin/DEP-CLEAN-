# DEP Nesso Construction Safety System

Clean deployment repository for the Nesso N1 construction-safety Data Engineering Project.

## Architecture

```text
Nesso N1 IMU
  -> Wi-Fi HTTPS
  -> Render FastAPI backend
  -> feature engineering + rule-based FFH/STF/near-miss detector
  -> Supabase PostgreSQL
  -> dashboard + incident review + Threshold Lab
  -> Telegram supervisor alerts

No network:
Nesso N1 -> local edge detector -> LittleFS buffer -> sync after reconnect
```

## What is in this repository

### Deployable website/backend
- `main.py` - FastAPI API, dashboard serving, sensor ingestion, edge-event ingestion.
- `db.py` - PostgreSQL/Supabase connection and schema bootstrap.
- `feature_engineering.py` - IMU window feature calculations.
- `activity_engine.py` - rule-based activity/fall classification support.
- `thresholds.py` - live prototype rule thresholds.
- `threshold_optimizer.py` - Threshold Lab Decision Tree analysis.
- `telegram_notifications.py` - Telegram recipients and notifications.
- `index.html`, `app.js`, `styles.css` - dashboard UI.
- `schema.sql` - database schema.
- `render.yaml` - Render deployment configuration.

### Arduino
`arduino/` contains one edge-processing + persistent-buffer sketch for each Nesso device.
Each sketch expects a local `secrets.h`. Copy `secrets.example.h` to `secrets.h` and fill in the values locally. `secrets.h` is ignored by Git.

### Analysis
`analysis/FALL_NOFALL_DecisionTree_Colab.ipynb` is the optional offline Google Colab analysis notebook for labelled FALL / NO_FALL CSV recordings.

## Threshold Lab

Threshold Lab is deliberately **binary**:

```text
FFH / STF reviewed event -> FALL
Near miss / false alarm -> NO_FALL
Ordinary background window -> NO_FALL
```

It trains a Decision Tree using the same engineered sensor-window features used by the backend:
- acceleration peak
- gyroscope peak
- minimum acceleration
- tilt change
- low-g duration
- acceleration peak-to-peak

When enough labels exist, model settings are selected with stratified cross-validation on the training set, then evaluated on a separate 30% holdout set.

The page reports:
- Decision Tree accuracy
- balanced accuracy
- FALL recall
- FALL precision
- confusion matrix
- learned split rules
- feature importance
- current rule thresholds

**The model never automatically changes the live detector.** Learned splits are recommendations that must be validated with controlled labelled tests before changing `thresholds.py` or the Nesso edge detector.

## Render environment variables

Set these in Render; never commit their real values:

```text
DATABASE_URL
INGEST_API_KEY
ADMIN_PASSWORD
TELEGRAM_BOT_TOKEN
PUBLIC_DASHBOARD_URL
```

See `.env.example`.

## Deploy

1. Create a new GitHub repository and upload the contents of this folder.
2. In Supabase, run `schema.sql` if the schema has not already been created. The application also calls `ensure_schema()` during startup.
3. Point the existing Render service at the new repository, or create a Render Blueprint using `render.yaml`.
4. Re-enter the Render environment variables. Do not put secrets in GitHub.
5. Confirm `/health` returns HTTP 200.
6. Confirm ordinary Arduino batches show `Render HTTP: 200`.
7. Confirm edge events show `Render EDGE EVENT HTTP: 200`.
8. Review incidents in the dashboard before training Threshold Lab.

## Important validation note

This is a school prototype, not a certified occupational-safety device. Report accuracy only from controlled, independently labelled trials. Do not treat automatically inferred historical labels as independent ground truth.
