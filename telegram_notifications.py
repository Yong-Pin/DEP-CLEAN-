import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from psycopg2.extras import RealDictCursor

from db import database


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PUBLIC_DASHBOARD_URL = os.getenv("PUBLIC_DASHBOARD_URL", "").strip()

LOW_BATTERY_THRESHOLD = 15.0
LOW_BATTERY_RESET_THRESHOLD = 25.0
OFFLINE_NOTIFY_AFTER_S = 30
MONITOR_INTERVAL_S = 15
RECENT_DEVICE_WINDOW_HOURS = 24

_monitor_stop = threading.Event()
_monitor_thread = None
_pairing_stop = threading.Event()
_pairing_thread = None
_updates_offset = 0
_bot_username_cache = None
_bot_username_checked_at = 0.0


def _legacy_recipient_ids():
    """Optional backwards-compatible recipients stored in Render."""
    return [
        item.strip()
        for item in TELEGRAM_CHAT_ID.split(",")
        if item.strip()
    ]


def _telegram_request(method, payload=None, timeout=10):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured on Render.")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))

    if not body.get("ok"):
        raise RuntimeError(str(body.get("description") or "Telegram API returned ok=false"))

    return body.get("result")


def get_bot_username(force=False):
    global _bot_username_cache, _bot_username_checked_at

    if not TELEGRAM_BOT_TOKEN:
        return None

    now = time.time()
    if not force and _bot_username_cache and now - _bot_username_checked_at < 600:
        return _bot_username_cache

    try:
        result = _telegram_request("getMe", {}, timeout=8)
        _bot_username_cache = (result or {}).get("username")
        _bot_username_checked_at = now
    except Exception:
        if force:
            _bot_username_cache = None
            _bot_username_checked_at = now

    return _bot_username_cache


def list_recipients(include_inactive=True):
    query = """
        SELECT
            id,
            chat_type,
            display_name,
            telegram_username,
            active,
            normal_enabled,
            urgent_enabled,
            critical_enabled,
            created_at,
            updated_at,
            RIGHT(chat_id, 4) AS chat_id_last4
        FROM telegram_recipients
    """
    if not include_inactive:
        query += " WHERE active = TRUE"
    query += " ORDER BY display_name, id"

    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]


def _recipient_rows_for_priority(priority, recipient_id=None):
    priority = str(priority).upper()
    toggle_column = {
        "NORMAL": "normal_enabled",
        "URGENT": "urgent_enabled",
        "CRITICAL": "critical_enabled",
    }.get(priority, "normal_enabled")

    where = ["active = TRUE"]
    params = []

    if recipient_id is not None:
        where = ["id = %s"]
        params.append(int(recipient_id))
    else:
        where.append(f"{toggle_column} = TRUE")

    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT id, chat_id, chat_type, display_name, telegram_username,
                       active, normal_enabled, urgent_enabled, critical_enabled
                FROM telegram_recipients
                WHERE {' AND '.join(where)}
                ORDER BY id
                """,
                tuple(params),
            )
            rows = [dict(row) for row in cursor.fetchall()]

    # Keep old TELEGRAM_CHAT_ID support as a migration fallback.
    if recipient_id is None:
        seen = {str(row["chat_id"]) for row in rows}
        for chat_id in _legacy_recipient_ids():
            if chat_id not in seen:
                rows.append(
                    {
                        "id": None,
                        "chat_id": chat_id,
                        "chat_type": "legacy",
                        "display_name": "Render legacy recipient",
                        "telegram_username": None,
                        "active": True,
                        "normal_enabled": True,
                        "urgent_enabled": True,
                        "critical_enabled": True,
                    }
                )
    return rows


def telegram_config():
    try:
        database_recipients = list_recipients(include_inactive=False)
    except Exception:
        database_recipients = []

    legacy = _legacy_recipient_ids()
    count = len(database_recipients) + len(legacy)
    bot_username = get_bot_username()

    return {
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "bot_username": bot_username,
        "recipient_count": count,
        "database_recipient_count": len(database_recipients),
        "legacy_recipient_count": len(legacy),
        "configured": bool(TELEGRAM_BOT_TOKEN and count > 0),
        "bot_ready": bool(TELEGRAM_BOT_TOKEN),
    }


def create_pairing(
    *,
    display_name,
    recipient_type,
    normal_enabled=True,
    urgent_enabled=True,
    critical_enabled=True,
):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Add TELEGRAM_BOT_TOKEN in Render before connecting recipients.")

    recipient_type = str(recipient_type).upper()
    if recipient_type not in {"PERSON", "GROUP"}:
        raise ValueError("recipient_type must be PERSON or GROUP")

    bot_username = get_bot_username(force=True)
    if not bot_username:
        raise RuntimeError(
            "The Telegram bot token could not be verified. Check TELEGRAM_BOT_TOKEN in Render."
        )

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    for _ in range(6):
        code = "NS-" + "".join(secrets.choice(alphabet) for _ in range(6))
        try:
            with database() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM telegram_pairings WHERE expires_at <= CURRENT_TIMESTAMP")
                    cursor.execute(
                        """
                        INSERT INTO telegram_pairings (
                            code,
                            display_name,
                            recipient_type,
                            normal_enabled,
                            urgent_enabled,
                            critical_enabled,
                            expires_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            code,
                            display_name.strip(),
                            recipient_type,
                            bool(normal_enabled),
                            bool(urgent_enabled),
                            bool(critical_enabled),
                            expires_at,
                        ),
                    )
                connection.commit()
            break
        except Exception as exc:
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
    else:
        raise RuntimeError("Could not create a unique pairing code. Try again.")

    parameter = code.replace("-", "_")
    if recipient_type == "PERSON":
        deep_link = f"https://t.me/{bot_username}?start={parameter}"
    else:
        deep_link = f"https://t.me/{bot_username}?startgroup={parameter}"

    return {
        "code": code,
        "display_name": display_name.strip(),
        "recipient_type": recipient_type,
        "expires_at": expires_at,
        "bot_username": bot_username,
        "deep_link": deep_link,
        "command": f"/start {code}" if recipient_type == "PERSON" else f"/link {code}",
    }


def cancel_pairing(code):
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM telegram_pairings WHERE code = %s", (str(code).upper(),))
            changed = cursor.rowcount
        connection.commit()
    return bool(changed)


def update_recipient(
    recipient_id,
    *,
    display_name,
    active,
    normal_enabled,
    urgent_enabled,
    critical_enabled,
):
    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                UPDATE telegram_recipients
                SET
                    display_name = %s,
                    active = %s,
                    normal_enabled = %s,
                    urgent_enabled = %s,
                    critical_enabled = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING
                    id, chat_type, display_name, telegram_username,
                    active, normal_enabled, urgent_enabled, critical_enabled,
                    created_at, updated_at, RIGHT(chat_id, 4) AS chat_id_last4
                """,
                (
                    display_name.strip(),
                    bool(active),
                    bool(normal_enabled),
                    bool(urgent_enabled),
                    bool(critical_enabled),
                    int(recipient_id),
                ),
            )
            row = cursor.fetchone()
        connection.commit()

    return dict(row) if row else None


def remove_recipient(recipient_id):
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM telegram_recipients WHERE id = %s", (int(recipient_id),))
            changed = cursor.rowcount
        connection.commit()
    return bool(changed)


def get_notification_settings():
    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    telegram_enabled,
                    normal_updates_enabled,
                    near_miss_enabled,
                    stf_enabled,
                    ffh_enabled,
                    device_offline_enabled,
                    low_battery_enabled,
                    critical_repeat_seconds,
                    updated_at
                FROM notification_settings
                WHERE id = 1
                """
            )
            row = cursor.fetchone()

    return dict(row) if row else {
        "telegram_enabled": True,
        "normal_updates_enabled": True,
        "near_miss_enabled": True,
        "stf_enabled": True,
        "ffh_enabled": True,
        "device_offline_enabled": True,
        "low_battery_enabled": True,
        "critical_repeat_seconds": 30,
        "updated_at": None,
    }


def update_notification_settings(settings):
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_settings
                SET
                    telegram_enabled = %s,
                    normal_updates_enabled = %s,
                    near_miss_enabled = %s,
                    stf_enabled = %s,
                    ffh_enabled = %s,
                    device_offline_enabled = %s,
                    low_battery_enabled = %s,
                    critical_repeat_seconds = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (
                    settings["telegram_enabled"],
                    settings["normal_updates_enabled"],
                    settings["near_miss_enabled"],
                    settings["stf_enabled"],
                    settings["ffh_enabled"],
                    settings["device_offline_enabled"],
                    settings["low_battery_enabled"],
                    settings["critical_repeat_seconds"],
                ),
            )
        connection.commit()

    return get_notification_settings()


def recent_notification_log(limit=30):
    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    sent_at,
                    priority,
                    category,
                    incident_id,
                    worker_id,
                    device_name,
                    recipient_name,
                    success,
                    message_preview,
                    error_message
                FROM notification_log
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


def _log_delivery(
    *,
    priority,
    category,
    incident_id=None,
    worker_id=None,
    device_name=None,
    recipient_name=None,
    success,
    preview,
    error_message=None,
):
    try:
        with database() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO notification_log (
                        priority,
                        category,
                        incident_id,
                        worker_id,
                        device_name,
                        recipient_name,
                        success,
                        message_preview,
                        error_message
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        priority,
                        category,
                        incident_id,
                        worker_id,
                        device_name,
                        recipient_name,
                        success,
                        preview[:500],
                        error_message[:1000] if error_message else None,
                    ),
                )
            connection.commit()
    except Exception:
        pass


def _dashboard_markup():
    if not PUBLIC_DASHBOARD_URL:
        return None

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Open Nesso Safety Dashboard",
                    "url": PUBLIC_DASHBOARD_URL,
                }
            ]
        ]
    }


def _send_raw_to_chat(chat_id, text, *, disable_notification=False, markup=None):
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": bool(disable_notification),
        "link_preview_options": {"is_disabled": True},
    }
    if markup:
        payload["reply_markup"] = markup
    return _telegram_request("sendMessage", payload, timeout=10)


def send_telegram_message(
    text,
    *,
    priority="NORMAL",
    category="SYSTEM",
    incident_id=None,
    worker_id=None,
    device_name=None,
    bypass_enabled=False,
    recipient_id=None,
):
    """
    NORMAL = silent Telegram delivery.
    URGENT / CRITICAL = normal audible Telegram notification.
    Recipients are selected from the website-managed recipient list.
    """
    settings = get_notification_settings()

    if not bypass_enabled and not settings.get("telegram_enabled", True):
        return {
            "ok": False,
            "skipped": True,
            "reason": "Telegram notifications are disabled in dashboard settings.",
        }

    if not TELEGRAM_BOT_TOKEN:
        return {
            "ok": False,
            "skipped": True,
            "reason": "TELEGRAM_BOT_TOKEN is not configured on Render.",
        }

    recipients = _recipient_rows_for_priority(priority, recipient_id=recipient_id)
    if not recipients:
        return {
            "ok": False,
            "skipped": True,
            "reason": "No matching Telegram recipients are enabled for this alert level.",
        }

    disable_notification = str(priority).upper() == "NORMAL"
    successes = 0
    failures = []

    for recipient in recipients:
        recipient_name = recipient.get("display_name") or "Telegram recipient"
        try:
            _send_raw_to_chat(
                recipient["chat_id"],
                text,
                disable_notification=disable_notification,
                markup=_dashboard_markup(),
            )
            successes += 1
            _log_delivery(
                priority=priority,
                category=category,
                incident_id=incident_id,
                worker_id=worker_id,
                device_name=device_name,
                recipient_name=recipient_name,
                success=True,
                preview=re.sub(r"<[^>]+>", "", text),
            )
        except Exception as exc:
            failures.append(f"{recipient_name}: {exc}")
            _log_delivery(
                priority=priority,
                category=category,
                incident_id=incident_id,
                worker_id=worker_id,
                device_name=device_name,
                recipient_name=recipient_name,
                success=False,
                preview=re.sub(r"<[^>]+>", "", text),
                error_message=str(exc),
            )

    return {
        "ok": successes > 0 and not failures,
        "successful_recipients": successes,
        "failed_recipients": len(failures),
        "errors": failures,
    }


def _event_priority(event_type):
    if event_type == "POSSIBLE_FFH":
        return "CRITICAL"
    if event_type == "POSSIBLE_STF":
        return "URGENT"
    return "NORMAL"


def _event_enabled(event_type, settings):
    if event_type == "POSSIBLE_FFH":
        return settings.get("ffh_enabled", True)
    if event_type == "POSSIBLE_STF":
        return settings.get("stf_enabled", True)
    if event_type == "POSSIBLE_NEAR_MISS":
        return settings.get("near_miss_enabled", True)
    return False


def _incident_message(incident):
    event_type = incident["event_type"]
    worker = html.escape(str(incident.get("worker_id") or "Unknown"))
    device = html.escape(str(incident.get("device_name") or "Unknown"))
    incident_id = incident.get("id")
    acc = incident.get("acceleration_peak_g")
    gyro = incident.get("gyroscope_peak_dps")
    tilt = incident.get("tilt_change_deg")
    low_g = incident.get("low_g_duration_s")

    def fmt(value, digits=2):
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    if event_type == "POSSIBLE_FFH":
        return (
            "🆘🆘 <b>CRITICAL NESSO SAFETY ALERT</b> 🆘🆘\n\n"
            "<b>POSSIBLE FALL FROM HEIGHT</b>\n"
            "Immediate worker check required.\n\n"
            f"👷 Worker: <b>{worker}</b>\n"
            f"📟 Device: {device}\n"
            f"🧾 Incident: #{incident_id}\n\n"
            f"Impact peak: <b>{fmt(acc)} g</b>\n"
            f"Gyroscope peak: <b>{fmt(gyro, 1)} °/s</b>\n"
            f"Tilt change: <b>{fmt(tilt, 1)}°</b>\n"
            f"Low-g duration: <b>{fmt(low_g, 2)} s</b>\n\n"
            "🚨 <b>DO NOT IGNORE THIS ALERT.</b> Check the worker immediately and acknowledge the incident in the dashboard."
        )

    if event_type == "POSSIBLE_STF":
        return (
            "🚨 <b>URGENT NESSO SAFETY ALERT</b>\n\n"
            "<b>Possible slip / trip / fall detected</b>\n\n"
            f"👷 Worker: <b>{worker}</b>\n"
            f"📟 Device: {device}\n"
            f"🧾 Incident: #{incident_id}\n\n"
            f"Acceleration peak: <b>{fmt(acc)} g</b>\n"
            f"Gyroscope peak: <b>{fmt(gyro, 1)} °/s</b>\n"
            f"Tilt change: <b>{fmt(tilt, 1)}°</b>\n\n"
            "Please check the worker as soon as possible and review the incident in the dashboard."
        )

    return (
        "🟡 <b>Nesso Safety Update</b>\n\n"
        "Possible near miss detected. No full fall pattern was confirmed by the current rule set.\n\n"
        f"👷 Worker: <b>{worker}</b>\n"
        f"📟 Device: {device}\n"
        f"🧾 Incident: #{incident_id}\n"
        f"Acceleration peak: {fmt(acc)} g\n"
        f"Gyroscope peak: {fmt(gyro, 1)} °/s\n\n"
        "This is a normal silent update for later review."
    )


def _incident_acknowledged(incident_id):
    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT acknowledged FROM safety_alerts WHERE id = %s",
                (incident_id,),
            )
            row = cursor.fetchone()
    return bool(row and row[0])


def _incident_notification_worker(incident):
    settings = get_notification_settings()
    event_type = incident["event_type"]

    if not _event_enabled(event_type, settings):
        return

    priority = _event_priority(event_type)
    send_telegram_message(
        _incident_message(incident),
        priority=priority,
        category=event_type,
        incident_id=incident.get("id"),
        worker_id=incident.get("worker_id"),
        device_name=incident.get("device_name"),
    )

    # FFH gets an automatic second alert if nobody has acknowledged it.
    if event_type == "POSSIBLE_FFH":
        repeat_seconds = int(settings.get("critical_repeat_seconds") or 30)
        repeat_seconds = max(15, min(300, repeat_seconds))
        time.sleep(repeat_seconds)

        if _incident_acknowledged(incident["id"]):
            return

        worker = html.escape(str(incident.get("worker_id") or "Unknown"))
        device = html.escape(str(incident.get("device_name") or "Unknown"))
        send_telegram_message(
            "🆘 <b>CRITICAL ALERT STILL UNACKNOWLEDGED</b>\n\n"
            f"Possible fall from height for <b>{worker}</b> has not been acknowledged after {repeat_seconds} seconds.\n"
            f"Device: {device}\n"
            f"Incident: #{incident['id']}\n\n"
            "🚨 Please check the worker immediately.",
            priority="CRITICAL",
            category="FFH_ESCALATION",
            incident_id=incident.get("id"),
            worker_id=incident.get("worker_id"),
            device_name=incident.get("device_name"),
        )


def notify_incident_async(incident):
    thread = threading.Thread(
        target=_incident_notification_worker,
        args=(dict(incident),),
        daemon=True,
        name=f"telegram-incident-{incident.get('id')}",
    )
    thread.start()


def send_test_notification_async(priority, recipient_id=None):
    priority = str(priority).upper()
    if priority not in {"NORMAL", "URGENT", "CRITICAL"}:
        priority = "NORMAL"

    if priority == "CRITICAL":
        text = (
            "🆘 <b>CRITICAL TEST ALERT</b>\n\n"
            "This is a test of the Nesso Safety fall-from-height Telegram alert.\n"
            "Real critical alerts will be audible and can repeat if they are not acknowledged."
        )
    elif priority == "URGENT":
        text = (
            "🚨 <b>URGENT TEST ALERT</b>\n\n"
            "This is a test of the Nesso Safety slip/trip/fall Telegram alert."
        )
    else:
        text = (
            "✅ <b>Nesso Safety Test Message</b>\n\n"
            "Telegram is connected. This normal test is sent silently."
        )

    result_box = {}

    def worker():
        result_box.update(
            send_telegram_message(
                text,
                priority=priority,
                category="TEST",
                bypass_enabled=True,
                recipient_id=recipient_id,
            )
        )

    thread = threading.Thread(target=worker, daemon=True, name="telegram-test")
    thread.start()
    return {"queued": True, "priority": priority}


def _pairing_lookup(code):
    code = str(code).strip().upper().replace("_", "-")
    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("DELETE FROM telegram_pairings WHERE expires_at <= CURRENT_TIMESTAMP")
            cursor.execute(
                """
                SELECT code, display_name, recipient_type,
                       normal_enabled, urgent_enabled, critical_enabled, expires_at
                FROM telegram_pairings
                WHERE code = %s AND expires_at > CURRENT_TIMESTAMP
                """,
                (code,),
            )
            row = cursor.fetchone()
        connection.commit()
    return dict(row) if row else None


def _complete_pairing(pairing, chat):
    chat_id = str(chat.get("id"))
    chat_type = str(chat.get("type") or "unknown")
    username = chat.get("username")

    desired = pairing["recipient_type"]
    is_private = chat_type == "private"
    is_group = chat_type in {"group", "supergroup"}

    if desired == "PERSON" and not is_private:
        _send_raw_to_chat(
            chat_id,
            "This pairing code is for a <b>person</b>. Open the bot in a private chat and use the same pairing code there.",
        )
        return False

    if desired == "GROUP" and not is_group:
        _send_raw_to_chat(
            chat_id,
            "This pairing code is for a <b>Telegram group</b>. Add the bot to the target group and use the code inside that group.",
        )
        return False

    with database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO telegram_recipients (
                    chat_id,
                    chat_type,
                    display_name,
                    telegram_username,
                    active,
                    normal_enabled,
                    urgent_enabled,
                    critical_enabled
                )
                VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET
                    chat_type = EXCLUDED.chat_type,
                    display_name = EXCLUDED.display_name,
                    telegram_username = EXCLUDED.telegram_username,
                    active = TRUE,
                    normal_enabled = EXCLUDED.normal_enabled,
                    urgent_enabled = EXCLUDED.urgent_enabled,
                    critical_enabled = EXCLUDED.critical_enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    chat_type,
                    pairing["display_name"],
                    username,
                    pairing["normal_enabled"],
                    pairing["urgent_enabled"],
                    pairing["critical_enabled"],
                ),
            )
            cursor.execute("DELETE FROM telegram_pairings WHERE code = %s", (pairing["code"],))
        connection.commit()

    levels = []
    if pairing["normal_enabled"]:
        levels.append("normal")
    if pairing["urgent_enabled"]:
        levels.append("urgent")
    if pairing["critical_enabled"]:
        levels.append("critical")

    _send_raw_to_chat(
        chat_id,
        "✅ <b>Nesso Safety recipient connected</b>\n\n"
        f"Name: <b>{html.escape(str(pairing['display_name']))}</b>\n"
        f"Alert levels: {html.escape(', '.join(levels) or 'none')}\n\n"
        "An administrator can change these alert levels from the Nesso Safety website at any time.",
        disable_notification=False,
        markup=_dashboard_markup(),
    )
    return True


def _handle_pairing_update(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text = str(message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()

    if command not in {"/start", "/link"}:
        return

    if len(parts) < 2:
        _send_raw_to_chat(
            chat_id,
            "👋 <b>Nesso Safety Bot</b>\n\nTo receive safety alerts, ask an administrator to create a pairing code from the Nesso Safety website, then use that code here.",
            disable_notification=True,
        )
        return

    code = parts[1].strip().upper().replace("_", "-")
    pairing = _pairing_lookup(code)

    if not pairing:
        _send_raw_to_chat(
            chat_id,
            "That pairing code is invalid or has expired. Create a new code from the Nesso Safety website and try again.",
            disable_notification=True,
        )
        return

    _complete_pairing(pairing, chat)


def _pairing_poll_loop():
    global _updates_offset

    while not _pairing_stop.is_set():
        if not TELEGRAM_BOT_TOKEN:
            _pairing_stop.wait(5)
            continue

        try:
            payload = {
                "timeout": 20,
                "allowed_updates": ["message", "edited_message"],
            }
            if _updates_offset:
                payload["offset"] = _updates_offset

            updates = _telegram_request("getUpdates", payload, timeout=25) or []
            for update in updates:
                update_id = int(update.get("update_id", 0))
                _updates_offset = max(_updates_offset, update_id + 1)
                try:
                    _handle_pairing_update(update)
                except Exception:
                    pass
        except Exception:
            _pairing_stop.wait(4)


def start_recipient_pairing_monitor():
    global _pairing_thread
    _pairing_stop.clear()

    if _pairing_thread and _pairing_thread.is_alive():
        return

    _pairing_thread = threading.Thread(
        target=_pairing_poll_loop,
        daemon=True,
        name="telegram-pairing-monitor",
    )
    _pairing_thread.start()


def stop_recipient_pairing_monitor():
    _pairing_stop.set()


def _ensure_device_state(cursor, device_name):
    cursor.execute(
        """
        INSERT INTO device_notification_state (device_name)
        VALUES (%s)
        ON CONFLICT (device_name) DO NOTHING
        """,
        (device_name,),
    )


def _monitor_devices_once():
    settings = get_notification_settings()
    config = telegram_config()

    if not settings.get("telegram_enabled", True) or not config["configured"]:
        return

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=RECENT_DEVICE_WINDOW_HOURS)
    offline_cutoff = now - timedelta(seconds=OFFLINE_NOTIFY_AFTER_S)

    with database() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    d.device_name,
                    d.worker_id,
                    d.last_seen,
                    d.battery_percent,
                    COALESCE(s.offline_notified, FALSE) AS offline_notified,
                    COALESCE(s.low_battery_notified, FALSE) AS low_battery_notified
                FROM devices d
                LEFT JOIN device_notification_state s
                    ON s.device_name = d.device_name
                WHERE d.last_seen IS NOT NULL
                """
            )
            devices = [dict(row) for row in cursor.fetchall()]

            state_changes = []

            for device in devices:
                device_name = device["device_name"]
                worker = device["worker_id"]
                last_seen = device["last_seen"]
                battery = device["battery_percent"]

                _ensure_device_state(cursor, device_name)

                # Ignore very old demo/test devices so a Render restart does not spam Telegram.
                is_recent = bool(last_seen and last_seen >= recent_cutoff)
                is_offline = bool(is_recent and last_seen < offline_cutoff)

                if (
                    settings.get("normal_updates_enabled", True)
                    and settings.get("device_offline_enabled", True)
                    and is_offline
                    and not device["offline_notified"]
                ):
                    send_telegram_message(
                        "🔌 <b>Nesso Device Offline</b>\n\n"
                        f"Worker: <b>{html.escape(str(worker))}</b>\n"
                        f"Device: {html.escape(str(device_name))}\n"
                        f"No new data has been received for at least {OFFLINE_NOTIFY_AFTER_S} seconds.\n\n"
                        "This is a normal silent system update.",
                        priority="NORMAL",
                        category="DEVICE_OFFLINE",
                        worker_id=worker,
                        device_name=device_name,
                    )
                    state_changes.append((True, None, device_name))

                elif (
                    settings.get("normal_updates_enabled", True)
                    and device["offline_notified"]
                    and is_recent
                    and not is_offline
                ):
                    send_telegram_message(
                        "✅ <b>Nesso Connection Restored</b>\n\n"
                        f"Worker: <b>{html.escape(str(worker))}</b>\n"
                        f"Device: {html.escape(str(device_name))}\n"
                        "Live sensor data is being received again.",
                        priority="NORMAL",
                        category="DEVICE_RESTORED",
                        worker_id=worker,
                        device_name=device_name,
                    )
                    state_changes.append((False, None, device_name))

                try:
                    battery_value = float(battery) if battery is not None else None
                except (TypeError, ValueError):
                    battery_value = None

                if (
                    settings.get("normal_updates_enabled", True)
                    and settings.get("low_battery_enabled", True)
                    and battery_value is not None
                    and battery_value <= LOW_BATTERY_THRESHOLD
                    and not device["low_battery_notified"]
                ):
                    send_telegram_message(
                        "🔋 <b>Low Nesso Battery</b>\n\n"
                        f"Worker: <b>{html.escape(str(worker))}</b>\n"
                        f"Device: {html.escape(str(device_name))}\n"
                        f"Battery: <b>{battery_value:.0f}%</b>\n\n"
                        "Please charge the device when practical. This is a normal silent update.",
                        priority="NORMAL",
                        category="LOW_BATTERY",
                        worker_id=worker,
                        device_name=device_name,
                    )
                    state_changes.append((None, True, device_name))

                elif (
                    device["low_battery_notified"]
                    and battery_value is not None
                    and battery_value >= LOW_BATTERY_RESET_THRESHOLD
                ):
                    state_changes.append((None, False, device_name))

            for offline_value, low_value, device_name in state_changes:
                if offline_value is not None:
                    cursor.execute(
                        """
                        UPDATE device_notification_state
                        SET offline_notified = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE device_name = %s
                        """,
                        (offline_value, device_name),
                    )
                if low_value is not None:
                    cursor.execute(
                        """
                        UPDATE device_notification_state
                        SET low_battery_notified = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE device_name = %s
                        """,
                        (low_value, device_name),
                    )

        connection.commit()


def _monitor_loop():
    while not _monitor_stop.wait(MONITOR_INTERVAL_S):
        try:
            _monitor_devices_once()
        except Exception:
            # The monitoring loop must not crash the web service.
            pass


def start_device_notification_monitor():
    global _monitor_thread
    _monitor_stop.clear()

    if _monitor_thread and _monitor_thread.is_alive():
        return

    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        daemon=True,
        name="telegram-device-monitor",
    )
    _monitor_thread.start()


def stop_device_notification_monitor():
    _monitor_stop.set()
