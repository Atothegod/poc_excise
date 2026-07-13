from django.db import transaction
from django.utils import timezone

from .case_logic import INACTIVE_CASE_STATUSES
from .models import ChatMessage, CustomerReport


def build_notification_key(
    event_type, ca_number, report_id, case_id, message
):
    return "|".join(
        str(value or "")
        for value in (event_type, ca_number, report_id, case_id, message)
    )


def create_system_message(report, message, event_type, closed_loop_kind=None):
    """Persist one durable notification; retries return the existing row."""
    case_id = report.related_case_id
    key = build_notification_key(
        event_type,
        report.ca_number,
        report.id,
        case_id,
        message,
    )
    with transaction.atomic():
        notification, created = ChatMessage.objects.get_or_create(
            notification_key=key,
            defaults={
                "session_id": report.session_id,
                "report": report,
                "case_id": case_id,
                "role": ChatMessage.ROLE_SYSTEM,
                "content": message,
                "ca_number": report.ca_number,
                "event_type": event_type,
                "closed_loop_kind": closed_loop_kind or "",
            },
        )
    return notification, created


def serialize_chat_message(message):
    role = message.role
    if role == ChatMessage.ROLE_SYSTEM:
        role = "System Alert (OMS)"
    return {
        "id": message.id,
        "message_id": message.id,
        "role": role,
        "message": message.content,
        "content": message.content,
        "timestamp": message.created_at.isoformat(),
        "event_type": message.event_type or None,
        "ca_number": message.ca_number or None,
        "report_id": message.report_id,
        "case_id": str(message.case_id) if message.case_id else None,
        "notification_key": message.notification_key,
        "closed_loop_kind": message.closed_loop_kind or None,
    }


def serialize_notification(message, source=None):
    payload = {
        "message": message.content,
        "event_type": message.event_type,
        "ca_number": message.ca_number,
        "report_id": message.report_id,
        "case_id": str(message.case_id) if message.case_id else None,
        "notification_key": message.notification_key,
        "closed_loop_kind": message.closed_loop_kind or None,
        "message_id": message.id,
    }
    if source:
        payload["source"] = source
    return payload


def acknowledge_notifications(session_id, notification_keys):
    keys = {str(key) for key in notification_keys if key}
    if not keys:
        return 0
    return ChatMessage.objects.filter(
        session_id=session_id,
        role=ChatMessage.ROLE_SYSTEM,
        notification_key__in=keys,
        acknowledged_at__isnull=True,
    ).update(acknowledged_at=timezone.now())


def active_report_for_session(session_id, ca_number=None):
    reports = CustomerReport.objects.filter(
        session_id=session_id,
        is_resolved=False,
    )
    if ca_number:
        reports = reports.filter(ca_number=ca_number)
    return reports.select_related("related_case").order_by("-updated_at", "-id").first()


def active_case_report_for_session(session_id, ca_number=None):
    reports = (
        CustomerReport.objects.filter(
            session_id=session_id,
            related_case__isnull=False,
        )
        .exclude(related_case__status__in=INACTIVE_CASE_STATUSES)
        .select_related("related_case")
    )
    if ca_number:
        reports = reports.filter(ca_number=ca_number)
    return reports.order_by("is_resolved", "-updated_at", "-id").first()


def pending_closed_loop_report_for_session(session_id, ca_number=None):
    latest = (
        ChatMessage.objects.select_related("report__related_case")
        .filter(session_id=session_id)
        .order_by("-created_at", "-id")
        .first()
    )
    if not latest or latest.event_type != "closed_loop_prompt" or not latest.report:
        return None

    report = latest.report
    if ca_number and report.ca_number != ca_number:
        return None
    return report
