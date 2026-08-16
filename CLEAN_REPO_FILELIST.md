# Clean Repository File List

## Root deployment files

- `main.py`
- `db.py`
- `feature_engineering.py`
- `activity_engine.py`
- `thresholds.py`
- `threshold_optimizer.py`
- `telegram_notifications.py`
- `index.html`
- `app.js`
- `styles.css`
- `schema.sql`
- `requirements.txt`
- `render.yaml`
- `.env.example`
- `.gitignore`
- `README.md`

## Folders

- `assets/` - dashboard audio asset.
- `arduino/` - three Nesso N1 edge-processing + LittleFS sketches, with secret templates only.
- `analysis/` - optional Google Colab FALL/NO_FALL Decision Tree analysis.
- `docs/` - deployment, Telegram, Threshold Lab, and migration notes.

## Intentionally excluded

The clean repo does not contain:
- old ZIP packages;
- duplicate versions of `main.py`;
- screenshots;
- raw CSV recordings;
- generated Excel files;
- real Wi-Fi credentials;
- Render ingest secrets;
- Supabase passwords;
- Telegram bot tokens;
- local `secrets.h` files.
