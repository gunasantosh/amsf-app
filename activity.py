from html import escape

from sqlalchemy.orm import Session

from database import AuditEvent, Member, NotificationLog, SessionLocal, get_display_name
from mailer import send_email_sync
from app_time import utc_now


def record_event(
    db: Session,
    *,
    actor_id: int | None,
    subject_member_id: int | None,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    details: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor_id,
        subject_member_id=subject_member_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        details=details,
    )
    db.add(event)
    return event


def queue_notification(
    db: Session,
    member: Member,
    *,
    subject: str,
    message: str,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
) -> NotificationLog | None:
    if not member.email:
        return None
    recipient_name = get_display_name(member)
    text_body = f"Hello {recipient_name},\n\n{message}\n\nSign in to AMSF for the full activity history."
    html_body = (
        '<html><body style="font-family:Arial,sans-serif;background:#0d1324;color:#e2e8f0;padding:24px;">'
        '<div style="max-width:620px;margin:0 auto;background:#11192e;border:1px solid #24314f;border-radius:18px;padding:28px;">'
        '<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8;margin-bottom:8px;">AMSF Activity Notice</div>'
        f'<h2 style="margin:0 0 14px;color:#5eead4;">{escape(subject)}</h2>'
        f'<p style="line-height:1.7;">Hello {escape(recipient_name)},</p>'
        f'<p style="line-height:1.7;white-space:pre-line;">{escape(message)}</p>'
        '<p style="line-height:1.7;color:#94a3b8;">Sign in to AMSF for the full activity history.</p>'
        "</div></body></html>"
    )
    notification = NotificationLog(
        recipient_member_id=member.id,
        recipient_email=member.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.add(notification)
    return notification


def queue_notifications(
    db: Session,
    members: list[Member],
    *,
    subject: str,
    message: str,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
) -> list[NotificationLog]:
    queued = []
    seen_emails = set()
    for member in members:
        if not member.email or member.email.lower() in seen_emails:
            continue
        seen_emails.add(member.email.lower())
        notification = queue_notification(
            db,
            member,
            subject=subject,
            message=message,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if notification:
            queued.append(notification)
    return queued


def deliver_notification(notification_id: int) -> None:
    db = SessionLocal()
    try:
        notification = db.query(NotificationLog).filter(NotificationLog.id == notification_id).first()
        if not notification or notification.status == "Sent":
            return
        try:
            was_sent = send_email_sync(
                notification.recipient_email,
                notification.subject,
                notification.text_body,
                notification.html_body,
            )
            notification.status = "Sent" if was_sent else "Skipped"
            notification.sent_at = utc_now() if was_sent else None
            notification.error_message = None
        except Exception as exc:
            notification.status = "Failed"
            notification.error_message = str(exc)[:2000]
        db.commit()
    finally:
        db.close()


def dispatch_queued(background_tasks, notifications: list[NotificationLog]) -> None:
    for notification in notifications:
        background_tasks.add_task(deliver_notification, notification.id)
