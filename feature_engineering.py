from dataclasses import dataclass
import math

import numpy as np


@dataclass
class WindowFeatures:
    sample_count: int
    duration_s: float

    acc_mean_g: float
    acc_std_g: float
    acc_min_g: float
    acc_peak_g: float
    acc_pp_g: float

    gyro_mean_dps: float
    gyro_max_dps: float

    dominant_frequency_hz: float
    tilt_change_deg: float
    low_g_duration_s: float

    impact_after_low_g: bool
    recovery_acc_mean_g: float
    recovery_gyro_mean_dps: float


def _magnitude(x, y, z):
    return np.sqrt(
        np.square(x)
        + np.square(y)
        + np.square(z)
    )


def _angle_between_deg(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)

    if na == 0 or nb == 0:
        return 0.0

    cosine = float(
        np.clip(
            np.dot(a, b) / (na * nb),
            -1.0,
            1.0,
        )
    )

    return math.degrees(
        math.acos(cosine)
    )


def _dominant_frequency(values, timestamps):
    if len(values) < 8:
        return 0.0

    duration = timestamps[-1] - timestamps[0]

    if duration <= 0:
        return 0.0

    sample_rate = (
        len(values) - 1
    ) / duration

    if sample_rate <= 0:
        return 0.0

    signal = np.asarray(values, dtype=float)
    signal = signal - np.mean(signal)

    spectrum = np.abs(
        np.fft.rfft(signal)
    )

    frequencies = np.fft.rfftfreq(
        len(signal),
        d=1.0 / sample_rate,
    )

    if len(spectrum) <= 1:
        return 0.0

    spectrum[0] = 0.0

    valid = (
        (frequencies >= 0.5)
        &
        (frequencies <= 6.0)
    )

    if not np.any(valid):
        return 0.0

    valid_indexes = np.where(valid)[0]

    best_index = valid_indexes[
        int(
            np.argmax(
                spectrum[valid]
            )
        )
    ]

    return float(
        frequencies[best_index]
    )


def _longest_low_g_duration(
    timestamps,
    acc_magnitude,
    threshold,
):
    longest = 0.0
    start = None

    for index, value in enumerate(acc_magnitude):
        if value <= threshold:
            if start is None:
                start = timestamps[index]
        else:
            if start is not None:
                longest = max(
                    longest,
                    timestamps[index - 1] - start,
                )
                start = None

    if start is not None:
        longest = max(
            longest,
            timestamps[-1] - start,
        )

    return float(
        max(0.0, longest)
    )


def calculate_features(
    samples,
    free_fall_threshold_g=0.45,
    impact_threshold_g=2.50,
):
    if not samples:
        raise ValueError(
            "No samples supplied."
        )

    timestamps = np.asarray(
        [sample["timestamp"] for sample in samples],
        dtype=float,
    )

    ax = np.asarray(
        [sample["ax"] for sample in samples],
        dtype=float,
    )
    ay = np.asarray(
        [sample["ay"] for sample in samples],
        dtype=float,
    )
    az = np.asarray(
        [sample["az"] for sample in samples],
        dtype=float,
    )

    gx = np.asarray(
        [sample["gx"] for sample in samples],
        dtype=float,
    )
    gy = np.asarray(
        [sample["gy"] for sample in samples],
        dtype=float,
    )
    gz = np.asarray(
        [sample["gz"] for sample in samples],
        dtype=float,
    )

    acc = _magnitude(
        ax,
        ay,
        az,
    )

    gyro = _magnitude(
        gx,
        gy,
        gz,
    )

    duration = float(
        max(
            0.0,
            timestamps[-1] - timestamps[0],
        )
    )

    low_g_duration = (
        _longest_low_g_duration(
            timestamps,
            acc,
            free_fall_threshold_g,
        )
    )

    low_indexes = np.where(
        acc <= free_fall_threshold_g
    )[0]

    impact_after_low_g = False

    if len(low_indexes):
        first_low = int(
            low_indexes[0]
        )

        impact_after_low_g = bool(
            np.any(
                acc[first_low + 1 :]
                >= impact_threshold_g
            )
        )

    segment = max(
        3,
        len(samples) // 5,
    )

    start_vector = [
        float(np.mean(ax[:segment])),
        float(np.mean(ay[:segment])),
        float(np.mean(az[:segment])),
    ]

    end_vector = [
        float(np.mean(ax[-segment:])),
        float(np.mean(ay[-segment:])),
        float(np.mean(az[-segment:])),
    ]

    recovery_count = max(
        3,
        len(samples) // 6,
    )

    return WindowFeatures(
        sample_count=len(samples),
        duration_s=duration,

        acc_mean_g=float(np.mean(acc)),
        acc_std_g=float(np.std(acc)),
        acc_min_g=float(np.min(acc)),
        acc_peak_g=float(np.max(acc)),
        acc_pp_g=float(
            np.max(acc) - np.min(acc)
        ),

        gyro_mean_dps=float(np.mean(gyro)),
        gyro_max_dps=float(np.max(gyro)),

        dominant_frequency_hz=(
            _dominant_frequency(
                acc,
                timestamps,
            )
        ),

        tilt_change_deg=(
            _angle_between_deg(
                start_vector,
                end_vector,
            )
        ),

        low_g_duration_s=low_g_duration,

        impact_after_low_g=impact_after_low_g,

        recovery_acc_mean_g=float(
            np.mean(
                acc[-recovery_count:]
            )
        ),

        recovery_gyro_mean_dps=float(
            np.mean(
                gyro[-recovery_count:]
            )
        ),
    )
