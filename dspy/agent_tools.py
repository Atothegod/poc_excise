from django_client import (
    is_valid_ca_number,
    record_closed_loop_response,
    save_report_to_db,
)
from response_formatters import (
    format_branch_label,
    format_etr_label,
    format_mass_outage_label,
    join_branch_eta_etr,
)
from session_state import (
    current_closed_loop_recorded,
    current_conversation_state,
    current_login_ca_number,
    current_pending_closed_loop_prompt,
    current_pdpa_consent,
    record_tool_call,
    record_tool_error,
    record_tool_result,
    remember_latest_outage,
)
from time_utils import format_eta_label, format_time_only


def Check_Outage_Tool(
    ca_number: str, pdpa_consent: bool = False, force_new_case: bool = False
):
    """Check or open a PEA outage case after a separate user confirmation.

    Never call this for the first outage-related message. It is allowed only
    after the previous state records a pending confirmation, for follow-up on
    an active outage, or for a confirmed closed-loop re-report.
    """
    record_tool_call("Check_Outage_Tool")
    try:
        result = _check_outage(
            ca_number,
            pdpa_consent=pdpa_consent,
            force_new_case=force_new_case,
        )
    except Exception:
        record_tool_error("Check_Outage_Tool")
        result = (
            "[FallBack] ระบบตรวจสอบเหตุขัดข้องไม่พร้อมใช้งาน "
            "ให้ตอบว่ากำลังโอนสายให้เจ้าหน้าที่"
        )
    record_tool_result("Check_Outage_Tool", result)
    return result


def _check_outage(
    ca_number: str, pdpa_consent: bool = False, force_new_case: bool = False
):
    pending_prompt = current_pending_closed_loop_prompt.get()
    previous_state = current_conversation_state.get()
    if isinstance(previous_state, dict):
        confirmation_pending = bool(
            previous_state.get("outage_confirmation_pending")
        )
        previous_flow_step = previous_state.get("flow_step")
    else:
        confirmation_pending = bool(
            getattr(previous_state, "outage_confirmation_pending", False)
        )
        previous_flow_step = getattr(previous_state, "flow_step", None)

    active_outage_steps = {
        "providing_eta_first",
        "existing_case_providing_eta",
        "mass_outage_providing_etr",
        "eta_timeout_waiting_etr",
        "etr_timeout_sla",
    }
    confirmed_closed_loop = bool(force_new_case and pending_prompt)
    confirmed_active_outage = previous_flow_step in active_outage_steps
    if force_new_case and not confirmed_closed_loop:
        return (
            "[OUTAGE_CONFIRMATION_REQUIRED] "
            "การเปิดเหตุใหม่ต้องเป็นคำตอบต่อจากคำถามยืนยันของระบบ"
        )
    if not (
        confirmation_pending
        or confirmed_closed_loop
        or confirmed_active_outage
    ):
        return (
            "[OUTAGE_CONFIRMATION_REQUIRED] "
            "ต้องได้รับคำยืนยันจากผู้ใช้ในคนละข้อความก่อนตรวจสอบหรือเปิดใบงาน"
        )

    login_ca_number = current_login_ca_number.get()
    if login_ca_number and is_valid_ca_number(login_ca_number):
        ca_number = login_ca_number
    else:
        ca_number = str(ca_number).strip()
    pdpa_consent = bool(pdpa_consent or current_pdpa_consent.get())

    if force_new_case and pending_prompt and not current_closed_loop_recorded.get():
        record_closed_loop_response(
            "still_out",
            ca_number=ca_number or None,
            report_id=pending_prompt.get("report_id"),
        )
        current_closed_loop_recorded.set(True)

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
    etr_label = format_etr_label(etr, db_response.get("etr_source"))
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
        branch_label = format_branch_label(fastest_branch)
        detail_text = join_branch_eta_etr(branch_label, eta_label, etr_label)
        if detail_text:
            return f"[เคสเดิมของ CA] แจ้งว่า {detail_text}"
        return "[เคสเดิมของ CA] ระบบพบเคสที่เปิดอยู่แล้ว แต่ยังไม่มีเวลาประเมินล่าสุด"

    if event_type in {"mass_outage", "repeated_event"}:
        return f"[เหตุวงกว้าง] {format_mass_outage_label(etr_label)}"

    # เคส 3: แจ้งครั้งแรก (New Event) หรือ เคสเดี่ยว -> บังคับแจ้ง ETA ตาม Rule 7
    # และตรวจสอบ ETR เพิ่มเติม
    elif event_type == "new_event":
        eta_label = format_eta_label(eta, db_response.get("eta_formatted")) or eta
        branch_label = format_branch_label(fastest_branch)
        detail_text = join_branch_eta_etr(branch_label, eta_label, etr_label)
        if not detail_text:
            detail_text = "ระบบกำลังประเมินเวลาช่างเข้าหน้างานล่าสุด"
        return f"[เหตุแจ้งใหม่] เปิดใบงานแล้ว แจ้งว่า {detail_text}"

    return "ขัดข้อง ไม่สามารถระบุประเภทเหตุการณ์ได้ค่ะ"
