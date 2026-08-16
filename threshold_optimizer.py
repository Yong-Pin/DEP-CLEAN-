from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
import csv
import hashlib
import io
import math

import numpy as np
from psycopg2.extras import Json, RealDictCursor

from db import database
from thresholds import (
    FFH_GYRO_DPS,
    FREE_FALL_G,
    FREE_FALL_MIN_DURATION_S,
    IMPACT_G,
    MIN_TILT_CHANGE_DEG,
    NEAR_MISS_ACC_PP_G,
    NEAR_MISS_GYRO_DPS,
    STF_GYRO_DPS,
)

try:
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        classification_report,
        confusion_matrix,
    )
    from sklearn.model_selection import (
        GridSearchCV,
        StratifiedKFold,
        train_test_split,
    )
    from sklearn.tree import DecisionTreeClassifier
except ImportError as exc:
    raise RuntimeError(
        "scikit-learn is required for threshold learning. "
        "Add scikit-learn to requirements.txt."
    ) from exc


FEATURES = [
    {
        "key": "acc_peak_g",
        "label": "Acceleration peak",
        "unit": "g",
    },
    {
        "key": "gyro_max_dps",
        "label": "Gyroscope peak",
        "unit": "°/s",
    },
    {
        "key": "acc_min_g",
        "label": "Minimum acceleration",
        "unit": "g",
    },
    {
        "key": "tilt_change_deg",
        "label": "Tilt change",
        "unit": "°",
    },
    {
        "key": "low_g_duration_s",
        "label": "Low-g duration",
        "unit": "s",
    },
    {
        "key": "acc_pp_g",
        "label": "Acceleration peak-to-peak",
        "unit": "g",
    },
]

FEATURE_KEYS = [
    item["key"]
    for item in FEATURES
]

CLASS_LABELS = [
    "NO_FALL",
    "FALL",
]

MIN_REVIEWED_LABELS = 5
MAX_AUTO_NORMAL_ROWS = 200
MAX_IMPORTED_NO_FALL_ROWS = 120
IMPORT_NO_FALL_WINDOW_S = 1.20
IMPORT_NO_FALL_STEP_S = 2.50


def current_thresholds():
    return [
        {
            "key": "free_fall_g",
            "label": "Free-fall acceleration",
            "comparison": "<=",
            "value": FREE_FALL_G,
            "unit": "g",
        },
        {
            "key": "free_fall_duration",
            "label": "Minimum low-g duration",
            "comparison": ">=",
            "value": FREE_FALL_MIN_DURATION_S,
            "unit": "s",
        },
        {
            "key": "impact_g",
            "label": "Impact acceleration",
            "comparison": ">=",
            "value": IMPACT_G,
            "unit": "g",
        },
        {
            "key": "ffh_gyro",
            "label": "FFH gyroscope",
            "comparison": ">=",
            "value": FFH_GYRO_DPS,
            "unit": "°/s",
        },
        {
            "key": "stf_gyro",
            "label": "STF gyroscope",
            "comparison": ">=",
            "value": STF_GYRO_DPS,
            "unit": "°/s",
        },
        {
            "key": "near_miss_gyro",
            "label": "Near-miss gyroscope",
            "comparison": ">=",
            "value": NEAR_MISS_GYRO_DPS,
            "unit": "°/s",
        },
        {
            "key": "near_miss_acc_pp",
            "label": "Near-miss acceleration variation",
            "comparison": ">=",
            "value": NEAR_MISS_ACC_PP_G,
            "unit": "g",
        },
        {
            "key": "tilt",
            "label": "Tilt change",
            "comparison": ">=",
            "value": MIN_TILT_CHANGE_DEG,
            "unit": "°",
        },
    ]


def _map_predicted_event(value):
    """
    Convert the live multi-event detector into the binary class used by
    Threshold Lab.

    FFH / STF -> FALL
    Near miss / ordinary activity -> NO_FALL
    """

    mapping = {
        "POSSIBLE_FFH": "FALL",
        "FFH": "FALL",
        "POSSIBLE_STF": "FALL",
        "STF": "FALL",
        "POSSIBLE_NEAR_MISS": "NO_FALL",
        "NEAR_MISS": "NO_FALL",
        "STANDING": "NO_FALL",
        "WALKING": "NO_FALL",
        "RUNNING": "NO_FALL",
        "MOVING": "NO_FALL",
        "COLLECTING": "NO_FALL",
        "NORMAL": "NO_FALL",
    }

    return mapping.get(
        str(value).strip().upper()
        if value is not None
        else "",
        "NO_FALL",
    )


def _normalise_actual_type(value):
    """
    Human review still records the detailed event type, but the Threshold Lab
    deliberately reduces it to FALL / NO_FALL.
    """

    if not value:
        return None

    value = str(value).strip().upper()

    mapping = {
        "POSSIBLE_FFH": "FALL",
        "FFH": "FALL",
        "POSSIBLE_STF": "FALL",
        "STF": "FALL",
        "POSSIBLE_NEAR_MISS": "NO_FALL",
        "NEAR_MISS": "NO_FALL",
        "NORMAL": "NO_FALL",
        "NO_FALL": "NO_FALL",
        "FALL": "FALL",
    }

    return mapping.get(value)


def _reviewed_rows():
    """
    Use human-reviewed incidents as the trusted labelled event examples.

    FALSE_ALARM -> true class NO_FALL.
    ACTUAL_EVENT -> actual_event_type when supplied; otherwise fall back to
                    the originally detected event type for legacy reviews.

    A nearby feature window is used so the Decision Tree receives the same
    engineering features that the live cloud detector already calculates.
    """

    query = """
        SELECT
            a.id AS source_id,
            a.feedback_label,
            a.actual_event_type,
            a.event_type,
            a.device_name,
            a.worker_id,
            a.received_timestamp,
            COALESCE(
                f.acc_peak_g,
                a.acceleration_peak_g
            ) AS acc_peak_g,
            COALESCE(
                f.gyro_max_dps,
                a.gyroscope_peak_dps
            ) AS gyro_max_dps,
            COALESCE(
                f.acc_min_g,
                a.minimum_acceleration_g
            ) AS acc_min_g,
            COALESCE(
                f.tilt_change_deg,
                a.tilt_change_deg
            ) AS tilt_change_deg,
            COALESCE(
                f.low_g_duration_s,
                a.low_g_duration_s
            ) AS low_g_duration_s,
            f.acc_pp_g,
            COALESCE(
                f.activity_status,
                a.event_type
            ) AS predicted_status
        FROM safety_alerts a
        LEFT JOIN LATERAL (
            SELECT
                fw.acc_peak_g,
                fw.gyro_max_dps,
                fw.acc_min_g,
                fw.tilt_change_deg,
                fw.low_g_duration_s,
                fw.acc_pp_g,
                fw.activity_status
            FROM feature_windows fw
            WHERE
                fw.device_name = a.device_name
                AND fw.window_end
                    BETWEEN
                        a.received_timestamp
                            - INTERVAL '6 seconds'
                        AND
                        a.received_timestamp
                            + INTERVAL '6 seconds'
            ORDER BY
                ABS(
                    EXTRACT(
                        EPOCH FROM (
                            fw.window_end
                            - a.received_timestamp
                        )
                    )
                )
            LIMIT 1
        ) f ON TRUE
        WHERE
            a.feedback_label IN (
                'ACTUAL_EVENT',
                'FALSE_ALARM'
            )
        ORDER BY a.id DESC
    """

    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()

    result = []

    for row in rows:
        feedback = row["feedback_label"]

        if feedback == "FALSE_ALARM":
            true_label = "NO_FALL"

        else:
            true_label = _normalise_actual_type(
                row.get(
                    "actual_event_type"
                )
            )

            if true_label is None:
                true_label = _map_predicted_event(
                    row.get(
                        "event_type"
                    )
                )

        if true_label not in CLASS_LABELS:
            continue

        features = {}

        valid = True

        for key in FEATURE_KEYS:
            value = row.get(key)

            if value is None:
                valid = False
                break

            try:
                features[key] = float(value)
            except (TypeError, ValueError):
                valid = False
                break

        if not valid:
            continue

        result.append(
            {
                "source": "HUMAN_REVIEW",
                "source_id": int(
                    row["source_id"]
                ),
                "true_label": true_label,
                "predicted_label": _map_predicted_event(
                    row.get(
                        "predicted_status"
                    )
                ),
                **features,
            }
        )

    return result


def _auto_normal_rows(limit):
    """
    Add background NO_FALL windows so the tree can see ordinary movement too.

    These are not human labels. They are intentionally kept separate in the
    returned metadata and are excluded from event-class label counts.

    A window is only selected when:
    - the live rule detector called it a normal activity, and
    - there was no recorded incident for that device within +/- 10 seconds.
    """

    query = """
        SELECT
            fw.id AS source_id,
            fw.acc_peak_g,
            fw.gyro_max_dps,
            fw.acc_min_g,
            fw.tilt_change_deg,
            fw.low_g_duration_s,
            fw.acc_pp_g,
            fw.activity_status
        FROM feature_windows fw
        WHERE
            fw.activity_status IN (
                'STANDING',
                'WALKING',
                'RUNNING',
                'MOVING'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM safety_alerts a
                WHERE
                    a.device_name = fw.device_name
                    AND a.received_timestamp
                        BETWEEN
                            fw.window_end
                                - INTERVAL '10 seconds'
                            AND
                            fw.window_end
                                + INTERVAL '10 seconds'
            )
        ORDER BY fw.id DESC
        LIMIT %s
    """

    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                query,
                (
                    limit,
                ),
            )
            rows = cursor.fetchall()

    result = []

    for row in rows:
        features = {}

        valid = True

        for key in FEATURE_KEYS:
            value = row.get(key)

            if value is None:
                valid = False
                break

            try:
                features[key] = float(value)
            except (TypeError, ValueError):
                valid = False
                break

        if not valid:
            continue

        result.append(
            {
                "source": "AUTO_NORMAL",
                "source_id": int(
                    row["source_id"]
                ),
                "true_label": "NO_FALL",
                "predicted_label": "NO_FALL",
                **features,
            }
        )

    return result



def _baseline_label_for_features(features):
    """
    Approximate the current binary FALL / NO_FALL rule for imported historical
    samples. This is only used for the "current rule system" comparison.
    """

    ffh_like = (
        float(features["acc_min_g"]) <= FREE_FALL_G
        and float(features["low_g_duration_s"]) >= FREE_FALL_MIN_DURATION_S
        and float(features["acc_peak_g"]) >= IMPACT_G
        and float(features["gyro_max_dps"]) >= FFH_GYRO_DPS
    )

    stf_like = (
        float(features["acc_peak_g"]) >= IMPACT_G
        and float(features["gyro_max_dps"]) >= STF_GYRO_DPS
        and float(features["tilt_change_deg"]) >= MIN_TILT_CHANGE_DEG
    )

    return "FALL" if (ffh_like or stf_like) else "NO_FALL"


def _parse_timestamp(value):
    value = str(value or "").strip()

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    # Treat naive timestamps as UTC only for duration calculations.
    # Relative timing is what matters for feature extraction.
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed


def _csv_float(row, key):
    try:
        return float(
            str(
                row.get(key, "")
            ).strip()
        )
    except (TypeError, ValueError):
        return None


def _normalise_csv_label(value):
    value = (
        str(value or "")
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )

    if value in {
        "FALL",
        "FFH",
        "STF",
        "POSSIBLE_FFH",
        "POSSIBLE_STF",
    }:
        return "FALL"

    if value in {
        "NO_FALL",
        "NOFALL",
        "NORMAL",
        "NEAR_MISS",
        "POSSIBLE_NEAR_MISS",
    }:
        return "NO_FALL"

    return None


def _vector_magnitude(x, y, z):
    return math.sqrt(
        x * x
        + y * y
        + z * z
    )


def _tilt_change_from_rows(rows):
    if len(rows) < 6:
        return 0.0

    count = max(
        3,
        int(
            len(rows) * 0.15
        ),
    )

    first = rows[:count]
    last = rows[-count:]

    def average_vector(items):
        return [
            sum(
                item[key]
                for item in items
            )
            / len(items)
            for key in (
                "ax",
                "ay",
                "az",
            )
        ]

    start = average_vector(first)
    end = average_vector(last)

    start_mag = _vector_magnitude(
        *start
    )
    end_mag = _vector_magnitude(
        *end
    )

    denominator = (
        start_mag
        * end_mag
    )

    if denominator <= 1e-9:
        return 0.0

    dot = sum(
        a * b
        for a, b
        in zip(
            start,
            end,
        )
    )

    cosine = max(
        -1.0,
        min(
            1.0,
            dot / denominator,
        ),
    )

    return math.degrees(
        math.acos(cosine)
    )


def _longest_low_g_duration(rows):
    if len(rows) < 2:
        return 0.0

    longest = 0.0
    started_at = None
    last_below = None

    for row in rows:
        if (
            row["acc_mag_g"]
            <= FREE_FALL_G
        ):
            if started_at is None:
                started_at = row[
                    "timestamp"
                ]

            last_below = row[
                "timestamp"
            ]

        else:
            if (
                started_at is not None
                and last_below is not None
            ):
                longest = max(
                    longest,
                    (
                        last_below
                        - started_at
                    ).total_seconds(),
                )

            started_at = None
            last_below = None

    if (
        started_at is not None
        and last_below is not None
    ):
        longest = max(
            longest,
            (
                last_below
                - started_at
            ).total_seconds(),
        )

    return max(
        0.0,
        float(longest),
    )


def _features_from_import_rows(rows):
    if len(rows) < 5:
        return None

    rows = sorted(
        rows,
        key=lambda item:
            item["timestamp"],
    )

    accelerations = [
        row["acc_mag_g"]
        for row in rows
    ]

    rotations = [
        row["gyro_mag_dps"]
        for row in rows
    ]

    acc_peak = max(
        accelerations
    )

    acc_min = min(
        accelerations
    )

    return {
        "acc_peak_g": float(
            acc_peak
        ),
        "gyro_max_dps": float(
            max(rotations)
        ),
        "acc_min_g": float(
            acc_min
        ),
        "tilt_change_deg": float(
            _tilt_change_from_rows(
                rows
            )
        ),
        "low_g_duration_s": float(
            _longest_low_g_duration(
                rows
            )
        ),
        "acc_pp_g": float(
            acc_peak
            - acc_min
        ),
    }


def parse_labelled_csv(
    csv_text,
    filename="historical.csv",
):
    """
    Convert a row-level marked IMU CSV into event-level FALL / NO_FALL samples.

    Accepted label headers:
      label
      Fall_Label

    FALL rows:
      one training sample per Event_ID when Event_ID exists.

    NO_FALL rows:
      non-overlapping-ish 1.2 s windows sampled every 2.5 s.
    """

    if not isinstance(
        csv_text,
        str,
    ):
        raise ValueError(
            "CSV text is required."
        )

    if len(csv_text) > 8_000_000:
        raise ValueError(
            "CSV is too large. Keep each import below 8 MB."
        )

    reader = csv.DictReader(
        io.StringIO(
            csv_text
        )
    )

    fieldnames = [
        str(name or "")
        .strip()
        .lstrip("\ufeff")
        for name
        in (
            reader.fieldnames
            or []
        )
    ]

    required = {
        "Timestamp",
        "Accelerometer_X_g",
        "Accelerometer_Y_g",
        "Accelerometer_Z_g",
        "Gyroscope_X_deg_s",
        "Gyroscope_Y_deg_s",
        "Gyroscope_Z_deg_s",
    }

    missing = sorted(
        required
        - set(fieldnames)
    )

    if missing:
        raise ValueError(
            "CSV is missing required columns: "
            + ", ".join(
                missing
            )
        )

    label_key = (
        "label"
        if "label" in fieldnames
        else (
            "Fall_Label"
            if "Fall_Label"
            in fieldnames
            else None
        )
    )

    if label_key is None:
        raise ValueError(
            "CSV needs a 'label' or 'Fall_Label' column."
        )

    rows = []

    for raw in reader:
        normalised = {
            str(key or "")
            .strip()
            .lstrip("\ufeff"):
                value
            for key, value
            in raw.items()
        }

        timestamp = _parse_timestamp(
            normalised.get(
                "Timestamp"
            )
        )

        label = _normalise_csv_label(
            normalised.get(
                label_key
            )
        )

        values = {
            "ax": _csv_float(
                normalised,
                "Accelerometer_X_g",
            ),
            "ay": _csv_float(
                normalised,
                "Accelerometer_Y_g",
            ),
            "az": _csv_float(
                normalised,
                "Accelerometer_Z_g",
            ),
            "gx": _csv_float(
                normalised,
                "Gyroscope_X_deg_s",
            ),
            "gy": _csv_float(
                normalised,
                "Gyroscope_Y_deg_s",
            ),
            "gz": _csv_float(
                normalised,
                "Gyroscope_Z_deg_s",
            ),
        }

        if (
            timestamp is None
            or label is None
            or any(
                value is None
                for value
                in values.values()
            )
        ):
            continue

        event_id = str(
            normalised.get(
                "Event_ID",
                "",
            )
            or ""
        ).strip()

        acc_mag = _vector_magnitude(
            values["ax"],
            values["ay"],
            values["az"],
        )

        gyro_mag = _vector_magnitude(
            values["gx"],
            values["gy"],
            values["gz"],
        )

        rows.append(
            {
                "timestamp": timestamp,
                "label": label,
                "event_id": event_id,
                "acc_mag_g": acc_mag,
                "gyro_mag_dps": gyro_mag,
                **values,
            }
        )

    if not rows:
        raise ValueError(
            "No valid labelled sensor rows were found."
        )

    rows.sort(
        key=lambda item:
            item["timestamp"]
    )

    # Remove exact duplicate sensor records.
    unique_rows = []
    seen = set()

    for row in rows:
        key = (
            row["timestamp"],
            row["label"],
            row["event_id"],
            round(
                row["ax"],
                6,
            ),
            round(
                row["ay"],
                6,
            ),
            round(
                row["az"],
                6,
            ),
            round(
                row["gx"],
                6,
            ),
            round(
                row["gy"],
                6,
            ),
            round(
                row["gz"],
                6,
            ),
        )

        if key in seen:
            continue

        seen.add(key)
        unique_rows.append(
            row
        )

    rows = unique_rows

    fall_rows = [
        row
        for row in rows
        if row["label"]
            ==
            "FALL"
    ]

    fall_groups = {}

    # Prefer Event_ID because the marked files already contain it.
    event_ids = {
        row["event_id"]
        for row in fall_rows
        if row["event_id"]
    }

    if event_ids:
        for event_id in sorted(
            event_ids
        ):
            group = [
                row
                for row in fall_rows
                if row["event_id"]
                    ==
                    event_id
            ]

            if group:
                fall_groups[
                    f"FALL_{event_id}"
                ] = group

    else:
        # Fallback: split FALL rows whenever there is a >1.5 s gap.
        group_number = 0
        previous_time = None

        for row in fall_rows:
            if (
                previous_time is None
                or (
                    row["timestamp"]
                    - previous_time
                ).total_seconds()
                > 1.5
            ):
                group_number += 1

            fall_groups.setdefault(
                f"FALL_{group_number}",
                [],
            ).append(
                row
            )

            previous_time = row[
                "timestamp"
            ]

    extracted = []

    for source_key, group in fall_groups.items():
        features = _features_from_import_rows(
            group
        )

        if features is None:
            continue

        extracted.append(
            {
                "source_event_key": source_key,
                "true_label": "FALL",
                **features,
            }
        )

    no_fall_rows = [
        row
        for row in rows
        if row["label"]
            ==
            "NO_FALL"
    ]

    no_fall_candidates = []

    if no_fall_rows:
        start_time = no_fall_rows[
            0
        ]["timestamp"]

        end_time = no_fall_rows[
            -1
        ]["timestamp"]

        centre = (
            start_time
            + timedelta(
                seconds=
                    IMPORT_NO_FALL_WINDOW_S
                    / 2.0
            )
        )

        half_window = (
            IMPORT_NO_FALL_WINDOW_S
            / 2.0
        )

        while (
            centre
            <=
            end_time
        ):
            window = [
                row
                for row in no_fall_rows
                if abs(
                    (
                        row["timestamp"]
                        - centre
                    ).total_seconds()
                )
                <=
                half_window
            ]

            # Do not create a NO_FALL window that overlaps any FALL row.
            overlaps_fall = any(
                abs(
                    (
                        row["timestamp"]
                        - centre
                    ).total_seconds()
                )
                <=
                half_window
                for row
                in fall_rows
            )

            if (
                len(window) >= 5
                and not overlaps_fall
            ):
                features = _features_from_import_rows(
                    window
                )

                if features is not None:
                    no_fall_candidates.append(
                        {
                            "source_event_key": (
                                "NOFALL_"
                                + centre.isoformat()
                            ),
                            "true_label": "NO_FALL",
                            **features,
                        }
                    )

            centre += timedelta(
                seconds=
                    IMPORT_NO_FALL_STEP_S
            )

    # Keep NO_FALL examples useful but bounded.
    max_no_fall = min(
        MAX_IMPORTED_NO_FALL_ROWS,
        max(
            20,
            len(extracted)
            * 3,
        ),
    )

    if (
        len(no_fall_candidates)
        >
        max_no_fall
    ):
        indexes = np.linspace(
            0,
            len(no_fall_candidates)
            - 1,
            max_no_fall,
            dtype=int,
        )

        no_fall_candidates = [
            no_fall_candidates[
                int(index)
            ]
            for index
            in indexes
        ]

    extracted.extend(
        no_fall_candidates
    )

    counts = Counter(
        row["true_label"]
        for row in extracted
    )

    if (
        counts.get(
            "FALL",
            0,
        )
        == 0
        or counts.get(
            "NO_FALL",
            0,
        )
        == 0
    ):
        raise ValueError(
            "Import needs both FALL and NO_FALL examples."
        )

    return {
        "filename": str(
            filename
            or "historical.csv"
        ),
        "rows": extracted,
        "class_counts": dict(
            counts
        ),
        "raw_valid_rows": len(
            rows
        ),
    }


def import_labelled_csv(
    csv_text,
    filename="historical.csv",
):
    parsed = parse_labelled_csv(
        csv_text,
        filename,
    )

    fingerprint = hashlib.sha256(
        csv_text.encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()

    inserted = 0

    with database() as connection:
        with connection.cursor() as cursor:
            for row in parsed["rows"]:
                cursor.execute(
                    """
                    INSERT INTO threshold_imported_samples (
                        import_batch_id,
                        source_filename,
                        source_event_key,
                        label,
                        acc_peak_g,
                        gyro_max_dps,
                        acc_min_g,
                        tilt_change_deg,
                        low_g_duration_s,
                        acc_pp_g
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (
                        import_batch_id,
                        source_event_key
                    )
                    DO NOTHING
                    """,
                    (
                        fingerprint,
                        parsed["filename"],
                        row["source_event_key"],
                        row["true_label"],
                        row["acc_peak_g"],
                        row["gyro_max_dps"],
                        row["acc_min_g"],
                        row["tilt_change_deg"],
                        row["low_g_duration_s"],
                        row["acc_pp_g"],
                    ),
                )

                inserted += int(
                    cursor.rowcount
                    > 0
                )

    return {
        "ok": True,
        "filename": parsed[
            "filename"
        ],
        "raw_valid_rows": parsed[
            "raw_valid_rows"
        ],
        "extracted_samples": len(
            parsed["rows"]
        ),
        "inserted_samples": inserted,
        "duplicate_samples": (
            len(
                parsed["rows"]
            )
            - inserted
        ),
        "class_counts": parsed[
            "class_counts"
        ],
    }


def clear_imported_samples():
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM threshold_imported_samples
                """
            )

            removed = int(
                cursor.rowcount
                or 0
            )

    return {
        "ok": True,
        "removed_samples": removed,
    }


def _imported_rows():
    query = """
        SELECT
            id,
            label,
            acc_peak_g,
            gyro_max_dps,
            acc_min_g,
            tilt_change_deg,
            low_g_duration_s,
            acc_pp_g
        FROM threshold_imported_samples
        ORDER BY id
    """

    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                query
            )

            rows = cursor.fetchall()

    result = []

    for row in rows:
        features = {
            key: float(
                row[key]
            )
            for key
            in FEATURE_KEYS
        }

        true_label = _normalise_actual_type(
            row.get(
                "label"
            )
        )

        if true_label not in CLASS_LABELS:
            continue

        result.append(
            {
                "source": "IMPORTED_CSV",
                "source_id": int(
                    row["id"]
                ),
                "true_label": true_label,
                "predicted_label": (
                    _baseline_label_for_features(
                        features
                    )
                ),
                **features,
            }
        )

    return result


def imported_sample_summary():
    query = """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE label = 'FALL'
            ) AS fall_count,
            COUNT(*) FILTER (
                WHERE label = 'NO_FALL'
            ) AS no_fall_count,
            COUNT(
                DISTINCT import_batch_id
            ) AS file_count
        FROM threshold_imported_samples
    """

    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                query
            )

            row = cursor.fetchone() or {}

    return {
        "total": int(
            row.get(
                "total"
            )
            or 0
        ),
        "fall": int(
            row.get(
                "fall_count"
            )
            or 0
        ),
        "no_fall": int(
            row.get(
                "no_fall_count"
            )
            or 0
        ),
        "files": int(
            row.get(
                "file_count"
            )
            or 0
        ),
    }



def build_training_dataset():
    reviewed = _reviewed_rows()
    imported = _imported_rows()

    reviewed_count = len(
        reviewed
    )
    imported_count = len(
        imported
    )

    labelled = (
        reviewed
        + imported
    )

    labelled_count = len(
        labelled
    )

    # Background normal windows supplement the labelled examples without
    # overwhelming them.
    auto_normal_limit = min(
        MAX_AUTO_NORMAL_ROWS,
        max(
            20,
            labelled_count * 2,
        ),
    )

    auto_normal = _auto_normal_rows(
        auto_normal_limit
    )

    rows = (
        labelled
        + auto_normal
    )

    class_counts = Counter(
        row["true_label"]
        for row in rows
    )

    reviewed_class_counts = Counter(
        row["true_label"]
        for row in reviewed
    )

    imported_class_counts = Counter(
        row["true_label"]
        for row in imported
    )

    labelled_class_counts = Counter(
        row["true_label"]
        for row in labelled
    )

    return {
        "rows": rows,
        "reviewed_rows": reviewed,
        "imported_rows": imported,
        "auto_normal_rows": auto_normal,
        "summary": {
            "total_samples": len(
                rows
            ),
            "labelled_samples": (
                labelled_count
            ),
            "reviewed_samples": (
                reviewed_count
            ),
            "imported_samples": (
                imported_count
            ),
            "auto_normal_samples": len(
                auto_normal
            ),
            "class_counts": dict(
                class_counts
            ),
            "labelled_class_counts": dict(
                labelled_class_counts
            ),
            "reviewed_class_counts": dict(
                reviewed_class_counts
            ),
            "imported_class_counts": dict(
                imported_class_counts
            ),
        },
    }


def training_readiness(dataset=None):
    if dataset is None:
        dataset = build_training_dataset()

    summary = dataset["summary"]

    labelled_count = int(
        summary.get(
            "labelled_samples",
            0,
        )
    )

    labelled_class_counts = {
        key: int(value)
        for key, value
        in summary.get(
            "labelled_class_counts",
            {},
        ).items()
    }

    class_counts = {
        key: int(value)
        for key, value
        in summary.get(
            "class_counts",
            {},
        ).items()
    }

    reasons = []

    if (
        labelled_count
        <
        MIN_REVIEWED_LABELS
    ):
        reasons.append(
            "Import or review at least "
            f"{MIN_REVIEWED_LABELS} labelled events "
            f"({labelled_count} available)."
        )

    fall_labels = int(
        labelled_class_counts.get(
            "FALL",
            0,
        )
    )

    if fall_labels < 3:
        reasons.append(
            "At least 3 labelled FALL examples are recommended."
        )

    nonzero_classes = [
        key
        for key, value
        in class_counts.items()
        if value > 0
    ]

    if len(nonzero_classes) < 2:
        reasons.append(
            "Training needs both FALL and NO_FALL examples."
        )

    return {
        "ready": not reasons,
        "reasons": reasons,
        "recommended_next_step": (
            "Use reviewed live incidents or import a historical labelled CSV. "
            "FALL and NO_FALL examples are combined with background normal "
            "feature windows before training."
        ),
    }


def _matrix_from_rows(rows):
    return np.asarray(
        [
            [
                float(
                    row[key]
                )
                for key
                in FEATURE_KEYS
            ]
            for row in rows
        ],
        dtype=float,
    )


def _labels_from_rows(rows):
    return np.asarray(
        [
            row["true_label"]
            for row in rows
        ],
        dtype=object,
    )


def _baseline_predictions(rows):
    return np.asarray(
        [
            row["predicted_label"]
            for row in rows
        ],
        dtype=object,
    )


def _extract_split_rules(model):
    tree = model.tree_

    result = []

    feature_names = FEATURE_KEYS

    def walk(node_id, depth, path):
        if (
            tree.children_left[
                node_id
            ]
            ==
            tree.children_right[
                node_id
            ]
        ):
            return

        feature_index = int(
            tree.feature[
                node_id
            ]
        )

        threshold = float(
            tree.threshold[
                node_id
            ]
        )

        feature_key = feature_names[
            feature_index
        ]

        feature_meta = next(
            item
            for item in FEATURES
            if item["key"]
                ==
                feature_key
        )

        left_id = int(
            tree.children_left[
                node_id
            ]
        )

        right_id = int(
            tree.children_right[
                node_id
            ]
        )

        left_class = model.classes_[
            int(
                np.argmax(
                    tree.value[
                        left_id
                    ][0]
                )
            )
        ]

        right_class = model.classes_[
            int(
                np.argmax(
                    tree.value[
                        right_id
                    ][0]
                )
            )
        ]

        result.append(
            {
                "node_id": int(
                    node_id
                ),
                "depth": int(
                    depth
                ),
                "feature": feature_key,
                "feature_label": (
                    feature_meta[
                        "label"
                    ]
                ),
                "unit": feature_meta[
                    "unit"
                ],
                "threshold": round(
                    threshold,
                    4,
                ),
                "samples": int(
                    tree.n_node_samples[
                        node_id
                    ]
                ),
                "impurity": round(
                    float(
                        tree.impurity[
                            node_id
                        ]
                    ),
                    5,
                ),
                "left_prediction": str(
                    left_class
                ),
                "right_prediction": str(
                    right_class
                ),
                "rule_text": (
                    f"{feature_meta['label']} <= "
                    f"{threshold:.3f} {feature_meta['unit']} "
                    f"→ mostly {left_class}; "
                    f"> {threshold:.3f} {feature_meta['unit']} "
                    f"→ mostly {right_class}"
                ),
                "path": path,
            }
        )

        if depth >= 4:
            return

        walk(
            left_id,
            depth + 1,
            path
            + [
                f"{feature_meta['label']} <= {threshold:.3f}"
            ],
        )

        walk(
            right_id,
            depth + 1,
            path
            + [
                f"{feature_meta['label']} > {threshold:.3f}"
            ],
        )

    walk(
        0,
        0,
        [],
    )

    return result


def _feature_importances(model):
    values = []

    for item, score in zip(
        FEATURES,
        model.feature_importances_,
    ):
        values.append(
            {
                **item,
                "importance": round(
                    float(score),
                    6,
                ),
            }
        )

    return sorted(
        values,
        key=lambda item:
            item["importance"],
        reverse=True,
    )


def _metric_bundle(
    y_true,
    y_pred,
    labels,
):
    if len(y_true) == 0:
        return None

    matrix = confusion_matrix(
        y_true,
        y_pred,
        labels=labels,
    )

    report = classification_report(
        y_true,
        y_pred,
        labels=labels,
        output_dict=True,
        zero_division=0,
    )

    result = {
        "accuracy": round(
            float(
                accuracy_score(
                    y_true,
                    y_pred,
                )
            ),
            6,
        ),
        "balanced_accuracy": round(
            float(
                balanced_accuracy_score(
                    y_true,
                    y_pred,
                )
            ),
            6,
        ),
        "classes": list(labels),
        "confusion_matrix": (
            matrix
            .astype(int)
            .tolist()
        ),
        "classification_report": report,
    }

    fall_metrics = report.get(
        "FALL",
        {},
    )

    result["fall_precision"] = round(
        float(
            fall_metrics.get(
                "precision",
                0.0,
            )
        ),
        6,
    )

    result["fall_recall"] = round(
        float(
            fall_metrics.get(
                "recall",
                0.0,
            )
        ),
        6,
    )

    result["fall_f1"] = round(
        float(
            fall_metrics.get(
                "f1-score",
                0.0,
            )
        ),
        6,
    )

    if (
        list(labels)
        ==
        ["NO_FALL", "FALL"]
        and matrix.shape
        ==
        (2, 2)
    ):
        result["false_positives"] = int(
            matrix[0][1]
        )
        result["false_negatives"] = int(
            matrix[1][0]
        )
        result["true_falls_detected"] = int(
            matrix[1][1]
        )

    return result


def train_recommendation():
    dataset = build_training_dataset()

    readiness = training_readiness(
        dataset
    )

    if not readiness["ready"]:
        return {
            "ok": False,
            "trained": False,
            "readiness": readiness,
            "dataset": dataset[
                "summary"
            ],
        }

    rows = dataset["rows"]

    X = _matrix_from_rows(
        rows
    )

    y = _labels_from_rows(
        rows
    )

    indexes = np.arange(
        len(rows)
    )

    counts = Counter(
        y.tolist()
    )

    enough_for_holdout = (
        len(rows) >= 20
        and len(counts) == 2
        and min(
            counts.values()
        ) >= 4
    )

    if enough_for_holdout:
        (
            train_indexes,
            test_indexes,
        ) = train_test_split(
            indexes,
            test_size=0.30,
            random_state=42,
            shuffle=True,
            stratify=y,
        )

        X_train = X[
            train_indexes
        ]
        y_train = y[
            train_indexes
        ]

        X_eval = X[
            test_indexes
        ]
        y_eval = y[
            test_indexes
        ]

        eval_rows = [
            rows[
                int(index)
            ]
            for index
            in test_indexes
        ]

        evaluation_mode = (
            "STRATIFIED_HOLDOUT"
        )

    else:
        train_indexes = indexes
        X_train = X
        y_train = y

        X_eval = X
        y_eval = y

        eval_rows = rows

        evaluation_mode = (
            "TRAINING_SET_ONLY"
        )

    train_counts = Counter(
        y_train.tolist()
    )

    can_cross_validate = (
        len(X_train) >= 16
        and len(train_counts) == 2
        and min(
            train_counts.values()
        ) >= 3
    )

    best_cv_score = None
    best_params = None

    if can_cross_validate:
        folds = min(
            4,
            min(
                train_counts.values()
            ),
        )

        cv = StratifiedKFold(
            n_splits=folds,
            shuffle=True,
            random_state=42,
        )

        parameter_grid = {
            "criterion": [
                "gini",
                "entropy",
            ],
            "max_depth": [
                2,
                3,
                4,
            ],
            "min_samples_split": [
                2,
                4,
            ],
            "min_samples_leaf": [
                1,
                2,
            ],
            "class_weight": [
                "balanced",
                {
                    "NO_FALL": 1.0,
                    "FALL": 1.5,
                },
                {
                    "NO_FALL": 1.0,
                    "FALL": 2.0,
                },
            ],
        }

        search = GridSearchCV(
            estimator=DecisionTreeClassifier(
                random_state=42,
            ),
            param_grid=parameter_grid,
            scoring="balanced_accuracy",
            cv=cv,
            n_jobs=1,
        )

        search.fit(
            X_train,
            y_train,
        )

        model = search.best_estimator_

        best_cv_score = round(
            float(
                search.best_score_
            ),
            6,
        )

        best_params = {
            key: value
            for key, value
            in search.best_params_.items()
        }

        selection_method = (
            "GRID_SEARCH_CV"
        )

    else:
        minimum_leaf = max(
            1,
            min(
                3,
                len(
                    X_train
                )
                //
                10,
            ),
        )

        model = DecisionTreeClassifier(
            criterion="gini",
            max_depth=3,
            min_samples_leaf=minimum_leaf,
            class_weight="balanced",
            random_state=42,
        )

        model.fit(
            X_train,
            y_train,
        )

        best_params = {
            "criterion": "gini",
            "max_depth": 3,
            "min_samples_leaf": (
                minimum_leaf
            ),
            "class_weight": "balanced",
        }

        selection_method = (
            "FIXED_SMALL_DATA_TREE"
        )

    if not can_cross_validate:
        model.fit(
            X_train,
            y_train,
        )

    model_pred = model.predict(
        X_eval
    )

    baseline_pred = (
        _baseline_predictions(
            eval_rows
        )
    )

    present_labels = [
        label
        for label
        in CLASS_LABELS
        if label
        in set(
            y.tolist()
        )
    ]

    model_metrics = _metric_bundle(
        y_eval,
        model_pred,
        present_labels,
    )

    baseline_metrics = _metric_bundle(
        y_eval,
        baseline_pred,
        present_labels,
    )

    split_rules = _extract_split_rules(
        model
    )

    importances = _feature_importances(
        model
    )

    run = {
        "trained_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "model_scope": "FALL_VS_NO_FALL",
        "evaluation_mode": (
            evaluation_mode
        ),
        "holdout_reliable": bool(
            enough_for_holdout
        ),
        "selection_method": (
            selection_method
        ),
        "cross_validation_balanced_accuracy": (
            best_cv_score
        ),
        "best_parameters": (
            best_params
        ),
        "dataset": dataset[
            "summary"
        ],
        "tree": {
            "max_depth": int(
                model.get_depth()
            ),
            "leaf_count": int(
                model.get_n_leaves()
            ),
            "split_rules": split_rules,
            "feature_importances": (
                importances
            ),
        },
        "decision_tree_metrics": (
            model_metrics
        ),
        "current_system_metrics": (
            baseline_metrics
        ),
        "safety_note": (
            "Threshold Lab is a FALL vs NO_FALL analysis tool. "
            "It never automatically changes the live FFH/STF/near-miss detector. "
            "Use learned splits as evidence, then validate them with controlled "
            "labelled trials before editing thresholds."
        ),
        "data_note": (
            "Human reviews are converted to binary labels: FFH/STF -> FALL; "
            "near miss/false alarm -> NO_FALL. Background normal windows are "
            "automatically selected only when no incident occurred nearby."
        ),
    }

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO threshold_training_runs (
                    sample_count,
                    reviewed_sample_count,
                    auto_normal_sample_count,
                    evaluation_mode,
                    decision_tree_accuracy,
                    decision_tree_balanced_accuracy,
                    current_system_accuracy,
                    current_system_balanced_accuracy,
                    class_counts,
                    feature_importances,
                    split_rules,
                    confusion_matrix,
                    result_json
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                RETURNING id, created_at
                """,
                (
                    dataset[
                        "summary"
                    ][
                        "total_samples"
                    ],
                    dataset[
                        "summary"
                    ][
                        "reviewed_samples"
                    ],
                    dataset[
                        "summary"
                    ][
                        "auto_normal_samples"
                    ],
                    evaluation_mode,
                    model_metrics[
                        "accuracy"
                    ],
                    model_metrics[
                        "balanced_accuracy"
                    ],
                    baseline_metrics[
                        "accuracy"
                    ],
                    baseline_metrics[
                        "balanced_accuracy"
                    ],
                    Json(
                        dataset[
                            "summary"
                        ][
                            "class_counts"
                        ]
                    ),
                    Json(
                        importances
                    ),
                    Json(
                        split_rules
                    ),
                    Json(
                        model_metrics[
                            "confusion_matrix"
                        ]
                    ),
                    Json(
                        run
                    ),
                ),
            )

            record = cursor.fetchone()

        connection.commit()

    run["run_id"] = int(
        record[0]
    )

    run["database_created_at"] = (
        record[1].isoformat()
        if record[1]
        else None
    )

    return {
        "ok": True,
        "trained": True,
        "run": run,
    }


def latest_run():
    with database() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    created_at,
                    result_json
                FROM threshold_training_runs
                ORDER BY id DESC
                LIMIT 1
                """
            )

            row = cursor.fetchone()

    if not row:
        return None

    result = row.get(
        "result_json"
    ) or {}

    result["run_id"] = int(
        row["id"]
    )

    result[
        "database_created_at"
    ] = (
        row["created_at"].isoformat()
        if row["created_at"]
        else None
    )

    return result


def status():
    dataset = build_training_dataset()

    return {
        "current_thresholds": (
            current_thresholds()
        ),
        "features": FEATURES,
        "dataset": dataset[
            "summary"
        ],
        "readiness": (
            training_readiness(
                dataset
            )
        ),
        "latest_run": latest_run(),
        "historical_import": (
            imported_sample_summary()
        ),
        "behaviour": {
            "automatic_apply": False,
            "explanation": (
                "The binary Decision Tree learns FALL vs NO_FALL split recommendations "
                "from reviewed data. It never silently changes the live detector."
            ),
        },
    }
