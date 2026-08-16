-- DEP Nesso Safety System - Supabase schema v5 (website-managed Telegram recipients)
-- Safe to run more than once. Render also calls ensure_schema() on startup.

CREATE TABLE IF NOT EXISTS devices (
    device_id BIGSERIAL PRIMARY KEY,
    device_name VARCHAR(100) UNIQUE NOT NULL,
    worker_id VARCHAR(80) NOT NULL,
    last_seen TIMESTAMPTZ,
    activity_status VARCHAR(40) NOT NULL DEFAULT 'OFFLINE',
    status_message TEXT NOT NULL DEFAULT 'Waiting for sensor data.',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    latest_acceleration_g DOUBLE PRECISION,
    latest_gyro_dps DOUBLE PRECISION,
    battery_percent DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sensor_data (
    id BIGSERIAL PRIMARY KEY,
    device_name VARCHAR(100) NOT NULL,
    worker_id VARCHAR(80) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    elapsed_time_s DOUBLE PRECISION,
    accelerometer_x_g DOUBLE PRECISION NOT NULL,
    accelerometer_y_g DOUBLE PRECISION NOT NULL,
    accelerometer_z_g DOUBLE PRECISION NOT NULL,
    acceleration_magnitude_g DOUBLE PRECISION NOT NULL,
    gyroscope_x_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_y_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_z_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_magnitude_deg_s DOUBLE PRECISION NOT NULL,
    high_resolution BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS feature_windows (
    id BIGSERIAL PRIMARY KEY,
    device_name VARCHAR(100) NOT NULL,
    worker_id VARCHAR(80) NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    sample_count INTEGER NOT NULL,
    duration_s DOUBLE PRECISION,
    acc_mean_g DOUBLE PRECISION,
    acc_std_g DOUBLE PRECISION,
    acc_min_g DOUBLE PRECISION,
    acc_peak_g DOUBLE PRECISION,
    acc_pp_g DOUBLE PRECISION,
    gyro_mean_dps DOUBLE PRECISION,
    gyro_max_dps DOUBLE PRECISION,
    dominant_frequency_hz DOUBLE PRECISION,
    tilt_change_deg DOUBLE PRECISION,
    low_g_duration_s DOUBLE PRECISION,
    activity_status VARCHAR(40),
    confidence DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS safety_alerts (
    id BIGSERIAL PRIMARY KEY,
    worker_id VARCHAR(80) NOT NULL,
    device_name VARCHAR(100) NOT NULL,
    received_timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type VARCHAR(40) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    description TEXT NOT NULL,
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    acceleration_peak_g DOUBLE PRECISION,
    gyroscope_peak_dps DOUBLE PRECISION,
    minimum_acceleration_g DOUBLE PRECISION,
    tilt_change_deg DOUBLE PRECISION,
    low_g_duration_s DOUBLE PRECISION,
    trigger_details JSONB,
    feedback_label VARCHAR(30),
    actual_event_type VARCHAR(30),
    feedback_notes TEXT,
    feedback_at TIMESTAMPTZ
);

ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS low_g_duration_s DOUBLE PRECISION;
ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS trigger_details JSONB;
ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS feedback_label VARCHAR(30);
ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS feedback_notes TEXT;
ALTER TABLE safety_alerts ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMPTZ;


ALTER TABLE safety_alerts
ADD COLUMN IF NOT EXISTS actual_event_type VARCHAR(30);

CREATE TABLE IF NOT EXISTS incident_replay_samples (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES safety_alerts(id) ON DELETE CASCADE,
    device_name VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    relative_seconds DOUBLE PRECISION NOT NULL,
    phase VARCHAR(12) NOT NULL,
    accelerometer_x_g DOUBLE PRECISION NOT NULL,
    accelerometer_y_g DOUBLE PRECISION NOT NULL,
    accelerometer_z_g DOUBLE PRECISION NOT NULL,
    acceleration_magnitude_g DOUBLE PRECISION NOT NULL,
    gyroscope_x_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_y_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_z_deg_s DOUBLE PRECISION NOT NULL,
    gyroscope_magnitude_deg_s DOUBLE PRECISION NOT NULL,
    UNIQUE (incident_id, timestamp)
);

CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id BIGSERIAL PRIMARY KEY,
    device_name VARCHAR(100) NOT NULL,
    worker_id VARCHAR(80) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    received_samples INTEGER NOT NULL,
    batch_duration_s DOUBLE PRECISION NOT NULL DEFAULT 0,
    observed_cloud_hz DOUBLE PRECISION,
    expected_cloud_hz DOUBLE PRECISION NOT NULL DEFAULT 20,
    estimated_local_samples INTEGER NOT NULL DEFAULT 0,
    estimated_missing_cloud_samples INTEGER NOT NULL DEFAULT 0,
    sensor_rows_saved INTEGER NOT NULL DEFAULT 0,
    high_resolution_batch BOOLEAN NOT NULL DEFAULT FALSE,
    processing_ms DOUBLE PRECISION
);


CREATE TABLE IF NOT EXISTS notification_settings (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    telegram_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    normal_updates_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    near_miss_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    stf_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ffh_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    device_offline_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    low_battery_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    critical_repeat_seconds INTEGER NOT NULL DEFAULT 30,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO notification_settings (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS notification_log (
    id BIGSERIAL PRIMARY KEY,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    priority VARCHAR(20) NOT NULL,
    category VARCHAR(50) NOT NULL,
    incident_id BIGINT REFERENCES safety_alerts(id) ON DELETE SET NULL,
    worker_id VARCHAR(80),
    device_name VARCHAR(100),
    success BOOLEAN NOT NULL,
    message_preview TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS device_notification_state (
    device_name VARCHAR(100) PRIMARY KEY,
    offline_notified BOOLEAN NOT NULL DEFAULT FALSE,
    low_battery_notified BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_recipients (
    id BIGSERIAL PRIMARY KEY,
    chat_id VARCHAR(80) UNIQUE NOT NULL,
    chat_type VARCHAR(30) NOT NULL DEFAULT 'private',
    display_name VARCHAR(120) NOT NULL,
    telegram_username VARCHAR(120),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    normal_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    urgent_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    critical_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_pairings (
    code VARCHAR(20) PRIMARY KEY,
    display_name VARCHAR(120) NOT NULL,
    recipient_type VARCHAR(20) NOT NULL,
    normal_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    urgent_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    critical_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE notification_log
ADD COLUMN IF NOT EXISTS recipient_name VARCHAR(120);

CREATE INDEX IF NOT EXISTS idx_telegram_recipients_active ON telegram_recipients (active, id);
CREATE INDEX IF NOT EXISTS idx_telegram_pairings_expiry ON telegram_pairings (expires_at);
CREATE INDEX IF NOT EXISTS idx_notification_log_time ON notification_log (sent_at DESC);



CREATE TABLE IF NOT EXISTS threshold_imported_samples (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    import_batch_id VARCHAR(64) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_event_key VARCHAR(160) NOT NULL,
    label VARCHAR(20) NOT NULL,
    acc_peak_g DOUBLE PRECISION NOT NULL,
    gyro_max_dps DOUBLE PRECISION NOT NULL,
    acc_min_g DOUBLE PRECISION NOT NULL,
    tilt_change_deg DOUBLE PRECISION NOT NULL,
    low_g_duration_s DOUBLE PRECISION NOT NULL,
    acc_pp_g DOUBLE PRECISION NOT NULL,
    UNIQUE (import_batch_id, source_event_key)
);

CREATE INDEX IF NOT EXISTS idx_threshold_imported_created
ON threshold_imported_samples (created_at DESC);

CREATE TABLE IF NOT EXISTS threshold_training_runs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sample_count INTEGER NOT NULL,
    reviewed_sample_count INTEGER NOT NULL,
    auto_normal_sample_count INTEGER NOT NULL,
    evaluation_mode VARCHAR(40) NOT NULL,
    decision_tree_accuracy DOUBLE PRECISION,
    decision_tree_balanced_accuracy DOUBLE PRECISION,
    current_system_accuracy DOUBLE PRECISION,
    current_system_balanced_accuracy DOUBLE PRECISION,
    class_counts JSONB,
    feature_importances JSONB,
    split_rules JSONB,
    confusion_matrix JSONB,
    result_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_threshold_training_created
ON threshold_training_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sensor_device_time ON sensor_data (device_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_feature_device_time ON feature_windows (device_name, window_end DESC);
CREATE INDEX IF NOT EXISTS idx_alert_device_time ON safety_alerts (device_name, received_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_replay_incident_time ON incident_replay_samples (incident_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_pipeline_device_time ON pipeline_metrics (device_name, timestamp DESC);

ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE sensor_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE feature_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE safety_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_replay_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_metrics ENABLE ROW LEVEL SECURITY;

ALTER TABLE notification_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_notification_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_pairings ENABLE ROW LEVEL SECURITY;
ALTER TABLE threshold_imported_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE threshold_training_runs ENABLE ROW LEVEL SECURITY;
