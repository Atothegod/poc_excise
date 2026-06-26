import requests
import os
import contextvars
import re
from datetime import datetime

DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")
CA_NUMBER_PATTERN = re.compile(r"^\d{11,12}$")
latest_outage_by_session = {}

current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)


def is_valid_ca_number(ca_number: str) -> bool:
    return bool(CA_NUMBER_PATTERN.fullmatch(str(ca_number).strip()))


def parse_iso_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def remember_latest_outage(db_response):
    session_id = current_session_id.get()
    if not session_id or session_id == "unknown":
        return

    latest_outage_by_session[session_id] = {
        "event_type": db_response.get("event_type"),
        "case_id": db_response.get("case_id"),
        "report_id": db_response.get("report_id"),
        "eta_target_time": db_response.get("eta_target_time"),
        "eta_formatted": db_response.get("eta_formatted"),
        "fastest_branch": db_response.get("fastest_branch"),
        "oms_etr": db_response.get("oms_etr"),
        "etr_target_time": db_response.get("etr_target_time"),
        "etr_source": db_response.get("etr_source"),
    }


def get_latest_outage(session_id: str):
    return latest_outage_by_session.get(session_id)


def save_report_to_db(ca_number: str, pdpa_consent: bool):
    endpoint = f"{DJANGO_API_URL}/reports/sync/"
    payload = {
        "session_id": current_session_id.get(),
        "ca_number": ca_number,
        "time_stamp": current_time_stamp.get(),
        "pdpa_consent": pdpa_consent,
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(endpoint, json=payload, timeout=25)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                return {"event_type": "api_error"}
    return None


def _format_etr_label(etr, etr_source):
    if not etr:
        return None
    if etr_source == "pluem_model":
        return f"ETR จากโมเดลพี่ปลื้ม: {etr}"
    return f"ETR จาก OMS: {etr}"


def Check_Outage_Tool(ca_number: str, pdpa_consent: bool = False):
    ca_number = str(ca_number).strip()

    if not is_valid_ca_number(ca_number):
        return "[CA_INVALID] หมายเลขผู้ใช้ไฟต้องเป็นตัวเลข 11 หรือ 12 หลักเท่านั้น ห้ามมีตัวอักษรหรืออักขระอื่นปน"

    if not pdpa_consent:
        return "[CONSENT_REQUIRED] ต้องขออนุญาตลูกค้าก่อนใช้ Check_Outage_Tool เพื่อตรวจสอบข้อมูลไฟดับจากหมายเลขผู้ใช้ไฟ"

    db_response = save_report_to_db(ca_number, pdpa_consent)

    if not db_response:
        return "ขัดข้อง ไม่สามารถเชื่อมต่อกับระบบได้"

    event_type = db_response.get("event_type")
    eta = db_response.get("eta_target_time")
    eta_formatted = db_response.get("eta_formatted")
    fastest_branch = db_response.get("fastest_branch")
    etr = db_response.get("etr_target_time") or db_response.get("oms_etr")
    etr_label = _format_etr_label(etr, db_response.get("etr_source"))
    remember_latest_outage(db_response)

    # เคส 1: API ขัดข้องติดต่อกันจนครบกำหนด
    if event_type == "api_error":
        return "ขัดข้อง: API_Timeout เกิน 3 ครั้ง โปรดแจ้งลูกค้าว่าเปลี่ยนสถานะเป็นโอนสายให้ Human Agent"

    if event_type == "ca_not_found":
        return "[CA_NOT_FOUND] ไม่พบหมายเลข CA นี้ในฐานข้อมูลพิกัดลูกค้า กรุณาตรวจสอบหมายเลข CA อีกครั้ง หรือโอนให้เจ้าหน้าที่ช่วยตรวจสอบ"

    if event_type == "assessment_error":
        return "[FallBack] ระบบประเมิน ETA/ETR ขัดข้อง ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่"

    # เคส 2: เกิดเหตุวงกว้าง (Mass Outage) -> บังคับแจ้ง ETR ตาม Rule 6
    if event_type == "repeated_event":
        if etr_label:
            return f"[เหตุวงกว้าง] แจ้ง {etr_label} แก่ลูกค้า"
        else:
            return "[เหตุวงกว้าง] กำลังเชื่อมต่อกับโมเดล ETR พี่ปลื้มครับ"

    # เคส 3: แจ้งครั้งแรก (New Event) หรือ เคสเดี่ยว -> บังคับแจ้ง ETA ตาม Rule 7
    # และตรวจสอบ ETR เพิ่มเติม
    elif event_type == "new_event":
        eta_label = eta_formatted or eta
        branch_label = f" สาขาที่ประเมินว่าไปถึงเร็วที่สุดคือ {fastest_branch}" if fastest_branch else ""
        if etr_label:
            return (
                f"[เหตุแจ้งใหม่] ระบบได้เปิดใบงานใหม่แล้ว{branch_label} "
                f"ให้แจ้งเวลาที่ช่างจะเดินทางไปถึง (ETA): {eta_label} "
                f"และแจ้งเวลาที่คาดว่าจะแก้ไขเสร็จ ({etr_label})"
            )
        else:
            return (
                f"[เหตุแจ้งใหม่] ระบบได้เปิดใบงานใหม่แล้ว{branch_label} "
                f"ให้แจ้งเวลาที่ช่างจะเดินทางไปถึง (ETA): {eta_label}"
            )

    return "ขัดข้อง ไม่สามารถระบุประเภทเหตุการณ์ได้"


def Fast_Track_Tool(ca_number: str):
    """
    เครื่องมือสำหรับใช้สร้างตั๋ว Fast-track ด่วน
    เมื่อลูกค้าบอกว่าไฟยังไม่มาและเช็คเบรกเกอร์แล้ว
    """
    ca_number = str(ca_number).strip()
    if not is_valid_ca_number(ca_number):
        return "[CA_INVALID] หมายเลขผู้ใช้ไฟต้องเป็นตัวเลข 11 หรือ 12 หลักเท่านั้น ห้ามมีตัวอักษรหรืออักขระอื่นปน"

    endpoint = f"{DJANGO_API_URL}/reports/fast-track/"
    payload = {"ca_number": ca_number}

    try:
        response = requests.post(endpoint, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get("event_type") == "fallback_to_human":
            return "[FallBack] โควต้าแจ้งซ้ำหมดแล้ว ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่ (Force Fallback)"
        else:
            return "[Success] สร้างตั๋ว Fast-track สำเร็จ ให้ตอบลูกค้าว่าประสานงานด่วนแล้ว"

    except Exception as e:
        return "[FallBack] ระบบขัดข้อง ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่ (Force Fallback)"
