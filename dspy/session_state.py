import contextvars

current_session_id = contextvars.ContextVar("current_session_id", default="unknown")
current_time_stamp = contextvars.ContextVar("current_time_stamp", default=None)
current_login_ca_number = contextvars.ContextVar("current_login_ca_number", default=None)
current_pdpa_consent = contextvars.ContextVar("current_pdpa_consent", default=False)
current_latest_outage = contextvars.ContextVar("current_latest_outage", default=None)
current_conversation_state = contextvars.ContextVar(
    "current_conversation_state", default=None
)
current_chat_history = contextvars.ContextVar("current_chat_history", default="")
current_heart_history = contextvars.ContextVar("current_heart_history", default="")
current_question = contextvars.ContextVar("current_question", default="")
current_pending_closed_loop_prompt = contextvars.ContextVar(
    "current_pending_closed_loop_prompt", default=None
)
current_tools_use = contextvars.ContextVar("current_tools_use", default=())
current_tool_results = contextvars.ContextVar("current_tool_results", default=None)
current_tool_errors = contextvars.ContextVar("current_tool_errors", default=())
current_closed_loop_recorded = contextvars.ContextVar(
    "current_closed_loop_recorded", default=False
)


def reset_tool_execution():
    current_tools_use.set(())
    current_tool_results.set({})
    current_tool_errors.set(())
    current_closed_loop_recorded.set(False)


def record_tool_call(tool_name: str):
    calls = list(current_tools_use.get())
    calls.append(tool_name)
    current_tools_use.set(tuple(calls))


def record_tool_result(tool_name: str, result):
    results = dict(current_tool_results.get() or {})
    results[tool_name] = result
    current_tool_results.set(results)


def record_tool_error(tool_name: str):
    errors = list(current_tool_errors.get())
    errors.append(tool_name)
    current_tool_errors.set(tuple(errors))


def remember_latest_outage(db_response, ca_number=None):
    session_id = current_session_id.get()
    if not session_id or session_id == "unknown":
        return

    current_latest_outage.set({
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
        "case_type": db_response.get("case_type"),
    })


def get_latest_outage(session_id: str):
    return current_latest_outage.get()


def restore_latest_outage(session_id: str, latest_outage: dict | None):
    current_latest_outage.set(latest_outage)
