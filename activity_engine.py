from dataclasses import dataclass

from thresholds import (
    FFH_GYRO_DPS,
    FREE_FALL_MIN_DURATION_S,
    IMPACT_G,
    MIN_TILT_CHANGE_DEG,
    NEAR_MISS_ACC_PP_G,
    NEAR_MISS_GYRO_DPS,
    RUN_FREQ_MIN_HZ,
    STANDING_ACC_STD_G,
    STANDING_GYRO_MEAN_DPS,
    STF_GYRO_DPS,
    WALK_FREQ_MAX_HZ,
    WALK_FREQ_MIN_HZ,
)


@dataclass
class Decision:
    status: str
    message: str
    confidence: float
    severity: str | None = None
    is_event: bool = False


def classify(features):
    if (
        features.low_g_duration_s
        >= FREE_FALL_MIN_DURATION_S
        and features.impact_after_low_g
        and features.acc_peak_g
        >= IMPACT_G
        and features.gyro_max_dps
        >= FFH_GYRO_DPS
        and features.tilt_change_deg
        >= MIN_TILT_CHANGE_DEG
    ):
        return Decision(
            status="POSSIBLE_FFH",
            message=(
                "Possible fall from height detected: "
                "low-g/free-fall pattern followed by "
                "impact and body rotation."
            ),
            confidence=0.95,
            severity="URGENT",
            is_event=True,
        )

    if (
        features.acc_peak_g
        >= IMPACT_G
        and features.gyro_max_dps
        >= STF_GYRO_DPS
        and features.tilt_change_deg
        >= MIN_TILT_CHANGE_DEG
        and features.low_g_duration_s
        < FREE_FALL_MIN_DURATION_S
    ):
        return Decision(
            status="POSSIBLE_STF",
            message=(
                "Possible slip, trip or fall detected: "
                "strong impact, rotation and posture change."
            ),
            confidence=0.90,
            severity="URGENT",
            is_event=True,
        )

    recovered = (
        0.75
        <= features.recovery_acc_mean_g
        <= 1.25
        and features.recovery_gyro_mean_dps
        < 100.0
    )

    if (
        features.acc_peak_g
        < IMPACT_G
        and features.gyro_max_dps
        >= NEAR_MISS_GYRO_DPS
        and features.acc_pp_g
        >= NEAR_MISS_ACC_PP_G
        and recovered
    ):
        return Decision(
            status="POSSIBLE_NEAR_MISS",
            message=(
                "Possible near miss detected: "
                "sudden instability followed by recovery."
            ),
            confidence=0.78,
            severity="ATTENTION",
            is_event=True,
        )

    if (
        features.acc_std_g
        <= STANDING_ACC_STD_G
        and features.gyro_mean_dps
        <= STANDING_GYRO_MEAN_DPS
    ):
        return Decision(
            status="STANDING",
            message="Worker is standing or stationary.",
            confidence=0.90,
        )

    if (
        features.dominant_frequency_hz
        >= RUN_FREQ_MIN_HZ
        and features.acc_std_g
        >= 0.25
    ):
        return Decision(
            status="RUNNING",
            message="Worker movement is consistent with running.",
            confidence=0.78,
        )

    if (
        WALK_FREQ_MIN_HZ
        <= features.dominant_frequency_hz
        <= WALK_FREQ_MAX_HZ
        and features.acc_std_g
        >= 0.08
    ):
        return Decision(
            status="WALKING",
            message="Worker movement is consistent with walking.",
            confidence=0.82,
        )

    return Decision(
        status="MOVING",
        message="Worker is moving; no fall pattern detected.",
        confidence=0.60,
    )
