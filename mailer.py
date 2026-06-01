import asyncio
import os
from email.message import EmailMessage
from typing import Optional

import aiosmtplib

import settings  # noqa: F401


async def send_email(
    recipient: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
) -> bool:
    smtp_host = os.getenv("AMSF_SMTP_HOST")
    smtp_port = int(os.getenv("AMSF_SMTP_PORT", "587"))
    smtp_user = os.getenv("AMSF_SMTP_USER")
    smtp_password = os.getenv("AMSF_SMTP_PASSWORD")
    smtp_from = os.getenv("AMSF_SMTP_FROM", smtp_user or "noreply@amsf.local")

    if not smtp_host:
        print(f"Email skipped for {recipient}. Configure SMTP. Subject: {subject}")
        print(text_body)
        return False

    message = EmailMessage()
    message["From"] = smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    await aiosmtplib.send(
        message,
        hostname=smtp_host,
        port=smtp_port,
        username=smtp_user,
        password=smtp_password,
        start_tls=True,
    )
    return True


def send_email_sync(recipient: str, subject: str, text_body: str, html_body: Optional[str] = None) -> bool:
    return asyncio.run(send_email(recipient, subject, text_body, html_body))
