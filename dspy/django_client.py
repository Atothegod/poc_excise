import os
import re

import requests

from session_state import current_login_ca_number, current_session_id, current_time_stamp
from session_state import get_latest_outage


DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")
CA_NUMBER_PATTERN = re.compile(r"^\d{12}$")


def is_valid_ca_number(ca_number: str) -> bool:
    return bool(CA_NUMBER_PATTERN.fullmatch(str(ca_number).strip()))


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

    latest_outage = get_latest_outage(session_id) or {}
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
        except requests.exceptions.RequestException:
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
