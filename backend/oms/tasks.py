from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from .models import OutageCase, CustomerReport
from .services import get_pea_assessment
import requests
import os

AGENT_WEBHOOK_URL = os.getenv(
    "AGENT_WEBHOOK_URL", "http://dspy-agent:8000/webhook/notify"
)


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


def _notify_active_case_sessions(case, message, event_type):
    reports = (
        CustomerReport.objects.filter(related_case=case, is_resolved=False)
        .exclude(session_id__isnull=True)
        .exclude(session_id="")
    )

    sent_session_ids = set()
    for report in reports:
        if report.session_id in sent_session_ids:
            continue

        payload = {
            "session_id": report.session_id,
            "ca_number": report.ca_number,
            "message": message,
            "event_type": event_type,
        }

        try:
            requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=5)
            sent_session_ids.add(report.session_id)
            print(
                f"[{event_type}] ยิง Webhook แจ้งเตือน CA: {report.ca_number} "
                f"session: {report.session_id} สำเร็จ"
            )
        except requests.exceptions.RequestException as e:
            print(f"[{event_type}] ยิง Webhook ล้มเหลว: {e}")

    return sent_session_ids


def _ensure_pluem_etr(case, report_id=None):
    if case.oms_etr or case.pluem_etr_target_time:
        return case

    report = None
    if report_id:
        report = CustomerReport.objects.filter(id=report_id).first()

    payload = {
        "lat": case.latitude,
        "lon": case.longitude,
    }
    if report and report.ca_number:
        payload["ca_number"] = report.ca_number

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


@shared_task
def check_eta_timeout(case_id, report_id):
    try:
        case = OutageCase.objects.get(case_id=case_id)
        if case.status == "restored":
            return

        case.sync_affected_ca_numbers()
        if not case.oms_etr:
            case = _ensure_pluem_etr(case, report_id)

        etr_target_time = case.effective_etr_time()
        if etr_target_time:
            etr_label = _format_time_label(etr_target_time)
            message = (
                "ขออัปเดตสถานะครับ ขณะนี้ทีมงานกำลังดำเนินการอยู่ "
                f"คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ครับ"
            )
        else:
            message = (
                "ขออัปเดตสถานะครับ ขณะนี้ทีมงานกำลังดำเนินการอยู่ "
                "ระบบกำลังประเมินเวลาไฟกลับล่าสุด"
            )

        sent_session_ids = _notify_active_case_sessions(
            case, message, "eta_timeout"
        )
        if not sent_session_ids:
            print(f"[Timer_ETA] ไม่พบ session ที่ต้องแจ้งเตือนสำหรับ Case: {case_id}")

    except OutageCase.DoesNotExist:
        print("[Timer_ETA] ไม่พบข้อมูล Case (อาจถูกลบไปแล้ว)")


@shared_task
def check_etr_timeout(case_id):
    try:
        case = OutageCase.objects.get(case_id=case_id)
        if case.status == "restored":
            return

        etr_target_time = case.oms_etr or case.effective_etr_time()
        if not etr_target_time:
            return
        if timezone.now() < etr_target_time:
            return

        case.sync_affected_ca_numbers()
        case = _ensure_sla(
            case,
            reference_time=_sla_case_start_time(case),
            reason=case.sla_reason or "etr_timeout",
        )

        sla_label = _format_time_label(case.sla_target_time)
        message = (
            "เวลาไฟกลับที่ประเมินไว้เลยกำหนดแล้วครับ "
            f"กฟภ.จะเร่งดำเนินการให้ไม่เกิน {sla_label} ครับ"
        )
        sent_session_ids = _notify_active_case_sessions(
            case, message, "etr_timeout_sla"
        )
        if not sent_session_ids:
            print(f"[Timer_ETR] ไม่พบ session ที่ต้องแจ้งเตือนสำหรับ Case: {case_id}")

    except OutageCase.DoesNotExist:
        print("[Timer_ETR] ไม่พบข้อมูล Case (อาจถูกลบไปแล้ว)")


@shared_task
def send_proactive_alert(report_id, message, event_type):
    """
    ฟังก์ชันกลางสำหรับส่งแจ้งเตือนเชิงรุก (เช่น ช่างปิดงานไฟมาแล้ว, หรือส่ง ETR ครั้งที่ 2)
    """
    try:
        report = CustomerReport.objects.get(id=report_id)
        payload = {
            "session_id": report.session_id,
            "ca_number": report.ca_number,
            "message": message,
            "event_type": event_type,
        }
        requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=5)
    except CustomerReport.DoesNotExist:
        pass
