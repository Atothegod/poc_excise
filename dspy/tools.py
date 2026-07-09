import requests
import os
import contextvars
import re
from datetime import datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")
CA_NUMBER_PATTERN = re.compile(r"^\d{12}$")
latest_outage_by_session = {}

current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)
current_login_ca_number = contextvars.ContextVar("current_login_ca_number", default=None)
current_pdpa_consent = contextvars.ContextVar("current_pdpa_consent", default=False)


def is_valid_ca_number(ca_number: str) -> bool:
    return bool(CA_NUMBER_PATTERN.fullmatch(str(ca_number).strip()))


def parse_iso_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def current_authoritative_time():
    value = current_time_stamp.get()
    if value:
        return parse_iso_datetime(value)
    return datetime.now(ZoneInfo("Asia/Bangkok"))


def format_time_only(value):
    target_time = parse_iso_datetime(value)
    if not target_time:
        return None

    return target_time.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H:%M น.")


def format_eta_label(eta, eta_formatted=None):
    eta_label = format_time_only(eta)
    if eta_label:
        return eta_label

    if not eta_formatted:
        return None

    minute_match = re.search(r"(\d+(?:\.\d+)?)", str(eta_formatted))
    if not minute_match:
        return str(eta_formatted)

    eta_minutes = ceil(float(minute_match.group(1)))
    eta_time = current_authoritative_time() + timedelta(minutes=eta_minutes)
    return eta_time.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H:%M น.")


def remember_latest_outage(db_response, ca_number=None):
    session_id = current_session_id.get()
    if not session_id or session_id == "unknown":
        return

    latest_outage_by_session[session_id] = {
        "event_type": db_response.get("event_type"),
        "ca_number": ca_number,
        "case_id": db_response.get("case_id"),
        "lv_group_id": db_response.get("lv_group_id"),
        "affected_ca_numbers": db_response.get("affected_ca_numbers"),
        "report_id": db_response.get("report_id"),
        "eta_target_time": db_response.get("eta_target_time"),
        "eta_formatted": db_response.get("eta_formatted"),
        "fastest_branch": db_response.get("fastest_branch"),
        "oms_etr": db_response.get("oms_etr"),
        "etr_target_time": db_response.get("etr_target_time"),
        "etr_source": db_response.get("etr_source"),
        "sla_target_time": db_response.get("sla_target_time"),
        "sla_reference_time": db_response.get("sla_reference_time"),
        "sla_reason": db_response.get("sla_reason"),
    }


def get_latest_outage(session_id: str):
    return latest_outage_by_session.get(session_id)


def restore_latest_outage(session_id: str, latest_outage: dict | None):
    if not session_id or session_id == "unknown" or not latest_outage:
        return
    latest_outage_by_session[session_id] = latest_outage


def fetch_session_context(session_id: str, ca_number: str | None = None):
    if not session_id or session_id == "unknown":
        return None

    params = {}
    if ca_number and is_valid_ca_number(ca_number):
        params["ca_number"] = ca_number

    try:
        response = requests.get(
            f"{DJANGO_API_URL}/reports/session-context/{session_id}/",
            params=params,
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success":
            return data
    except requests.exceptions.RequestException:
        return None
    return None


def _normalize_dialog_history(history):
    dialog = []
    for item in history:
        raw_role = str(item.get("role", "")).lower()
        if "user" in raw_role:
            role = "user"
        elif "assistant" in raw_role or "agent" in raw_role or "system alert" in raw_role:
            role = "agent"
        else:
            continue

        message = item.get("message") or item.get("content") or ""
        if not message:
            continue

        dialog.append(
            {
                "role": role,
                "message": str(message),
                "timestamp": item.get("timestamp"),
                "event_type": item.get("event_type"),
                "ca_number": item.get("ca_number"),
                "report_id": item.get("report_id"),
                "case_id": item.get("case_id"),
                "notification_key": item.get("notification_key"),
                "closed_loop_kind": item.get("closed_loop_kind"),
            }
        )
    return dialog


def sync_chat_history_to_db(session_id: str, history: list):
    if not session_id or session_id == "unknown":
        return

    latest_outage = latest_outage_by_session.get(session_id) or {}
    ca_number = latest_outage.get("ca_number") or current_login_ca_number.get()
    payload = {
        "session_id": session_id,
        "ca_number": ca_number,
        "chat_history": _normalize_dialog_history(history),
    }

    try:
        requests.post(
            f"{DJANGO_API_URL}/reports/chat-history/",
            json=payload,
            timeout=5,
        )
    except requests.exceptions.RequestException:
        pass


def save_report_to_db(
    ca_number: str, pdpa_consent: bool, force_new_case: bool = False
):
    endpoint = f"{DJANGO_API_URL}/reports/sync/"
    payload = {
        "session_id": current_session_id.get(),
        "ca_number": ca_number,
        "time_stamp": current_time_stamp.get(),
        "pdpa_consent": pdpa_consent,
        "force_new_case": force_new_case,
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


def record_closed_loop_response(
    response: str,
    ca_number: str | None = None,
    report_id: int | None = None,
):
    session_id = current_session_id.get()
    if not session_id or session_id == "unknown":
        return None

    payload = {
        "session_id": session_id,
        "response": response,
    }
    if ca_number and is_valid_ca_number(ca_number):
        payload["ca_number"] = ca_number
    if report_id:
        payload["report_id"] = report_id

    try:
        response_obj = requests.post(
            f"{DJANGO_API_URL}/reports/closed-loop-response/",
            json=payload,
            timeout=5,
        )
        response_obj.raise_for_status()
        return response_obj.json()
    except requests.exceptions.RequestException:
        return None


def _format_etr_label(etr, etr_source=None):
    if not etr:
        return None
    formatted_etr = format_time_only(etr)
    return f"คาดว่าจะจ่ายไฟคืนประมาณ {formatted_etr or etr}"


def _format_mass_outage_label(etr_label=None):
    if etr_label:
        return f"ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ {etr_label} ค่ะ"
    return "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ ระบบกำลังประเมินเวลาไฟกลับล่าสุดค่ะ"


def _format_branch_label(fastest_branch):
    if not fastest_branch:
        return None
    return f"{fastest_branch} รับเรื่องแล้วค่ะ"


def _format_eta_detail(eta_label):
    if not eta_label:
        return None
    return f"ช่างจะถึงหน้างานประมาณ {eta_label}"


def _join_branch_eta_etr(branch_label=None, eta_label=None, etr_label=None):
    eta_detail = _format_eta_detail(eta_label)
    details = []

    if branch_label and eta_detail:
        details.append(f"{branch_label} {eta_detail}")
    elif branch_label:
        details.append(branch_label)
    elif eta_detail:
        details.append(eta_detail)

    if etr_label:
        details.append(etr_label)

    return " และ".join(details)


def Check_Outage_Tool(
    ca_number: str, pdpa_consent: bool = False, force_new_case: bool = False
):
    login_ca_number = current_login_ca_number.get()
    if login_ca_number and is_valid_ca_number(login_ca_number):
        ca_number = login_ca_number
    else:
        ca_number = str(ca_number).strip()
    pdpa_consent = bool(pdpa_consent or current_pdpa_consent.get())

    if not is_valid_ca_number(ca_number):
        return "[CA_INVALID] ไม่พบหมายเลข CA จากหน้าเข้าสู่ระบบ กรุณากลับไปเข้าสู่ระบบใหม่"

    if not pdpa_consent:
        return "[CONSENT_REQUIRED] ยังไม่ได้รับ PDPA consent จากหน้าเข้าสู่ระบบ กรุณากลับไปเข้าสู่ระบบใหม่"

    db_response = save_report_to_db(
        ca_number, pdpa_consent, force_new_case=force_new_case
    )

    if not db_response:
        return "ขัดข้อง ไม่สามารถเชื่อมต่อกับระบบได้"

    event_type = db_response.get("event_type")
    eta = db_response.get("eta_target_time")
    fastest_branch = db_response.get("fastest_branch")
    etr = db_response.get("etr_target_time") or db_response.get("oms_etr")
    etr_label = _format_etr_label(etr, db_response.get("etr_source"))
    sla = db_response.get("sla_target_time")
    sla_label = format_time_only(sla)
    remember_latest_outage(db_response, ca_number=ca_number)

    # เคส 1: API ขัดข้องติดต่อกันจนครบกำหนด
    if event_type == "api_error":
        return "ขัดข้อง: API_Timeout เกิน 3 ครั้ง โปรดแจ้งลูกค้าว่าเปลี่ยนสถานะเป็นโอนสายให้ Human Agent"

    if event_type == "ca_not_found":
        return "[CA_NOT_FOUND] ไม่พบหมายเลข CA นี้ในฐานข้อมูลพิกัดลูกค้า กรุณาตรวจสอบหมายเลข CA อีกครั้ง หรือโอนให้เจ้าหน้าที่ช่วยตรวจสอบ"

    if event_type == "assessment_error":
        return "[FallBack] ระบบประเมินเวลาเข้าหน้างานหรือเวลาไฟกลับขัดข้อง ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่"

    if event_type == "etr_timeout_sla":
        if sla_label:
            return f"[อัปเดตการจ่ายไฟ] แจ้งว่า ขออัปเดตค่ะ การจ่ายไฟจะไม่เกินเวลา {sla_label} ค่ะ"
        return "[อัปเดตการจ่ายไฟ] แจ้งว่า ขออัปเดตค่ะ ระบบกำลังเร่งดำเนินการจ่ายไฟคืนค่ะ"

    # เคส 2: เกิดเหตุวงกว้าง (Mass Outage) -> บังคับแจ้ง ETR ตาม Rule 6
    if event_type == "existing_ca_case":
        eta_label = format_eta_label(eta, db_response.get("eta_formatted")) or eta
        branch_label = _format_branch_label(fastest_branch)
        detail_text = _join_branch_eta_etr(branch_label, eta_label, etr_label)
        if detail_text:
            return f"[เคสเดิมของ CA] แจ้งว่า {detail_text}"
        return "[เคสเดิมของ CA] ระบบพบเคสที่เปิดอยู่แล้ว แต่ยังไม่มีเวลาประเมินล่าสุด"

    if event_type in {"mass_outage", "repeated_event"}:
        return f"[เหตุวงกว้าง] {_format_mass_outage_label(etr_label)}"

    # เคส 3: แจ้งครั้งแรก (New Event) หรือ เคสเดี่ยว -> บังคับแจ้ง ETA ตาม Rule 7
    # และตรวจสอบ ETR เพิ่มเติม
    elif event_type == "new_event":
        eta_label = format_eta_label(eta, db_response.get("eta_formatted")) or eta
        branch_label = _format_branch_label(fastest_branch)
        detail_text = _join_branch_eta_etr(branch_label, eta_label, etr_label)
        if not detail_text:
            detail_text = "ระบบกำลังประเมินเวลาช่างเข้าหน้างานล่าสุด"
        return f"[เหตุแจ้งใหม่] เปิดใบงานแล้ว แจ้งว่า {detail_text}"

    return "ขัดข้อง ไม่สามารถระบุประเภทเหตุการณ์ได้ค่ะ"
