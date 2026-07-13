import os
import re

import requests

from session_state import current_login_ca_number, current_session_id, current_time_stamp


DJANGO_API_URL = os.getenv("DJANGO_API_URL", "http://backend:8000/api")
CA_NUMBER_PATTERN = re.compile(r"^\d{12}$")


def is_valid_ca_number(ca_number: str) -> bool:
    return bool(CA_NUMBER_PATTERN.fullmatch(str(ca_number).strip()))


def fetch_session_context(
    session_id: str,
    ca_number: str | None = None,
    before_message_id: int | None = None,
):
    if not session_id or session_id == "unknown":
        return None

    params = {}
    if ca_number and is_valid_ca_number(ca_number):
        params["ca_number"] = ca_number
    if before_message_id is not None:
        params["before_message_id"] = before_message_id

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
