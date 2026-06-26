import math
from datetime import timedelta

from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone

from .models import CustomerLocation, CustomerReport, OutageCase
from .serializers import (
    AgentReportSerializer,
    ActionStatusRequestSerializer,
    ActionStatusResponseSerializer,
    ChatHistorySyncSerializer,
    validate_ca_number_format,
)
from .services import format_minutes_label, get_pea_assessment, parse_eta_minutes
from .tasks import check_eta_timeout


def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Keep outage cases effectively independent.

    The caller still uses the legacy `dist <= 5.0` check, so this function only
    returns a finite distance when two reports are within about 10 cm of each
    other. Anything farther away is treated as outside the linking radius.
    """
    if None in [lat1, lon1, lat2, lon2]:
        return float("inf")

    case_link_radius_km = 0.0001
    R = 6371.0
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance_km = R * c
    if distance_km > case_link_radius_km:
        return float("inf")
    return distance_km


def _parse_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime_iso(value):
    return value.isoformat() if value else None


def _case_response_fields(case, include_model_etr=False):
    if not case:
        return {
            "case_id": None,
            "lv_group_id": None,
            "affected_ca_numbers": [],
            "eta_target_time": None,
            "eta_formatted": None,
            "fastest_branch": None,
            "oms_etr": None,
            "pluem_etr_minutes": None,
            "pluem_etr_target_time": None,
            "etr_target_time": None,
            "etr_source": None,
        }

    etr_target_time = case.oms_etr
    etr_source = "oms" if case.oms_etr else None
    pluem_etr_minutes = None
    pluem_etr_target_time = None

    if include_model_etr and not case.oms_etr:
        etr_target_time = case.pluem_etr_target_time
        etr_source = case.effective_etr_source()
        pluem_etr_minutes = case.pluem_etr_minutes
        pluem_etr_target_time = case.pluem_etr_target_time

    return {
        "case_id": case.case_id,
        "lv_group_id": case.lv_group_id,
        "affected_ca_numbers": case.affected_ca_numbers or [],
        "eta_target_time": _datetime_iso(case.eta_target_time),
        "eta_formatted": case.assessment_eta_formatted or None,
        "fastest_branch": case.assessment_fastest_branch or None,
        "oms_etr": _datetime_iso(case.oms_etr),
        "pluem_etr_minutes": pluem_etr_minutes,
        "pluem_etr_target_time": _datetime_iso(pluem_etr_target_time),
        "etr_target_time": _datetime_iso(etr_target_time),
        "etr_source": etr_source,
    }


@api_view(["POST"])
def sync_agent_report(request):
    serializer = AgentReportSerializer(data=request.data)
    if serializer.is_valid():
        data = serializer.validated_data
        session_id = data.get("session_id")
        ca_number = data.get("ca_number")

        report, created = CustomerReport.objects.get_or_create(
            session_id=session_id, ca_number=ca_number, is_resolved=False
        )

        customer_location = CustomerLocation.objects.filter(ca_number=ca_number).first()
        if customer_location:
            report.customer_name = customer_location.fullname
            report.latitude = customer_location.latitude
            report.longitude = customer_location.longitude

        if data.get("latitude") is not None:
            report.latitude = data.get("latitude")
        if data.get("longitude") is not None:
            report.longitude = data.get("longitude")
        if data.get("time_stamp"):
            report.time_stamp = data.get("time_stamp")
        if data.get("pdpa_consent"):
            report.pdpa_consent = True
            if not report.pdpa_consent_at:
                report.pdpa_consent_at = timezone.now()

        event_type = "new_event"

        has_coordinates = report.latitude is not None and report.longitude is not None
        if not has_coordinates and not report.related_case:
            report.save()
            return Response(
                {
                    "status": "not_found",
                    "event_type": "ca_not_found",
                    "message": "ไม่พบ CA ในฐานข้อมูลพิกัดลูกค้า",
                    "report_id": report.id,
                    **_case_response_fields(None),
                }
            )

        if has_coordinates and not report.related_case:
            active_cases = OutageCase.objects.exclude(status="restored")
            nearest_case = None

            for case in active_cases:
                dist = calculate_distance(
                    report.latitude, report.longitude, case.latitude, case.longitude
                )
                print(f"DEBUG: Checking case {case.case_id}, Distance: {dist}")
                if dist <= 5.0:
                    nearest_case = case
                    event_type = "repeated_event"
                    break

            if nearest_case:
                report.related_case = nearest_case
            else:
                base_time = report.time_stamp if report.time_stamp else timezone.now()
                assessment = get_pea_assessment(
                    {
                        "ca_number": ca_number,
                        "lat": report.latitude,
                        "lon": report.longitude,
                    }
                )
                if assessment.get("error"):
                    report.save()
                    return Response(
                        {
                            "status": "assessment_error",
                            "event_type": "assessment_error",
                            "message": assessment["error"],
                            "report_id": report.id,
                            **_case_response_fields(None),
                        }
                    )

                eta_minutes = parse_eta_minutes(assessment.get("eta_formatted"))
                if eta_minutes is None:
                    report.save()
                    return Response(
                        {
                            "status": "assessment_error",
                            "event_type": "assessment_error",
                            "message": "assessment response does not contain a usable ETA",
                            "report_id": report.id,
                            **_case_response_fields(None),
                        }
                    )

                etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
                eta_target_time = base_time + timedelta(minutes=eta_minutes)
                pluem_etr_target_time = (
                    base_time + timedelta(minutes=etr_minutes)
                    if etr_minutes is not None
                    else None
                )

                new_case = OutageCase.objects.create(
                    title=f"แจ้งไฟดับจาก CA {ca_number}",
                    latitude=report.latitude,
                    longitude=report.longitude,
                    eta_target_time=eta_target_time,
                    assessment_fastest_branch=assessment.get("fastest_branch") or "",
                    assessment_eta_formatted=assessment.get("eta_formatted")
                    or format_minutes_label(eta_minutes)
                    or "",
                    assessment_eta_minutes=eta_minutes,
                    pluem_etr_minutes=etr_minutes,
                    pluem_etr_target_time=pluem_etr_target_time,
                    assessment_payload=assessment,
                )
                report.related_case = new_case

                # สั่งรัน Task และเก็บ task_id ลง Database
                task = check_eta_timeout.apply_async(
                    args=[new_case.case_id, report.id], eta=eta_target_time
                )
                new_case.celery_eta_task_id = task.id
                new_case.save(update_fields=["celery_eta_task_id"])

        report.save()
        if report.related_case:
            report.related_case.sync_affected_ca_numbers()

        response_data = {
            "status": "success",
            "event_type": event_type,
            "report_id": report.id,
            **_case_response_fields(report.related_case),
        }
        return Response(response_data)
    return Response(serializer.errors, status=400)


@api_view(["GET"])
def get_action_status(request):
    request_serializer = ActionStatusRequestSerializer(data=request.query_params)
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=400)

    ca_number = request_serializer.validated_data.get("ca_number")
    try:
        report = CustomerReport.objects.filter(
            ca_number=ca_number, is_resolved=False
        ).last()
        response_data = {
            "status": "first_time",
            "case_id": None,
            "case_status": None,
            "case_status_display": None,
            "oms_etr": None,
            "etr_target_time": None,
            "etr_source": None,
            "pluem_etr_minutes": None,
        }
        if report and report.related_case:
            case = report.related_case
            response_data.update(
                {
                    "status": "active_case_exists",
                    "case_id": case.case_id,
                    "case_status": case.status,
                    "case_status_display": case.get_status_display(),
                    "oms_etr": case.oms_etr if case.oms_etr else None,
                    "etr_target_time": case.oms_etr if case.oms_etr else None,
                    "etr_source": "oms" if case.oms_etr else None,
                    "pluem_etr_minutes": None,
                }
            )
        response_serializer = ActionStatusResponseSerializer(response_data)
        return Response(response_serializer.data)
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
def sync_chat_history(request):
    serializer = ChatHistorySyncSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    data = serializer.validated_data
    session_id = data["session_id"]
    ca_number = data.get("ca_number")
    chat_history = data.get("chat_history") or []

    reports = CustomerReport.objects.filter(session_id=session_id)
    if ca_number:
        reports = reports.filter(ca_number=ca_number)

    report = reports.filter(is_resolved=False).order_by("-updated_at").first()
    if not report:
        report = reports.order_by("-updated_at").first()

    if not report:
        return Response({"status": "no_report"})

    report.chat_history = chat_history
    report.save(update_fields=["chat_history", "updated_at"])
    return Response({"status": "success", "report_id": report.id})


@api_view(["GET"])
def get_session_context(request, session_id):
    reports = CustomerReport.objects.filter(session_id=session_id)
    report = reports.filter(is_resolved=False).order_by("-updated_at").first()
    if not report:
        report = reports.order_by("-updated_at").first()

    if not report:
        return Response(
            {
                "status": "not_found",
                "session_id": session_id,
                "chat_history": [],
                "latest_outage": None,
            }
        )

    case = report.related_case
    latest_outage = None
    if case:
        event_type = "restored" if case.status == "restored" else "active_case_exists"

        if case.status in ["reported", "investigating"] and case.eta_target_time:
            if timezone.now() >= case.eta_target_time:
                event_type = "eta_timeout"

        effective_etr = case.oms_etr
        etr_source = "oms" if case.oms_etr else None
        if not effective_etr and event_type == "eta_timeout":
            effective_etr = case.pluem_etr_target_time
            etr_source = case.effective_etr_source()

        latest_outage = {
            "event_type": event_type,
            "ca_number": report.ca_number,
            "case_id": str(case.case_id),
            "lv_group_id": case.lv_group_id,
            "affected_ca_numbers": case.affected_ca_numbers or [],
            "report_id": report.id,
            "eta_target_time": _datetime_iso(case.eta_target_time),
            "eta_formatted": case.assessment_eta_formatted or None,
            "fastest_branch": case.assessment_fastest_branch or None,
            "oms_etr": _datetime_iso(case.oms_etr),
            "etr_target_time": _datetime_iso(effective_etr),
            "etr_source": etr_source,
            "pluem_etr_minutes": case.pluem_etr_minutes
            if event_type == "eta_timeout" and not case.oms_etr
            else None,
            "pluem_etr_target_time": _datetime_iso(case.pluem_etr_target_time)
            if event_type == "eta_timeout" and not case.oms_etr
            else None,
        }

    return Response(
        {
            "status": "success",
            "session_id": session_id,
            "report_id": report.id,
            "ca_number": report.ca_number,
            "chat_history": report.chat_history or [],
            "latest_outage": latest_outage,
        }
    )


# --- API ใหม่สำหรับ Anti-Loop (Fast Track) ---
@api_view(["POST"])
def fast_track_report(request):
    """
    รับคำสั่งจากการยืนยันสวิตช์เบรกเกอร์ของลูกค้า
    """
    ca_number = request.data.get("ca_number")
    try:
        ca_number = validate_ca_number_format(ca_number)
    except serializers.ValidationError as e:
        return Response({"error": str(e)}, status=400)

    # ดึงประวัติลูกค้าล่าสุดที่เคสเพิ่งถูกปิดไป (is_resolved=True ล่าสุด) หรือเคสเดิม
    report = (
        CustomerReport.objects.filter(ca_number=ca_number)
        .order_by("-updated_at")
        .first()
    )

    if not report:
        return Response({"event_type": "fallback", "message": "ไม่พบข้อมูลประวัติ"})

    if report.fast_track_quota > 0:
        # หักโควต้าและสร้างเคส Fast Track
        report.fast_track_quota -= 1

        # รีเซ็ตสถานะเป็นรอการแก้ไข
        report.is_resolved = False

        new_case = OutageCase.objects.create(
            title=f"[ด่วน! ไฟดับซ้ำซ้อน] CA {ca_number}",
            status="reported",
            latitude=report.latitude or 13.0,
            longitude=report.longitude or 100.0,
        )
        report.related_case = new_case
        report.save()
        new_case.sync_affected_ca_numbers()

        return Response(
            {
                "event_type": "fast_track_created",
                "message": "ส่งเรื่องตรวจสอบซ้ำ (Fast-track) ให้ช่างเรียบร้อยแล้ว",
            }
        )
    else:
        # โควต้าหมด (ป้องกันการวนลูป) -> เปลี่ยนเป็นโอนสาย
        return Response(
            {
                "event_type": "fallback_to_human",
                "message": "โควต้าการตรวจสอบซ้ำหมดแล้ว ระบบกำลังโอนสายให้เจ้าหน้าที่",
            }
        )
