"""Outgoing email. One job today: verification codes."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError

logger = logging.getLogger("ln.email")


def is_configured() -> bool:
    return bool(settings.smtp_email and settings.smtp_password)


def _send_blocking(message: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls(context=context)
        smtp.login(settings.smtp_email, settings.smtp_password)
        smtp.send_message(message)


async def send(*, to: str, subject: str, body: str) -> None:
    if not is_configured():
        if settings.is_production:
            raise ServiceUnavailableError("Email is not configured on this server")
        # Development convenience only: production refuses above rather than
        # writing credentials to a log.
        logger.warning("SMTP not configured; email to %s not sent:\n%s\n%s", to, subject, body)
        return

    message = EmailMessage()
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_email}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        # smtplib blocks; off the event loop so one slow mail server cannot
        # stall every other request.
        await asyncio.to_thread(_send_blocking, message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.exception("Sending email to %s failed", to)
        raise ServiceUnavailableError("The email could not be sent. Try again shortly.") from exc
