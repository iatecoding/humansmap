"""Report endpoint — receives user-submitted issue reports, stores them, and optionally emails them."""

import json
import logging
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from app.config import settings
from app.rate_limit import limiter

logger = logging.getLogger(__name__)
router = APIRouter()

REPORTS_FILE = Path("/app/reports/reports.log")
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB hard cap


def _sanitize(text: str) -> str:
    """Strip control characters to prevent log injection."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


def append_report(message: str, client_ip: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ip": client_ip,
        "msg": _sanitize(message),
    }
    try:
        if REPORTS_FILE.exists() and REPORTS_FILE.stat().st_size >= MAX_FILE_BYTES:
            logger.warning("reports.log has reached the size cap — entry not written to file")
            return
        with REPORTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Could not write to reports.log: %s", exc)


class ReportPayload(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 1000:
            raise ValueError("message too long")
        return v


@router.post("/api/report", status_code=204)
@limiter.limit("5/hour")
async def submit_report(payload: ReportPayload, request: Request):
    """Accept a user report, store it in reports.log, and email if SMTP is configured."""
    client_ip = request.headers.get("X-Real-IP") or request.client.host
    logger.info("User report from %s: %.120s", client_ip, payload.message)
    append_report(payload.message, client_ip)

    if settings.report_smtp_host and settings.report_email_to:
        try:
            _send_email(payload.message)
            logger.info("Report email sent to %s", settings.report_email_to)
        except Exception as exc:
            logger.error("Failed to send report email: %s", exc)


def _send_email(message: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "HumansMap — User Report"
    msg["From"] = settings.report_smtp_from or settings.report_email_to
    msg["To"] = settings.report_email_to
    msg.set_content(message)

    with smtplib.SMTP(settings.report_smtp_host, settings.report_smtp_port) as smtp:
        smtp.ehlo()
        if settings.report_smtp_tls:
            smtp.starttls()
        if settings.report_smtp_user and settings.report_smtp_password:
            smtp.login(settings.report_smtp_user, settings.report_smtp_password)
        smtp.send_message(msg)
