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
    """
    Timer_ETA: ทริกเกอร์เมื่อเวลาผ่านไปจนถึง ETA
    เพื่อตรวจสอบว่าช่างถึงหน้างานหรือยัง
    """
    try:
        case = OutageCase.objects.get(case_id=case_id)

        # หากสถานะยังเป็นแค่ 'reported' หรือ 'investigating' แสดงว่าช่างยังไม่แจ้งว่าถึงหน้างาน (Status != Arrived)
        if case.status in ["reported", "investigating"]:
            if not case.oms_etr:
                case = _ensure_pluem_etr(case, report_id)

            message = ""
            etr_target_time = case.effective_etr_time()
            if etr_target_time:
                etr_label = timezone.localtime(etr_target_time).strftime("%H:%M น.")
                etr_source = case.effective_etr_source()
                source_label = "OMS" if etr_source == "oms" else "โมเดล ETR พี่ปลื้ม"
                message = (
                    "ขออภัยที่ช่างถึงหน้างานช้ากว่ากำหนดครับ "
                    f"เวลาที่คาดว่าจะแก้ไขเสร็จและจ่ายไฟคืนจาก {source_label} "
                    f"คือประมาณ {etr_label} ครับ"
                )
            else:
                message = "ขออภัยที่ช่างถึงหน้างานช้ากว่ากำหนดครับ ขณะนี้ยังไม่มี ETR จาก OMS ระบบกำลังเชื่อมต่อกับโมเดล ETR พี่ปลื้มเพื่อประเมินเวลาไฟกลับมาใช้งานได้ครับ"

            reports = CustomerReport.objects.filter(
                related_case=case, is_resolved=False
            ).exclude(session_id__isnull=True).exclude(session_id="")

            sent_session_ids = set()
            for report in reports:
                if report.session_id in sent_session_ids:
                    continue

                payload = {
                    "session_id": report.session_id,
                    "ca_number": report.ca_number,
                    "message": message,
                    "event_type": "eta_timeout",
                }

                try:
                    requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=5)
                    sent_session_ids.add(report.session_id)
                    print(
                        f"[Timer_ETA] ยิง Webhook แจ้งเตือน CA: {report.ca_number} "
                        f"session: {report.session_id} สำเร็จ"
                    )
                except requests.exceptions.RequestException as e:
                    print(f"[Timer_ETA] ยิง Webhook ล้มเหลว: {e}")

            if not sent_session_ids:
                print(f"[Timer_ETA] ไม่พบ session ที่ต้องแจ้งเตือนสำหรับ Case: {case_id}")

    except OutageCase.DoesNotExist:
        print("[Timer_ETA] ไม่พบข้อมูล Case (อาจถูกลบไปแล้ว)")


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
