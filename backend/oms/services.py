import os
import re

import requests


PEA_ASSESSMENT_URL = os.getenv(
    "PEA_ASSESSMENT_URL",
    "https://pea-estimated.services.storemesh.com/api/v1/assessment/",
)


def get_pea_assessment(payload: dict) -> dict:
    """
    ดึงข้อมูลประเมินไฟดับจาก pea-estimated.services แล้วคืนเฉพาะค่าที่ OMS ใช้ต่อ
    """
    has_ca_number = bool(payload.get("ca_number"))
    has_coordinates = payload.get("lat") is not None and payload.get("lon") is not None
    if not has_ca_number and not has_coordinates:
        return {"error": "Require 'ca_number' OR both 'lat' and 'lon'"}

    try:
        response = requests.get(PEA_ASSESSMENT_URL, params=payload, timeout=15)
        response.raise_for_status()
        data = response.json()

        routing = data.get("routing_eta", {})
        fastest_branch = routing.get("fastest_branch")

        eta_formatted = None
        for detail in routing.get("details", []):
            if detail.get("branch") == fastest_branch:
                eta_formatted = detail.get("eta_formatted")
                break

        estimated_etr = data.get("historical_etr", {}).get("estimated_etr_minutes")

        return {
            "fastest_branch": fastest_branch,
            "eta_formatted": eta_formatted,
            "estimated_etr_minutes": estimated_etr,
        }

    except requests.exceptions.RequestException as exc:
        return {"error": f"Connection error: {exc}"}
    except ValueError as exc:
        return {"error": f"Invalid JSON response: {exc}"}


def parse_eta_minutes(eta_formatted):
    """
    แปลง label เช่น '~ 8 min', '1 hr 20 min', '8 นาที' ให้เป็นจำนวนนาที
    """
    if eta_formatted is None:
        return None

    value = str(eta_formatted).strip().lower()
    if not value:
        return None

    total_minutes = 0.0
    matched_unit = False

    hour_pattern = r"(\d+(?:\.\d+)?)\s*(?:h|hr|hour|hours|ชั่วโมง|ชม\.?)"
    minute_pattern = r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes|นาที)"

    for amount in re.findall(hour_pattern, value):
        total_minutes += float(amount) * 60
        matched_unit = True

    for amount in re.findall(minute_pattern, value):
        total_minutes += float(amount)
        matched_unit = True

    if matched_unit:
        return total_minutes

    fallback = re.search(r"\d+(?:\.\d+)?", value)
    if fallback:
        return float(fallback.group())

    return None


def format_minutes_label(minutes):
    if minutes is None:
        return None

    rounded_minutes = int(round(float(minutes)))
    return f"~ {rounded_minutes} min"
