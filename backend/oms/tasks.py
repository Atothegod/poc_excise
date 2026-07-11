from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from uuid import uuid4
from .case_logic import INACTIVE_CASE_STATUSES
from .models import OutageCase, CustomerReport
from .services import get_pea_assessment
import requests
import os

AGENT_WEBHOOK_URL = os.getenv(
    "AGENT_WEBHOOK_URL", "http://dspy-agent:8000/webhook/notify"
)
SLA_EXPIRED_CLOSED_LOOP_KIND = "sla_expired"
SLA_EXPIRED_CLOSED_LOOP_MESSAGE = (
    "ขออภัยที่การดำเนินการเกินเวลาที่แจ้งไว้ค่ะ "
    "ไฟฟ้ากลับมาใช้งานได้หรือยังคะ"
)


def schedule_case_timer(case, task, task_id_field, args, eta):
    """Persist a timer identity before publishing so an immediate task can claim it."""
    task_id = str(uuid4())
    OutageCase.objects.filter(pk=case.pk).update(**{task_id_field: task_id})
    setattr(case, task_id_field, task_id)
    def publish():
        try:
            task.apply_async(args=args, eta=eta, task_id=task_id)
        except Exception:
            OutageCase.objects.filter(
                pk=case.pk,
                **{task_id_field: task_id},
            ).update(**{task_id_field: None})
            setattr(case, task_id_field, None)
            raise

    if getattr(case, "_defer_timer_publish_until_commit", False):
        transaction.on_commit(publish)
    else:
        publish()
    return task_id


def _claim_due_timer(case_id, task_id, task_id_field, target_time_field):
    """Claim the current timer once; stale and redelivered tasks become no-ops."""
    with transaction.atomic():
        try:
            case = OutageCase.objects.select_for_update().get(case_id=case_id)
        except OutageCase.DoesNotExist:
            return None

        if case.status in INACTIVE_CASE_STATUSES:
            return None

        current_task_id = getattr(case, task_id_field)
        if task_id and current_task_id != task_id:
            return None

        target_time = getattr(case, target_time_field)
        if not target_time or timezone.now() < target_time:
            return None

        setattr(case, task_id_field, None)
        OutageCase.objects.filter(pk=case.pk).update(**{task_id_field: None})
        return case


def _restore_failed_timer_claim(
    case, task_id, task_id_field, target_time_field
):
    if not task_id:
        return
    OutageCase.objects.filter(
        pk=case.pk,
        **{
            task_id_field: None,
            target_time_field: getattr(case, target_time_field),
        },
    ).exclude(status__in=INACTIVE_CASE_STATUSES).update(
        **{task_id_field: task_id}
    )


def _restore_due_sla(case_id, task_id):
    with transaction.atomic():
        try:
            case = OutageCase.objects.select_for_update().get(case_id=case_id)
        except OutageCase.DoesNotExist:
            return False
        if case.status in INACTIVE_CASE_STATUSES:
            return False
        if task_id and case.celery_sla_task_id != task_id:
            return False
        if not case.sla_target_time or timezone.now() < case.sla_target_time:
            return False

        case.celery_sla_task_id = None
        case._closed_loop_kind = SLA_EXPIRED_CLOSED_LOOP_KIND
        case.status = "restored"
        case.save(
            update_fields=["status", "celery_sla_task_id", "updated_at"]
        )
        return True


def _parse_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_time_label(target_time):
    if not target_time:
        return None
    return timezone.localtime(target_time).strftime("%H:%M น.")


def _notification_key(event_type, ca_number, report_id, case_id, message):
    return "|".join(
        [
            str(event_type or ""),
            str(ca_number or ""),
            str(report_id or ""),
            str(case_id or ""),
            str(message or ""),
        ]
    )


def _notification_payload(report, message, event_type, closed_loop_kind=None):
    case_id = str(report.related_case_id) if report.related_case_id else None
    report_id = report.id
    payload = {
        "session_id": report.session_id,
        "ca_number": report.ca_number,
        "message": message,
        "event_type": event_type,
        "report_id": report_id,
        "case_id": case_id,
        "notification_key": _notification_key(
            event_type, report.ca_number, report_id, case_id, message
        ),
    }
    if closed_loop_kind:
        payload["closed_loop_kind"] = closed_loop_kind
    return payload


def _sla_case_start_time(case):
    case_start_times = [
        value for value in [case.sla_reference_time, case.created_at] if value
    ]
    return min(case_start_times) if case_start_times else None


def _ensure_sla(case, reference_time=None, reason="case_created"):
    reference_time = reference_time or _sla_case_start_time(case)
    expected_target_time = (
        reference_time + timedelta(hours=OutageCase.SLA_HOURS)
        if reference_time
        else None
    )
    if (
        case.sla_target_time
        and case.sla_reference_time == reference_time
        and case.sla_target_time == expected_target_time
    ):
        return case

    case.set_sla_target(reference_time=reference_time, reason=reason)
    case.save(
        update_fields=[
            "sla_reference_time",
            "sla_target_time",
            "sla_reason",
            "updated_at",
        ]
    )
    return case


def _notify_active_case_sessions(case, message, event_type, closed_loop_kind=None):
    reports = (
        CustomerReport.objects.filter(related_case=case, is_resolved=False)
        .exclude(session_id__isnull=True)
        .exclude(session_id="")
    )

    sent_session_ids = set()
    last_error = None
    for report in reports:
        if report.session_id in sent_session_ids:
            continue

        payload = _notification_payload(
            report, message, event_type, closed_loop_kind=closed_loop_kind
        )

        try:
            response = requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=5)
            response.raise_for_status()
            sent_session_ids.add(report.session_id)
            print(
                f"[{event_type}] ยิง Webhook แจ้งเตือน CA: {report.ca_number} "
                f"session: {report.session_id} สำเร็จ"
            )
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"[{event_type}] ยิง Webhook ล้มเหลว: {e}")

    if last_error:
        raise last_error
    return sent_session_ids


def _ensure_pluem_etr(case, report_id=None):
    if case.oms_etr or case.pluem_etr_target_time:
        return case

    report = None
    if report_id:
        report = CustomerReport.objects.filter(id=report_id).first()

    if not report or not report.ca_number:
        return case

    payload = {"ca_number": report.ca_number}
    lat = report.latitude if report.latitude is not None else case.latitude
    lon = report.longitude if report.longitude is not None else case.longitude
    if lat is not None and lon is not None:
        payload.update({"lat": lat, "lon": lon})
    assessment = get_pea_assessment(payload)
    etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
    if assessment.get("error") or etr_minutes is None:
        return case

    case.pluem_etr_minutes = etr_minutes
    case.pluem_etr_target_time = timezone.now() + timedelta(minutes=etr_minutes)
    if assessment.get("fastest_branch"):
        case.assessment_fastest_branch = assessment["fastest_branch"]
    if assessment.get("eta_formatted"):
        case.assessment_eta_formatted = assessment["eta_formatted"]
    case.assessment_payload = assessment
    case.save(
        update_fields=[
            "pluem_etr_minutes",
            "pluem_etr_target_time",
            "assessment_fastest_branch",
            "assessment_eta_formatted",
            "assessment_payload",
            "updated_at",
        ]
    )
    return case


@shared_task(
    bind=True,
    autoretry_for=(requests.exceptions.RequestException,),
    retry_kwargs={"max_retries": 3, "countdown": 2},
    retry_backoff=True,
)
def check_eta_timeout(self, case_id, report_id):
    try:
        case = _claim_due_timer(
            case_id,
            self.request.id,
            "celery_eta_task_id",
            "eta_target_time",
        )
        if not case:
            return

        case.sync_affected_ca_numbers()
        if not case.oms_etr:
            case = _ensure_pluem_etr(case, report_id)

        etr_target_time = case.effective_etr_time()
        if etr_target_time:
            etr_label = _format_time_label(etr_target_time)
            message = (
                "ขออัปเดตสถานะค่ะ ขณะนี้ทีมงานกำลังดำเนินการอยู่ "
                f"คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ค่ะ"
            )
        else:
            message = (
                "ขออัปเดตสถานะค่ะ ขณะนี้ทีมงานกำลังดำเนินการอยู่ "
                "ระบบกำลังประเมินเวลาไฟกลับล่าสุดค่ะ"
            )

        try:
            sent_session_ids = _notify_active_case_sessions(
                case, message, "eta_timeout"
            )
        except requests.exceptions.RequestException:
            _restore_failed_timer_claim(
                case,
                self.request.id,
                "celery_eta_task_id",
                "eta_target_time",
            )
            raise
        if not sent_session_ids:
            print(f"[Timer_ETA] ไม่พบ session ที่ต้องแจ้งเตือนสำหรับ Case: {case_id}")

    except OutageCase.DoesNotExist:
        print("[Timer_ETA] ไม่พบข้อมูล Case (อาจถูกลบไปแล้ว)")


@shared_task(
    bind=True,
    autoretry_for=(requests.exceptions.RequestException,),
    retry_kwargs={"max_retries": 3, "countdown": 2},
    retry_backoff=True,
)
def check_etr_timeout(self, case_id):
    try:
        case = _claim_due_timer(
            case_id,
            self.request.id,
            "celery_etr_task_id",
            "oms_etr",
        )
        if not case:
            return

        etr_target_time = case.oms_etr or case.effective_etr_time()
        if not etr_target_time:
            return

        case.sync_affected_ca_numbers()
        case = _ensure_sla(
            case,
            reference_time=_sla_case_start_time(case),
            reason="case_created",
        )

        sla_label = _format_time_label(case.sla_target_time)
        message = (
            f"ขออัปเดตค่ะ การจ่ายไฟจะไม่เกินเวลา {sla_label} ค่ะ"
        )
        try:
            sent_session_ids = _notify_active_case_sessions(
                case, message, "etr_timeout_sla"
            )
        except requests.exceptions.RequestException:
            _restore_failed_timer_claim(
                case,
                self.request.id,
                "celery_etr_task_id",
                "oms_etr",
            )
            raise
        if not sent_session_ids:
            print(f"[Timer_ETR] ไม่พบ session ที่ต้องแจ้งเตือนสำหรับ Case: {case_id}")

    except OutageCase.DoesNotExist:
        print("[Timer_ETR] ไม่พบข้อมูล Case (อาจถูกลบไปแล้ว)")


@shared_task(bind=True)
def check_sla_timeout(self, case_id):
    _restore_due_sla(case_id, self.request.id)


@shared_task(
    autoretry_for=(requests.exceptions.RequestException,),
    retry_kwargs={"max_retries": 3, "countdown": 2},
    retry_backoff=True,
)
def send_proactive_alert(report_id, message, event_type, closed_loop_kind=None):
    """
    ฟังก์ชันกลางสำหรับส่งแจ้งเตือนเชิงรุก (เช่น ช่างปิดงานไฟมาแล้ว, หรือส่ง ETR ครั้งที่ 2)
    """
    try:
        report = CustomerReport.objects.get(id=report_id)
        payload = _notification_payload(
            report, message, event_type, closed_loop_kind=closed_loop_kind
        )
        response = requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
    except CustomerReport.DoesNotExist:
        pass
