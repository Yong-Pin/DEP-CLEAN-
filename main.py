import math
import os
import secrets
import time
from time import perf_counter
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg2.extras import Json, RealDictCursor, execute_values

from activity_engine import classify
from db import (
    close_pool,
    database,
    ensure_schema,
    init_pool,
)
from feature_engineering import calculate_features
from telegram_notifications import (
    cancel_pairing,
    create_pairing,
    get_notification_settings,
    list_recipients,
    notify_incident_async,
    recent_notification_log,
    remove_recipient,
    send_test_notification_async,
    start_device_notification_monitor,
    start_recipient_pairing_monitor,
    stop_device_notification_monitor,
    stop_recipient_pairing_monitor,
    telegram_config,
    update_notification_settings,
    update_recipient,
)
from threshold_optimizer import (
    status as threshold_learning_status,
    train_recommendation as train_threshold_recommendation,
)
from thresholds import (
    ALERT_COOLDOWN_S,
    DEVICE_OFFLINE_AFTER_S,
    FFH_GYRO_DPS,
    FREE_FALL_G,
    FREE_FALL_MIN_DURATION_S,
    HIGH_RES_ACCEL_G,
    HIGH_RES_GYRO_DPS,
    HIGH_RES_LOW_G,
    IMPACT_G,
    MIN_TILT_CHANGE_DEG,
    NEAR_MISS_ACC_PP_G,
    NEAR_MISS_GYRO_DPS,
    NORMAL_DATABASE_INTERVAL_S,
    PROCESS_WINDOW_S,
    STATUS_UPDATE_INTERVAL_S,
    STF_GYRO_DPS,
)


BASE_DIR = Path(__file__).resolve().parent

INGEST_API_KEY = os.getenv(
    "INGEST_API_KEY",
    "",
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "",
)

sample_windows = defaultdict(deque)
last_feature_save = defaultdict(float)
last_normal_database_save = defaultdict(float)
replay_capture_until = {}

# When a Nesso edge detector reports an event, keep the worker card
# showing that event briefly so the next normal sensor batch does not
# immediately overwrite it with MOVING / STANDING / COLLECTING.
edge_event_status_hold = {}
EDGE_EVENT_STATUS_HOLD_S = 10.0

REPLAY_POST_EVENT_S = 5.0
EXPECTED_CLOUD_HZ = 20.0
LOCAL_IMU_HZ = 100.0


@asynccontextmanager
async def lifespan(app):
    init_pool()
    ensure_schema()
    start_device_notification_monitor()
    start_recipient_pairing_monitor()

    yield

    stop_recipient_pairing_monitor()
    stop_device_notification_monitor()
    close_pool()


app = FastAPI(
    title="DEP Nesso Construction Safety System",
    version="5.1",
    lifespan=lifespan,
)

app.mount(
    "/assets",
    StaticFiles(
        directory=BASE_DIR / "assets"
    ),
    name="assets",
)


class SensorSample(BaseModel):
    t: float = Field(ge=0)
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


class SensorBatch(BaseModel):
    device_name: str = Field(
        min_length=1,
        max_length=100,
    )

    worker_id: str | None = Field(
        default=None,
        max_length=80,
    )

    battery_percent: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )

    samples: list[SensorSample] = Field(
        min_length=1,
        max_length=100,
    )

class EdgeEventReport(BaseModel):
    device_name: str = Field(
        min_length=1,
        max_length=100,
    )

    worker_id: str | None = Field(
        default=None,
        max_length=80,
    )

    battery_percent: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )

    event_type: str = Field(
        pattern="^(FFH|STF|NEAR_MISS)$",
    )

    confidence: int = Field(
        ge=0,
        le=100,
    )

    peak_acceleration_g: float = Field(
        ge=0,
    )

    peak_gyro_dps: float = Field(
        ge=0,
    )

    minimum_acceleration_g: float | None = Field(
        default=None,
        ge=0,
    )

    low_g_duration_s: float | None = Field(
        default=None,
        ge=0,
    )

    detected_elapsed_s: float | None = Field(
        default=None,
        ge=0,
    )


class WorkerAssignment(BaseModel):
    worker_id: str = Field(
        min_length=1,
        max_length=80,
    )


class IncidentFeedback(BaseModel):
    label: str = Field(
        pattern="^(ACTUAL_EVENT|FALSE_ALARM|UNSURE)$"
    )
    actual_event_type: str | None = Field(
        default=None,
        pattern="^(FFH|STF|NEAR_MISS)$",
    )
    notes: str = Field(
        default="",
        max_length=1000,
    )


class NotificationSettingsUpdate(BaseModel):
    telegram_enabled: bool = True
    normal_updates_enabled: bool = True
    near_miss_enabled: bool = True
    stf_enabled: bool = True
    ffh_enabled: bool = True
    device_offline_enabled: bool = True
    low_battery_enabled: bool = True
    critical_repeat_seconds: int = Field(default=30, ge=15, le=300)


class NotificationTest(BaseModel):
    priority: str = Field(pattern="^(NORMAL|URGENT|CRITICAL)$")
    recipient_id: int | None = None


class TelegramPairingCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    recipient_type: str = Field(pattern="^(PERSON|GROUP)$")
    normal_enabled: bool = True
    urgent_enabled: bool = True
    critical_enabled: bool = True


class TelegramRecipientUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    active: bool = True
    normal_enabled: bool = True
    urgent_enabled: bool = True
    critical_enabled: bool = True


def require_ingest_key(
    x_api_key: str | None,
):
    if not INGEST_API_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "INGEST_API_KEY is missing "
                "from Render."
            ),
        )

    if (
        not x_api_key
        or not secrets.compare_digest(
            x_api_key,
            INGEST_API_KEY,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
        )


def require_admin_password(
    x_admin_password: str | None,
):
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail=(
                "ADMIN_PASSWORD is missing "
                "from Render."
            ),
        )

    if (
        not x_admin_password
        or not secrets.compare_digest(
            x_admin_password,
            ADMIN_PASSWORD,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Incorrect admin password.",
        )


def fetch_all(
    query,
    params=None,
):
    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                query,
                params,
            )

            return [
                dict(row)
                for row in cursor.fetchall()
            ]


def resolve_worker(
    device_name,
    suggested_worker,
):
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT worker_id
                FROM devices
                WHERE device_name = %s
                """,
                (device_name,),
            )

            row = cursor.fetchone()

            if row:
                return row[0]

            worker = (
                suggested_worker.strip()
                if suggested_worker
                and suggested_worker.strip()
                else device_name
            )

            cursor.execute(
                """
                INSERT INTO devices (
                    device_name,
                    worker_id,
                    last_seen,
                    activity_status,
                    status_message
                )
                VALUES (
                    %s,
                    %s,
                    CURRENT_TIMESTAMP,
                    'COLLECTING',
                    'Receiving sensor data.'
                )
                ON CONFLICT (device_name)
                DO NOTHING
                """,
                (
                    device_name,
                    worker,
                ),
            )

            connection.commit()

            return worker


def make_processed_samples(
    batch,
    received_at,
):
    maximum_elapsed = max(
        sample.t
        for sample in batch.samples
    )

    processed = []

    for sample in batch.samples:
        acc_mag = math.sqrt(
            sample.ax ** 2
            + sample.ay ** 2
            + sample.az ** 2
        )

        gyro_mag = math.sqrt(
            sample.gx ** 2
            + sample.gy ** 2
            + sample.gz ** 2
        )

        timestamp = (
            received_at
            - timedelta(
                seconds=max(
                    0.0,
                    maximum_elapsed
                    - sample.t,
                )
            )
        )

        processed.append(
            {
                "timestamp_dt": timestamp,
                "timestamp": timestamp.timestamp(),
                "elapsed_time_s": sample.t,
                "ax": sample.ax,
                "ay": sample.ay,
                "az": sample.az,
                "gx": sample.gx,
                "gy": sample.gy,
                "gz": sample.gz,
                "acc_mag": acc_mag,
                "gyro_mag": gyro_mag,
            }
        )

    return processed


def update_memory_window(
    device_name,
    processed,
):
    window = sample_windows[
        device_name
    ]

    window.extend(
        processed
    )

    cutoff = (
        processed[-1]["timestamp"]
        - PROCESS_WINDOW_S
    )

    while (
        window
        and window[0]["timestamp"]
        < cutoff
    ):
        window.popleft()

    return list(window)


def batch_is_abnormal(
    processed,
):
    for sample in processed:
        if (
            sample["acc_mag"]
            >= HIGH_RES_ACCEL_G
            or sample["acc_mag"]
            <= HIGH_RES_LOW_G
            or sample["gyro_mag"]
            >= HIGH_RES_GYRO_DPS
        ):
            return True

    return False


def rows_to_store(
    device_name,
    worker_id,
    processed,
):
    high_resolution = (
        batch_is_abnormal(
            processed
        )
    )

    now = time.time()

    if high_resolution:
        selected = processed

    elif (
        now
        - last_normal_database_save[
            device_name
        ]
        >= NORMAL_DATABASE_INTERVAL_S
    ):
        selected = [
            processed[-1]
        ]

        last_normal_database_save[
            device_name
        ] = now

    else:
        selected = []

    rows = []

    for sample in selected:
        rows.append(
            (
                device_name,
                worker_id,
                sample["timestamp_dt"],
                sample["elapsed_time_s"],
                sample["ax"],
                sample["ay"],
                sample["az"],
                sample["acc_mag"],
                sample["gx"],
                sample["gy"],
                sample["gz"],
                sample["gyro_mag"],
                high_resolution,
            )
        )

    return rows, high_resolution


def build_trigger_details(features, decision):
    checks = []

    def add_check(key, label, value, threshold, passed, comparison, unit):
        checks.append(
            {
                "key": key,
                "label": label,
                "value": round(float(value), 4),
                "threshold": round(float(threshold), 4),
                "passed": bool(passed),
                "comparison": comparison,
                "unit": unit,
            }
        )

    if decision.status == "POSSIBLE_FFH":
        add_check(
            "low_g_duration",
            "Low-g / free-fall duration",
            features.low_g_duration_s,
            FREE_FALL_MIN_DURATION_S,
            features.low_g_duration_s >= FREE_FALL_MIN_DURATION_S,
            ">=",
            "s",
        )
        add_check(
            "impact",
            "Impact acceleration",
            features.acc_peak_g,
            IMPACT_G,
            features.acc_peak_g >= IMPACT_G,
            ">=",
            "g",
        )
        add_check(
            "rotation",
            "Body rotation",
            features.gyro_max_dps,
            FFH_GYRO_DPS,
            features.gyro_max_dps >= FFH_GYRO_DPS,
            ">=",
            "deg/s",
        )
        add_check(
            "tilt",
            "Posture / tilt change",
            features.tilt_change_deg,
            MIN_TILT_CHANGE_DEG,
            features.tilt_change_deg >= MIN_TILT_CHANGE_DEG,
            ">=",
            "deg",
        )

    elif decision.status == "POSSIBLE_STF":
        add_check(
            "impact",
            "Impact acceleration",
            features.acc_peak_g,
            IMPACT_G,
            features.acc_peak_g >= IMPACT_G,
            ">=",
            "g",
        )
        add_check(
            "rotation",
            "Rapid rotation",
            features.gyro_max_dps,
            STF_GYRO_DPS,
            features.gyro_max_dps >= STF_GYRO_DPS,
            ">=",
            "deg/s",
        )
        add_check(
            "tilt",
            "Posture / tilt change",
            features.tilt_change_deg,
            MIN_TILT_CHANGE_DEG,
            features.tilt_change_deg >= MIN_TILT_CHANGE_DEG,
            ">=",
            "deg",
        )
        add_check(
            "no_sustained_freefall",
            "No sustained free-fall",
            features.low_g_duration_s,
            FREE_FALL_MIN_DURATION_S,
            features.low_g_duration_s < FREE_FALL_MIN_DURATION_S,
            "<",
            "s",
        )

    elif decision.status == "POSSIBLE_NEAR_MISS":
        add_check(
            "rotation",
            "Sudden rotation",
            features.gyro_max_dps,
            NEAR_MISS_GYRO_DPS,
            features.gyro_max_dps >= NEAR_MISS_GYRO_DPS,
            ">=",
            "deg/s",
        )
        add_check(
            "acc_variation",
            "Acceleration variation",
            features.acc_pp_g,
            NEAR_MISS_ACC_PP_G,
            features.acc_pp_g >= NEAR_MISS_ACC_PP_G,
            ">=",
            "g",
        )
        add_check(
            "below_full_impact",
            "Below full-impact threshold",
            features.acc_peak_g,
            IMPACT_G,
            features.acc_peak_g < IMPACT_G,
            "<",
            "g",
        )

    return {
        "event_type": decision.status,
        "summary": decision.message,
        "checks": checks,
        "prototype_note": (
            "These are prototype rule-based thresholds and should be tuned "
            "with labelled project data before making safety claims."
        ),
    }


def replay_rows(incident_id, event_timestamp, samples):
    rows = []

    for sample in samples:
        relative = (
            sample["timestamp_dt"] - event_timestamp
        ).total_seconds()
        phase = "PRE" if relative < -0.05 else ("POST" if relative > 0.05 else "EVENT")
        rows.append(
            (
                incident_id,
                sample.get("device_name"),
                sample["timestamp_dt"],
                relative,
                phase,
                sample["ax"],
                sample["ay"],
                sample["az"],
                sample["acc_mag"],
                sample["gx"],
                sample["gy"],
                sample["gz"],
                sample["gyro_mag"],
            )
        )

    return rows


def insert_replay_rows(cursor, incident_id, device_name, event_timestamp, samples):
    if not samples:
        return

    normalized = []
    for sample in samples:
        item = dict(sample)
        item["device_name"] = device_name
        normalized.append(item)

    rows = replay_rows(
        incident_id,
        event_timestamp,
        normalized,
    )

    execute_values(
        cursor,
        """
        INSERT INTO incident_replay_samples (
            incident_id,
            device_name,
            timestamp,
            relative_seconds,
            phase,
            accelerometer_x_g,
            accelerometer_y_g,
            accelerometer_z_g,
            acceleration_magnitude_g,
            gyroscope_x_deg_s,
            gyroscope_y_deg_s,
            gyroscope_z_deg_s,
            gyroscope_magnitude_deg_s
        )
        VALUES %s
        ON CONFLICT (incident_id, timestamp)
        DO NOTHING
        """,
        rows,
        page_size=200,
    )


def append_active_replay(cursor, device_name, processed):
    capture = replay_capture_until.get(device_name)
    if not capture:
        return

    incident_id, event_timestamp, capture_until = capture
    now_timestamp = processed[-1]["timestamp_dt"]

    selected = [
        sample
        for sample in processed
        if sample["timestamp_dt"] <= capture_until
    ]

    insert_replay_rows(
        cursor,
        incident_id,
        device_name,
        event_timestamp,
        selected,
    )

    if now_timestamp >= capture_until:
        replay_capture_until.pop(device_name, None)


def store_pipeline_metric(
    device_name,
    worker_id,
    processed,
    received_samples,
    sensor_rows_saved,
    high_resolution_batch,
    processing_ms,
):
    if not processed:
        return

    batch_duration_s = max(
        0.0,
        processed[-1]["elapsed_time_s"] - processed[0]["elapsed_time_s"],
    )

    observed_cloud_hz = None
    if batch_duration_s > 0 and received_samples > 1:
        observed_cloud_hz = (received_samples - 1) / batch_duration_s

    estimated_local_samples = max(
        received_samples,
        int(round(batch_duration_s * LOCAL_IMU_HZ)) + 1,
    )

    expected_cloud_samples = max(
        1,
        int(round(batch_duration_s * EXPECTED_CLOUD_HZ)) + 1,
    )

    estimated_missing = max(
        0,
        expected_cloud_samples - received_samples,
    )

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pipeline_metrics (
                    device_name,
                    worker_id,
                    timestamp,
                    received_samples,
                    batch_duration_s,
                    observed_cloud_hz,
                    expected_cloud_hz,
                    estimated_local_samples,
                    estimated_missing_cloud_samples,
                    sensor_rows_saved,
                    high_resolution_batch,
                    processing_ms
                )
                VALUES (
                    %s, %s, CURRENT_TIMESTAMP,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    device_name,
                    worker_id,
                    received_samples,
                    batch_duration_s,
                    observed_cloud_hz,
                    EXPECTED_CLOUD_HZ,
                    estimated_local_samples,
                    estimated_missing,
                    sensor_rows_saved,
                    high_resolution_batch,
                    processing_ms,
                ),
            )
        connection.commit()


def edge_event_metadata(
    raw_event_type,
):
    mapping = {
        "FFH": {
            "status": "POSSIBLE_FFH",
            "severity": "CRITICAL",
            "message": (
                "Possible fall from height detected "
                "by the Nesso edge detector."
            ),
        },
        "STF": {
            "status": "POSSIBLE_STF",
            "severity": "URGENT",
            "message": (
                "Possible slip, trip or fall detected "
                "by the Nesso edge detector."
            ),
        },
        "NEAR_MISS": {
            "status": "POSSIBLE_NEAR_MISS",
            "severity": "ATTENTION",
            "message": (
                "Possible near miss detected "
                "by the Nesso edge detector."
            ),
        },
    }

    return mapping[
        raw_event_type
    ]


def remember_edge_event_status(
    device_name,
    metadata,
    confidence,
):
    edge_event_status_hold[
        device_name
    ] = {
        "until": (
            time.time()
            + EDGE_EVENT_STATUS_HOLD_S
        ),
        "status": metadata[
            "status"
        ],
        "message": metadata[
            "message"
        ],
        "confidence": (
            float(confidence)
            / 100.0
        ),
        "severity": metadata[
            "severity"
        ],
    }


def apply_edge_event_status_hold(
    device_name,
    decision,
):
    hold = edge_event_status_hold.get(
        device_name
    )

    if not hold:
        return

    if (
        time.time()
        >=
        hold["until"]
    ):
        edge_event_status_hold.pop(
            device_name,
            None,
        )

        return

    # The edge endpoint has already stored the incident. The hold is only
    # for UI/status persistence and must not create a second incident.
    decision.status = hold[
        "status"
    ]

    decision.message = hold[
        "message"
    ]

    decision.confidence = hold[
        "confidence"
    ]

    decision.severity = hold[
        "severity"
    ]

    decision.is_event = False


def recent_duplicate_event(
    cursor,
    device_name,
    event_type,
):
    cursor.execute(
        """
        SELECT 1
        FROM safety_alerts
        WHERE
            device_name = %s
            AND event_type = %s
            AND received_timestamp
                >= CURRENT_TIMESTAMP
                - (%s * INTERVAL '1 second')
        LIMIT 1
        """,
        (
            device_name,
            event_type,
            ALERT_COOLDOWN_S,
        ),
    )

    return (
        cursor.fetchone()
        is not None
    )


def store_everything(
    batch,
    worker_id,
    processed,
    analysis_samples,
    features,
    decision,
):
    new_incident = None

    sensor_rows, high_resolution_batch = rows_to_store(
        batch.device_name,
        worker_id,
        processed,
    )

    latest = processed[-1]

    with database() as connection:
        with connection.cursor() as cursor:

            append_active_replay(
                cursor,
                batch.device_name,
                processed,
            )

            if sensor_rows:
                execute_values(
                    cursor,
                    """
                    INSERT INTO sensor_data (
                        device_name,
                        worker_id,
                        timestamp,
                        elapsed_time_s,
                        accelerometer_x_g,
                        accelerometer_y_g,
                        accelerometer_z_g,
                        acceleration_magnitude_g,
                        gyroscope_x_deg_s,
                        gyroscope_y_deg_s,
                        gyroscope_z_deg_s,
                        gyroscope_magnitude_deg_s,
                        high_resolution
                    )
                    VALUES %s
                    """,
                    sensor_rows,
                    page_size=100,
                )

            cursor.execute(
                """
                INSERT INTO devices (
                    device_name,
                    worker_id,
                    last_seen,
                    activity_status,
                    status_message,
                    confidence,
                    latest_acceleration_g,
                    latest_gyro_dps,
                    battery_percent,
                    updated_at
                )
                VALUES (
                    %s, %s,
                    CURRENT_TIMESTAMP,
                    %s, %s, %s,
                    %s, %s, %s,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (device_name)
                DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    activity_status = EXCLUDED.activity_status,
                    status_message = EXCLUDED.status_message,
                    confidence = EXCLUDED.confidence,
                    latest_acceleration_g = EXCLUDED.latest_acceleration_g,
                    latest_gyro_dps = EXCLUDED.latest_gyro_dps,
                    battery_percent = COALESCE(
                        EXCLUDED.battery_percent,
                        devices.battery_percent
                    ),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    batch.device_name,
                    worker_id,
                    decision.status,
                    decision.message,
                    decision.confidence,
                    latest["acc_mag"],
                    latest["gyro_mag"],
                    batch.battery_percent,
                ),
            )

            now = time.time()

            if (
                now
                - last_feature_save[
                    batch.device_name
                ]
                >= STATUS_UPDATE_INTERVAL_S
            ):
                cursor.execute(
                    """
                    INSERT INTO feature_windows (
                        device_name,
                        worker_id,
                        window_end,
                        sample_count,
                        duration_s,
                        acc_mean_g,
                        acc_std_g,
                        acc_min_g,
                        acc_peak_g,
                        acc_pp_g,
                        gyro_mean_dps,
                        gyro_max_dps,
                        dominant_frequency_hz,
                        tilt_change_deg,
                        low_g_duration_s,
                        activity_status,
                        confidence
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        batch.device_name,
                        worker_id,
                        processed[-1][
                            "timestamp_dt"
                        ],
                        features.sample_count,
                        features.duration_s,
                        features.acc_mean_g,
                        features.acc_std_g,
                        features.acc_min_g,
                        features.acc_peak_g,
                        features.acc_pp_g,
                        features.gyro_mean_dps,
                        features.gyro_max_dps,
                        features.dominant_frequency_hz,
                        features.tilt_change_deg,
                        features.low_g_duration_s,
                        decision.status,
                        decision.confidence,
                    ),
                )

                last_feature_save[
                    batch.device_name
                ] = now

            if (
                decision.is_event
                and not recent_duplicate_event(
                    cursor,
                    batch.device_name,
                    decision.status,
                )
            ):
                event_timestamp = processed[-1]["timestamp_dt"]
                trigger_details = build_trigger_details(
                    features,
                    decision,
                )

                cursor.execute(
                    """
                    INSERT INTO safety_alerts (
                        worker_id,
                        device_name,
                        received_timestamp,
                        event_type,
                        severity,
                        description,
                        acknowledged,
                        acceleration_peak_g,
                        gyroscope_peak_dps,
                        minimum_acceleration_g,
                        tilt_change_deg,
                        low_g_duration_s,
                        trigger_details
                    )
                    VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        FALSE,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        worker_id,
                        batch.device_name,
                        event_timestamp,
                        decision.status,
                        decision.severity,
                        decision.message,
                        features.acc_peak_g,
                        features.gyro_max_dps,
                        features.acc_min_g,
                        features.tilt_change_deg,
                        features.low_g_duration_s,
                        Json(trigger_details),
                    ),
                )

                incident_id = cursor.fetchone()[0]

                new_incident = {
                    "id": incident_id,
                    "worker_id": worker_id,
                    "device_name": batch.device_name,
                    "event_type": decision.status,
                    "severity": decision.severity,
                    "description": decision.message,
                    "acceleration_peak_g": features.acc_peak_g,
                    "gyroscope_peak_dps": features.gyro_max_dps,
                    "minimum_acceleration_g": features.acc_min_g,
                    "tilt_change_deg": features.tilt_change_deg,
                    "low_g_duration_s": features.low_g_duration_s,
                }

                insert_replay_rows(
                    cursor,
                    incident_id,
                    batch.device_name,
                    event_timestamp,
                    analysis_samples,
                )

                replay_capture_until[
                    batch.device_name
                ] = (
                    incident_id,
                    event_timestamp,
                    event_timestamp + timedelta(
                        seconds=REPLAY_POST_EVENT_S
                    ),
                )

        connection.commit()

    return (
        len(sensor_rows),
        high_resolution_batch,
        new_incident,
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": (
            "dep-nesso-render-supabase"
        ),
    }


@app.post("/api/v1/edge-event")
def ingest_edge_event(
    report: EdgeEventReport,
    x_api_key: str | None = Header(
        default=None
    ),
):
    """
    Receives a completed FFH / STF / near-miss decision directly from
    the 100 Hz Nesso edge detector.

    This route does not replace the cloud feature detector. It gives the
    edge detector a direct path to the incident table and worker status so
    a locally detected fall cannot be hidden by a later COLLECTING/MOVING
    cloud status.
    """

    require_ingest_key(
        x_api_key
    )

    worker_id = resolve_worker(
        report.device_name,
        report.worker_id,
    )

    metadata = edge_event_metadata(
        report.event_type
    )

    confidence_fraction = (
        float(
            report.confidence
        )
        / 100.0
    )

    now_utc = datetime.now(
        timezone.utc
    )

    new_incident = None
    duplicate = False

    trigger_details = {
        "detector_source": "NESSO_EDGE",
        "raw_edge_event": report.event_type,
        "edge_confidence_percent": report.confidence,
        "peak_acceleration_g": report.peak_acceleration_g,
        "peak_gyro_dps": report.peak_gyro_dps,
        "minimum_acceleration_g": report.minimum_acceleration_g,
        "low_g_duration_s": report.low_g_duration_s,
        "detected_elapsed_s": report.detected_elapsed_s,
        "cloud_verification": (
            "The cloud feature detector remains active independently."
        ),
    }

    with database() as connection:
        with connection.cursor() as cursor:
            duplicate = recent_duplicate_event(
                cursor,
                report.device_name,
                metadata["status"],
            )

            cursor.execute(
                """
                INSERT INTO devices (
                    device_name,
                    worker_id,
                    last_seen,
                    activity_status,
                    status_message,
                    confidence,
                    latest_acceleration_g,
                    latest_gyro_dps,
                    battery_percent,
                    updated_at
                )
                VALUES (
                    %s, %s,
                    CURRENT_TIMESTAMP,
                    %s, %s, %s,
                    %s, %s, %s,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (device_name)
                DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    worker_id = EXCLUDED.worker_id,
                    activity_status = EXCLUDED.activity_status,
                    status_message = EXCLUDED.status_message,
                    confidence = EXCLUDED.confidence,
                    latest_acceleration_g = EXCLUDED.latest_acceleration_g,
                    latest_gyro_dps = EXCLUDED.latest_gyro_dps,
                    battery_percent = COALESCE(
                        EXCLUDED.battery_percent,
                        devices.battery_percent
                    ),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    report.device_name,
                    worker_id,
                    metadata["status"],
                    metadata["message"],
                    confidence_fraction,
                    report.peak_acceleration_g,
                    report.peak_gyro_dps,
                    report.battery_percent,
                ),
            )

            if not duplicate:
                cursor.execute(
                    """
                    INSERT INTO safety_alerts (
                        worker_id,
                        device_name,
                        received_timestamp,
                        event_type,
                        severity,
                        description,
                        acknowledged,
                        acceleration_peak_g,
                        gyroscope_peak_dps,
                        minimum_acceleration_g,
                        tilt_change_deg,
                        low_g_duration_s,
                        trigger_details
                    )
                    VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        FALSE,
                        %s, %s, %s, NULL,
                        %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        worker_id,
                        report.device_name,
                        now_utc,
                        metadata["status"],
                        metadata["severity"],
                        metadata["message"],
                        report.peak_acceleration_g,
                        report.peak_gyro_dps,
                        report.minimum_acceleration_g,
                        report.low_g_duration_s,
                        Json(
                            trigger_details
                        ),
                    ),
                )

                incident_id = (
                    cursor.fetchone()[0]
                )

                new_incident = {
                    "id": incident_id,
                    "worker_id": worker_id,
                    "device_name": report.device_name,
                    "event_type": metadata["status"],
                    "severity": metadata["severity"],
                    "description": metadata["message"],
                    "acceleration_peak_g": report.peak_acceleration_g,
                    "gyroscope_peak_dps": report.peak_gyro_dps,
                    "minimum_acceleration_g": report.minimum_acceleration_g,
                    "tilt_change_deg": None,
                    "low_g_duration_s": report.low_g_duration_s,
                }

                # Future high-resolution sensor batches received after the
                # edge event are attached to this incident replay.
                replay_capture_until[
                    report.device_name
                ] = (
                    incident_id,
                    now_utc,
                    now_utc
                    + timedelta(
                        seconds=REPLAY_POST_EVENT_S
                    ),
                )

        connection.commit()

    remember_edge_event_status(
        report.device_name,
        metadata,
        report.confidence,
    )

    if new_incident:
        notify_incident_async(
            new_incident
        )

    return {
        "ok": True,
        "device_name": report.device_name,
        "worker_id": worker_id,
        "activity_status": metadata[
            "status"
        ],
        "duplicate_suppressed": duplicate,
    }


@app.post("/api/v1/sensor/batch")
def ingest_sensor_batch(
    batch: SensorBatch,
    x_api_key: str | None = Header(
        default=None
    ),
):
    request_started = perf_counter()

    require_ingest_key(
        x_api_key
    )

    worker_id = resolve_worker(
        batch.device_name,
        batch.worker_id,
    )

    received_at = datetime.now(
        timezone.utc
    )

    processed = make_processed_samples(
        batch,
        received_at,
    )

    window = update_memory_window(
        batch.device_name,
        processed,
    )

    if (
        len(window) >= 12
        and (
            window[-1]["timestamp"]
            - window[0]["timestamp"]
        )
        >= PROCESS_WINDOW_S * 0.70
    ):
        features = calculate_features(
            window,
            free_fall_threshold_g=FREE_FALL_G,
            impact_threshold_g=IMPACT_G,
        )

        decision = classify(
            features
        )

    else:
        features = calculate_features(
            processed,
            free_fall_threshold_g=FREE_FALL_G,
            impact_threshold_g=IMPACT_G,
        )

        decision = classify(
            features
        )

        decision.status = (
            "COLLECTING"
        )

        decision.message = (
            "Collecting enough live data "
            "for a full movement window."
        )

        decision.confidence = 0.30
        decision.is_event = False
        decision.severity = None

    # If the Nesso itself just reported an edge event, keep that event visible
    # for a short hold period instead of immediately overwriting it with the
    # next normal/collecting cloud classification.
    apply_edge_event_status_hold(
        batch.device_name,
        decision,
    )

    stored_rows, high_resolution_batch, new_incident = store_everything(
        batch,
        worker_id,
        processed,
        window,
        features,
        decision,
    )

    processing_ms = (
        perf_counter() - request_started
    ) * 1000.0

    store_pipeline_metric(
        batch.device_name,
        worker_id,
        processed,
        len(batch.samples),
        stored_rows,
        high_resolution_batch,
        processing_ms,
    )

    if new_incident:
        notify_incident_async(new_incident)

    return {
        "ok": True,
        "device_name": batch.device_name,
        "worker_id": worker_id,
        "received_samples": len(
            batch.samples
        ),
        "database_rows_saved": stored_rows,
        "activity_status": (
            decision.status
        ),
    }


@app.get("/api/v1/dashboard")
def dashboard():
    devices = fetch_all(
        """
        SELECT
            device_id,
            device_name,
            worker_id,
            last_seen,
            activity_status,
            status_message,
            confidence,
            latest_acceleration_g,
            latest_gyro_dps,
            battery_percent,
            CASE
                WHEN
                    last_seen IS NOT NULL
                    AND last_seen
                        > CURRENT_TIMESTAMP
                        - (%s * INTERVAL '1 second')
                THEN 'Active'
                ELSE 'Inactive'
            END AS connection_status
        FROM devices
        ORDER BY worker_id
        """,
        (
            DEVICE_OFFLINE_AFTER_S,
        ),
    )

    for device in devices:
        if (
            device[
                "connection_status"
            ]
            == "Inactive"
        ):
            device[
                "activity_status"
            ] = "OFFLINE"

            device[
                "status_message"
            ] = (
                "Sensor is offline. "
                "No recent live data."
            )

    incidents = fetch_all(
        """
        SELECT
            id,
            worker_id,
            device_name,
            received_timestamp,
            event_type,
            severity,
            description,
            acknowledged,
            acceleration_peak_g,
            gyroscope_peak_dps,
            minimum_acceleration_g,
            tilt_change_deg,
            low_g_duration_s,
            trigger_details,
            feedback_label,
            feedback_notes,
            feedback_at,
            acknowledged_at
        FROM safety_alerts
        ORDER BY id DESC
        LIMIT 100
        """
    )

    last_live_update = max(
        (
            device["last_seen"]
            for device in devices
            if device["last_seen"]
            is not None
        ),
        default=None,
    )

    return {
        "devices": devices,
        "incidents": incidents,
        "summary": {
            "registered_devices": len(
                devices
            ),
            "workers_online": sum(
                1
                for device in devices
                if device[
                    "connection_status"
                ]
                == "Active"
            ),
            "open_incidents": sum(
                1
                for incident in incidents
                if not incident[
                    "acknowledged"
                ]
            ),
            "last_live_update": (
                last_live_update
            ),
        },
    }


@app.get(
    "/api/v1/devices/{device_name}/sensor"
)
def device_sensor(
    device_name: str,
    limit: int = Query(
        default=600,
        ge=10,
        le=3000,
    ),
):
    rows = fetch_all(
        """
        SELECT *
        FROM (
            SELECT
                id,
                device_name,
                worker_id,
                timestamp,
                elapsed_time_s,
                accelerometer_x_g,
                accelerometer_y_g,
                accelerometer_z_g,
                acceleration_magnitude_g,
                gyroscope_x_deg_s,
                gyroscope_y_deg_s,
                gyroscope_z_deg_s,
                gyroscope_magnitude_deg_s,
                high_resolution
            FROM sensor_data
            WHERE device_name = %s
            ORDER BY id DESC
            LIMIT %s
        ) recent
        ORDER BY id ASC
        """,
        (
            device_name,
            limit,
        ),
    )

    return {
        "device_name": device_name,
        "rows": rows,
    }


@app.get(
    "/api/v1/devices/{device_name}/features"
)
def device_features(
    device_name: str,
    limit: int = Query(
        default=300,
        ge=10,
        le=2000,
    ),
):
    rows = fetch_all(
        """
        SELECT *
        FROM (
            SELECT
                id,
                device_name,
                worker_id,
                window_end,
                sample_count,
                duration_s,
                acc_mean_g,
                acc_std_g,
                acc_min_g,
                acc_peak_g,
                acc_pp_g,
                gyro_mean_dps,
                gyro_max_dps,
                dominant_frequency_hz,
                tilt_change_deg,
                low_g_duration_s,
                activity_status,
                confidence
            FROM feature_windows
            WHERE device_name = %s
            ORDER BY id DESC
            LIMIT %s
        ) recent
        ORDER BY id ASC
        """,
        (
            device_name,
            limit,
        ),
    )

    return {
        "device_name": device_name,
        "rows": rows,
    }


@app.get("/api/v1/incidents")
def incidents(
    device_name: str | None = None,
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
):
    if device_name:
        rows = fetch_all(
            """
            SELECT *
            FROM safety_alerts
            WHERE device_name = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                device_name,
                limit,
            ),
        )

    else:
        rows = fetch_all(
            """
            SELECT *
            FROM safety_alerts
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )

    return {
        "incidents": rows
    }


@app.post(
    "/api/v1/incidents/{incident_id}/acknowledge"
)
def acknowledge_incident(
    incident_id: int,
    x_admin_password: str | None = Header(
        default=None
    ),
):
    require_admin_password(
        x_admin_password
    )

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE safety_alerts
                SET
                    acknowledged = TRUE,
                    acknowledged_at = COALESCE(
                        acknowledged_at,
                        CURRENT_TIMESTAMP
                    )
                WHERE id = %s
                """,
                (
                    incident_id,
                ),
            )

            changed = (
                cursor.rowcount
            )

        connection.commit()

    if changed == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                "Incident not found."
            ),
        )

    return {
        "ok": True
    }


@app.get("/api/v1/incidents/{incident_id}/replay")
def incident_replay(incident_id: int):
    incident_rows = fetch_all(
        """
        SELECT *
        FROM safety_alerts
        WHERE id = %s
        """,
        (incident_id,),
    )

    if not incident_rows:
        raise HTTPException(
            status_code=404,
            detail="Incident not found.",
        )

    rows = fetch_all(
        """
        SELECT
            id,
            incident_id,
            device_name,
            timestamp,
            relative_seconds,
            phase,
            accelerometer_x_g,
            accelerometer_y_g,
            accelerometer_z_g,
            acceleration_magnitude_g,
            gyroscope_x_deg_s,
            gyroscope_y_deg_s,
            gyroscope_z_deg_s,
            gyroscope_magnitude_deg_s
        FROM incident_replay_samples
        WHERE incident_id = %s
        ORDER BY timestamp ASC
        """,
        (incident_id,),
    )

    return {
        "incident": incident_rows[0],
        "rows": rows,
        "capture": {
            "pre_event_seconds": PROCESS_WINDOW_S,
            "post_event_seconds": REPLAY_POST_EVENT_S,
        },
    }


@app.post("/api/v1/incidents/{incident_id}/feedback")
def incident_feedback(
    incident_id: int,
    body: IncidentFeedback,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)

    notes = body.notes.strip()

    actual_event_type = (
        body.actual_event_type
        if body.label == "ACTUAL_EVENT"
        else None
    )

    # Backwards-compatible fallback: if an older client confirms an event
    # without choosing the true class, infer it from the alert that was reviewed.
    if (
        body.label == "ACTUAL_EVENT"
        and actual_event_type is None
    ):
        rows = fetch_all(
            """
            SELECT event_type
            FROM safety_alerts
            WHERE id = %s
            """,
            (incident_id,),
        )

        if not rows:
            raise HTTPException(
                status_code=404,
                detail="Incident not found.",
            )

        mapping = {
            "POSSIBLE_FFH": "FFH",
            "POSSIBLE_STF": "STF",
            "POSSIBLE_NEAR_MISS": "NEAR_MISS",
        }

        actual_event_type = mapping.get(
            rows[0]["event_type"]
        )

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE safety_alerts
                SET
                    feedback_label = %s,
                    actual_event_type = %s,
                    feedback_notes = %s,
                    feedback_at = CURRENT_TIMESTAMP,
                    acknowledged = TRUE,
                    acknowledged_at = COALESCE(
                        acknowledged_at,
                        CURRENT_TIMESTAMP
                    )
                WHERE id = %s
                """,
                (
                    body.label,
                    actual_event_type,
                    notes,
                    incident_id,
                ),
            )
            changed = cursor.rowcount
        connection.commit()

    if changed == 0:
        raise HTTPException(
            status_code=404,
            detail="Incident not found.",
        )

    return {
        "ok": True,
        "actual_event_type": actual_event_type,
    }


@app.get("/api/v1/system/quality")
def system_quality(
    hours: int = Query(default=24, ge=1, le=168),
):
    rows = fetch_all(
        """
        SELECT
            device_name,
            MAX(worker_id) AS worker_id,
            COUNT(*) AS batch_count,
            SUM(received_samples) AS cloud_samples_received,
            SUM(estimated_local_samples) AS estimated_local_samples,
            SUM(estimated_missing_cloud_samples) AS estimated_missing_cloud_samples,
            SUM(sensor_rows_saved) AS sensor_rows_saved,
            AVG(observed_cloud_hz) AS observed_cloud_hz,
            AVG(processing_ms) AS processing_ms,
            SUM(CASE WHEN high_resolution_batch THEN 1 ELSE 0 END) AS high_resolution_batches,
            MAX(timestamp) AS last_batch
        FROM pipeline_metrics
        WHERE timestamp >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
        GROUP BY device_name
        ORDER BY device_name
        """,
        (hours,),
    )

    total_batches = sum(int(row["batch_count"] or 0) for row in rows)
    cloud_received = sum(int(row["cloud_samples_received"] or 0) for row in rows)
    missing = sum(int(row["estimated_missing_cloud_samples"] or 0) for row in rows)
    stored = sum(int(row["sensor_rows_saved"] or 0) for row in rows)
    local_est = sum(int(row["estimated_local_samples"] or 0) for row in rows)

    observed_values = [
        float(row["observed_cloud_hz"])
        for row in rows
        if row["observed_cloud_hz"] is not None
    ]
    processing_values = [
        float(row["processing_ms"])
        for row in rows
        if row["processing_ms"] is not None
    ]

    expected_cloud_total = cloud_received + missing
    delivery_pct = (
        (cloud_received / expected_cloud_total) * 100.0
        if expected_cloud_total > 0
        else None
    )

    return {
        "hours": hours,
        "summary": {
            "batch_count": total_batches,
            "cloud_samples_received": cloud_received,
            "estimated_local_samples": local_est,
            "estimated_missing_cloud_samples": missing,
            "sensor_rows_saved": stored,
            "average_observed_cloud_hz": (
                sum(observed_values) / len(observed_values)
                if observed_values
                else None
            ),
            "average_processing_ms": (
                sum(processing_values) / len(processing_values)
                if processing_values
                else None
            ),
            "estimated_delivery_pct": delivery_pct,
        },
        "devices": rows,
    }


@app.get("/api/v1/system/storage")
def system_storage(
    hours: int = Query(default=24, ge=1, le=168),
):
    rows = fetch_all(
        """
        SELECT
            device_name,
            MAX(worker_id) AS worker_id,
            SUM(estimated_local_samples) AS estimated_local_samples,
            SUM(received_samples) AS cloud_samples_received,
            SUM(sensor_rows_saved) AS sensor_rows_saved,
            SUM(CASE WHEN high_resolution_batch THEN received_samples ELSE 0 END) AS abnormal_cloud_samples
        FROM pipeline_metrics
        WHERE timestamp >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
        GROUP BY device_name
        ORDER BY device_name
        """,
        (hours,),
    )

    local_est = sum(int(row["estimated_local_samples"] or 0) for row in rows)
    cloud = sum(int(row["cloud_samples_received"] or 0) for row in rows)
    stored = sum(int(row["sensor_rows_saved"] or 0) for row in rows)

    def reduction(before, after):
        if before <= 0:
            return None
        return max(0.0, (1.0 - (after / before)) * 100.0)

    return {
        "hours": hours,
        "summary": {
            "estimated_local_samples": local_est,
            "cloud_samples_received": cloud,
            "sensor_rows_saved": stored,
            "local_to_cloud_reduction_pct": reduction(local_est, cloud),
            "cloud_to_database_reduction_pct": reduction(cloud, stored),
            "estimated_end_to_end_reduction_pct": reduction(local_est, stored),
        },
        "devices": rows,
    }


@app.get("/api/v1/system/verification")
def system_verification():
    rows = fetch_all(
        """
        SELECT
            COUNT(*) AS total_incidents,
            COUNT(*) FILTER (WHERE feedback_label = 'ACTUAL_EVENT') AS actual_events,
            COUNT(*) FILTER (WHERE feedback_label = 'FALSE_ALARM') AS false_alarms,
            COUNT(*) FILTER (WHERE feedback_label = 'UNSURE') AS unsure,
            COUNT(*) FILTER (WHERE feedback_label IS NULL) AS unreviewed
        FROM safety_alerts
        """
    )

    summary = rows[0] if rows else {}
    actual = int(summary.get("actual_events") or 0)
    false = int(summary.get("false_alarms") or 0)
    reviewed_binary = actual + false

    verified_detection_rate = (
        actual / reviewed_binary * 100.0
        if reviewed_binary > 0
        else None
    )

    return {
        "summary": {
            **summary,
            "verified_detection_rate_pct": verified_detection_rate,
        },
        "note": (
            "This is human incident verification, not ML accuracy. "
            "It only measures reviewed alerts and cannot measure false negatives."
        ),
    }



@app.get("/api/v1/threshold-learning/status")
def threshold_learning_status_endpoint():
    return threshold_learning_status()


@app.post("/api/v1/threshold-learning/train")
def threshold_learning_train_endpoint(
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)

    result = train_threshold_recommendation()

    if not result.get("trained"):
        return result

    return result


@app.get("/api/v1/notifications/status")
def notification_status():
    settings = get_notification_settings()
    config = telegram_config()
    recent = recent_notification_log(limit=30)

    return {
        "telegram": {
            **config,
            "enabled": bool(settings.get("telegram_enabled", True)),
        },
        "settings": settings,
        "recent": recent,
        "priority_rules": {
            "NORMAL": "Silent Telegram update: near miss, device offline/restored, low battery.",
            "URGENT": "Audible Telegram alert: possible slip/trip/fall (STF).",
            "CRITICAL": "Audible Telegram alert: possible fall from height (FFH), repeated if unacknowledged.",
        },
    }


@app.get("/api/v1/notifications/recipients")
def notification_recipients(
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    return {
        "recipients": list_recipients(include_inactive=True),
    }


@app.post("/api/v1/notifications/pairings")
def notification_pairing_create(
    body: TelegramPairingCreate,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    try:
        pairing = create_pairing(**body.model_dump())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "pairing": pairing}


@app.delete("/api/v1/notifications/pairings/{code}")
def notification_pairing_cancel(
    code: str,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    return {"ok": cancel_pairing(code)}


@app.post("/api/v1/notifications/recipients/{recipient_id}")
def notification_recipient_update(
    recipient_id: int,
    body: TelegramRecipientUpdate,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    recipient = update_recipient(recipient_id, **body.model_dump())
    if not recipient:
        raise HTTPException(status_code=404, detail="Telegram recipient not found.")
    return {"ok": True, "recipient": recipient}


@app.delete("/api/v1/notifications/recipients/{recipient_id}")
def notification_recipient_delete(
    recipient_id: int,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    if not remove_recipient(recipient_id):
        raise HTTPException(status_code=404, detail="Telegram recipient not found.")
    return {"ok": True}


@app.post("/api/v1/notifications/test")
def notification_test(
    body: NotificationTest,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    return send_test_notification_async(body.priority, recipient_id=body.recipient_id)


@app.post("/api/v1/notifications/settings")
def notification_settings_update(
    body: NotificationSettingsUpdate,
    x_admin_password: str | None = Header(default=None),
):
    require_admin_password(x_admin_password)
    settings = update_notification_settings(body.model_dump())
    return {"ok": True, "settings": settings}


@app.post(
    "/api/v1/devices/{device_name}/assign"
)
def assign_worker(
    device_name: str,
    body: WorkerAssignment,
    x_admin_password: str | None = Header(
        default=None
    ),
):
    require_admin_password(
        x_admin_password
    )

    worker_id = (
        body.worker_id.strip()
    )

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE devices
                SET
                    worker_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE device_name = %s
                """,
                (
                    worker_id,
                    device_name,
                ),
            )

            changed = (
                cursor.rowcount
            )

            cursor.execute(
                """
                UPDATE sensor_data
                SET worker_id = %s
                WHERE
                    device_name = %s
                    AND timestamp
                    >= CURRENT_TIMESTAMP
                    - INTERVAL '1 day'
                """,
                (
                    worker_id,
                    device_name,
                ),
            )

        connection.commit()

    if changed == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                "Device not found."
            ),
        )

    return {
        "ok": True
    }


@app.get("/")
def website():
    return FileResponse(
        BASE_DIR / "index.html"
    )


@app.get("/styles.css")
def styles():
    return FileResponse(
        BASE_DIR / "styles.css",
        media_type="text/css",
    )


@app.get("/app.js")
def javascript():
    return FileResponse(
        BASE_DIR / "app.js",
        media_type=(
            "application/javascript"
        ),
    )
