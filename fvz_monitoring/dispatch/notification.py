"""Notification sender — Slack, email, SMS, web push.

Spúšťa sa Render workerom alebo webhook-om z alarm_engine.
Pre každý nový alarm pošle notifikáciu podľa severity:
- info     → nič (len log)
- warn     → Slack dispatch channel
- minor    → Slack + email assignee
- major    → Slack + email + SMS assignee
- critical → Slack + email + SMS + customer notification
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("notification")


def send_slack(channel: str, text: str, blocks: Optional[list] = None) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.warning("SLACK_BOT_TOKEN missing — skipping Slack")
        return False
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return True
    except Exception as e:
        log.warning(f"Slack send failed: {e}")
        return False


def send_email(to_email: str, subject: str, html: str) -> bool:
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        log.warning("SENDGRID_API_KEY missing — skipping email")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        msg = Mail(
            from_email=os.environ.get("NOTIFICATION_FROM_EMAIL", "dispecing@energovision.sk"),
            to_emails=to_email,
            subject=subject,
            html_content=html,
        )
        SendGridAPIClient(api_key).send(msg)
        return True
    except Exception as e:
        log.warning(f"Email send failed: {e}")
        return False


def send_sms(to_phone: str, message: str) -> bool:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth = os.environ.get("TWILIO_AUTH_TOKEN")
    from_phone = os.environ.get("TWILIO_FROM_PHONE")
    if not all([sid, auth, from_phone]):
        log.warning("Twilio creds missing — skipping SMS")
        return False
    try:
        from twilio.rest import Client
        Client(sid, auth).messages.create(body=message, from_=from_phone, to=to_phone)
        return True
    except Exception as e:
        log.warning(f"SMS send failed: {e}")
        return False


# =============================================================================
# Alarm notification orchestrator
# =============================================================================

def notify_alarm(alarm: dict, site: dict, assignee: Optional[dict] = None, customer: Optional[dict] = None):
    """Pošle notifikácie podľa severity."""
    severity = alarm.get("severity", "warn")
    title = alarm.get("title", "Alarm")
    site_name = site.get("site_name", "?")
    site_id = site.get("id")

    crm_link = f"https://app.energovision.sk/dispatch/site/{site_id}"
    text = f":rotating_light: *{severity.upper()}* — {site_name}\n{title}\n<{crm_link}|Otvor v CRM>"

    # Slack vždy (warn+)
    if severity in ("warn", "minor", "major", "critical"):
        send_slack(os.environ.get("SLACK_DISPATCH_CHANNEL", "#dispatch-alarms"), text)

    # Email assignee (minor+)
    if assignee and severity in ("minor", "major", "critical"):
        send_email(
            assignee["email"],
            f"[{severity.upper()}] {site_name} — {title}",
            f"<p>{alarm.get('description', '')}</p><p><a href='{crm_link}'>Otvor v CRM</a></p>",
        )

    # SMS assignee (major+)
    if assignee and severity in ("major", "critical") and assignee.get("phone"):
        send_sms(assignee["phone"], f"[{severity.upper()}] {site_name}: {title} — {crm_link}")

    # Customer notification (critical only — neotravujeme klientov pri minore)
    if customer and severity == "critical":
        send_email(
            customer["email"],
            f"Upozornenie — vaša FVE inštalácia ({site_name})",
            (
                f"<p>Dobrý deň,</p>"
                f"<p>na vašej fotovoltickej inštalácii sme zaznamenali problém: <b>{title}</b>.</p>"
                f"<p>Náš technik už dostal upozornenie a bude vás kontaktovať.</p>"
                f"<p>Energovision dispečing</p>"
            ),
        )
