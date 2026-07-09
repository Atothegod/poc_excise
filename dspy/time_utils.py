import re
from datetime import datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from session_state import current_time_stamp


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
