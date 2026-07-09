import contextvars


latest_outage_by_session = {}

current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)
current_login_ca_number = contextvars.ContextVar("current_login_ca_number", default=None)
current_pdpa_consent = contextvars.ContextVar("current_pdpa_consent", default=False)


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
