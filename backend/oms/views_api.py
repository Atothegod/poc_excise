import math
from datetime import timedelta

from rest_framework import serializers
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.utils import timezone

from pea_project.celery import app as celery_app

from .case_logic import (
    CASE_LINK_RADIUS_KM,
    CASE_TYPE_FAST_TRACK,
    CASE_TYPE_MASS_OUTAGE,
    CASE_TYPE_NORMAL,
    INACTIVE_CASE_STATUSES,
    MASS_OUTAGE_CONFIRMATION_COUNT,
    STATUS_MERGED,
)
from .models import CustomerLocation, CustomerReport, OutageCase
from .serializers import (
    AgentReportSerializer,
    ActionStatusRequestSerializer,
    ActionStatusResponseSerializer,
    CaValidationSerializer,
    ChatHistorySyncSerializer,
    SessionLoginSerializer,
    validate_ca_number_format,
)
from .services import format_minutes_label, get_pea_assessment, parse_eta_minutes
from .tasks import check_eta_timeout, send_proactive_alert


def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Return the distance in km only when two points are inside the case-link radius.
    Anything farther away is treated as outside any existing active case.
    """
    if None in [lat1, lon1, lat2, lon2]:
        return float("inf")

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
    if distance_km > CASE_LINK_RADIUS_KM:
        return float("inf")
    return distance_km


def _nearby_case_sort_key(candidate):
    distance, case = candidate
    created_at = case.created_at or timezone.now()
    return (distance, created_at, case.lv_group_id or 0, str(case.case_id))


def _anchor_case_sort_key(candidate):
    _distance, case = candidate
    created_at = case.created_at or timezone.now()
    return (created_at, case.lv_group_id or 0, str(case.case_id))


def _active_case_queryset():
    return OutageCase.objects.exclude(status__in=INACTIVE_CASE_STATUSES).order_by(
        "created_at", "lv_group_id", "case_id"
    )


def _cases_within_radius(latitude, longitude, case_type=None):
    candidates = []
    active_cases = _active_case_queryset()
    if case_type:
        active_cases = active_cases.filter(case_type=case_type)

    for case in active_cases:
        distance = calculate_distance(latitude, longitude, case.latitude, case.longitude)
        if distance <= CASE_LINK_RADIUS_KM:
            candidates.append((distance, case))

    return candidates


def _nearest_mass_outage_case(latitude, longitude):
    candidates = _cases_within_radius(
        latitude, longitude, case_type=CASE_TYPE_MASS_OUTAGE
    )
    if not candidates:
        return None
    return min(candidates, key=_nearby_case_sort_key)[1]


def _normal_anchor_case(latitude, longitude):
    candidates = _cases_within_radius(latitude, longitude, case_type=CASE_TYPE_NORMAL)
    if not candidates:
        return None
    return min(candidates, key=_anchor_case_sort_key)[1]


def _normal_cases_in_anchor_radius(anchor_case):
    if not anchor_case or anchor_case.latitude is None or anchor_case.longitude is None:
        return []

    candidates = []
    for case in _active_case_queryset().filter(case_type=CASE_TYPE_NORMAL):
        distance = calculate_distance(
            anchor_case.latitude,
            anchor_case.longitude,
            case.latitude,
            case.longitude,
        )
        if distance <= CASE_LINK_RADIUS_KM:
            candidates.append((distance, case))
    return [case for _distance, case in sorted(candidates, key=_anchor_case_sort_key)]


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
            "case_type": None,
            "oms_etr_updated_at": None,
            "sla_reference_time": None,
            "sla_target_time": None,
            "sla_reason": None,
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
        "case_type": case.case_type,
        "oms_etr_updated_at": _datetime_iso(case.oms_etr_updated_at),
        "sla_reference_time": _datetime_iso(case.sla_reference_time),
        "sla_target_time": _datetime_iso(case.sla_target_time),
        "sla_reason": case.sla_reason or None,
    }


def _get_or_create_active_report(session_id, ca_number):
    report = (
        CustomerReport.objects.filter(
            session_id=session_id, ca_number=ca_number, is_resolved=False
        )
        .order_by("-updated_at")
        .first()
    )
    if report:
        return report, False
    return (
        CustomerReport.objects.create(session_id=session_id, ca_number=ca_number),
        True,
    )


def _apply_customer_location(report, ca_number):
    customer_location = CustomerLocation.objects.filter(ca_number=ca_number).first()
    if not customer_location:
        return

    report.customer_name = customer_location.fullname
    report.latitude = customer_location.latitude
    report.longitude = customer_location.longitude


def _grant_pdpa_consent(report):
    report.pdpa_consent = True
    if not report.pdpa_consent_at:
        report.pdpa_consent_at = timezone.now()


def _has_active_related_case(report):
    return bool(
        report
        and report.related_case_id
        and report.related_case
        and report.related_case.status not in INACTIVE_CASE_STATUSES
    )


def _active_reports_for_ca(ca_number):
    return (
        CustomerReport.objects.select_related("related_case")
        .filter(
            ca_number=ca_number,
            is_resolved=False,
            related_case__isnull=False,
        )
        .exclude(related_case__status__in=INACTIVE_CASE_STATUSES)
        .order_by("-updated_at")
    )


def _latest_active_report_for_ca(ca_number, exclude_report_id=None):
    reports = _active_reports_for_ca(ca_number)
    if exclude_report_id:
        reports = reports.exclude(id=exclude_report_id)
    return reports.first()


def _active_fast_track_case_for_ca(ca_number):
    report = (
        _active_reports_for_ca(ca_number)
        .filter(related_case__case_type=CASE_TYPE_FAST_TRACK)
        .first()
    )
    return report.related_case if report else None


def _select_fast_track_report(ca_number, session_id=None):
    if session_id:
        report = (
            CustomerReport.objects.filter(session_id=session_id, ca_number=ca_number)
            .order_by("-updated_at")
            .first()
        )
        if report:
            return report

        report = CustomerReport(session_id=session_id, ca_number=ca_number)
        _apply_customer_location(report, ca_number)
        report.save()
        return report

    return (
        CustomerReport.objects.filter(ca_number=ca_number)
        .order_by("-updated_at")
        .first()
    )


def _case_start_reference(case, fallback=None):
    if not case:
        return fallback

    case_start_times = [
        value for value in [case.sla_reference_time, case.created_at] if value
    ]
    return min(case_start_times) if case_start_times else fallback


def _fast_track_sla_reference(report):
    reference_time = _case_start_reference(
        report.related_case if report else None,
        fallback=None,
    )
    return reference_time or timezone.now()


def _attach_active_ca_case(report):
    if _has_active_related_case(report):
        return False

    active_report = _latest_active_report_for_ca(
        report.ca_number, exclude_report_id=report.id
    )
    if not active_report or not active_report.related_case:
        return False

    report.related_case = active_report.related_case
    return True


def _attach_waiting_same_ca_reports(report):
    if not report.related_case_id:
        return 0

    return (
        CustomerReport.objects.filter(
            ca_number=report.ca_number,
            is_resolved=False,
            related_case__isnull=True,
        )
        .exclude(id=report.id)
        .update(related_case=report.related_case)
    )


def _existing_case_event_type(case):
    if case and case.case_type == CASE_TYPE_MASS_OUTAGE:
        return "mass_outage"
    return "existing_ca_case"


def _create_case_for_report(report, ca_number):
    base_time = report.time_stamp if report.time_stamp else timezone.now()
    assessment = get_pea_assessment(
        {
            "ca_number": ca_number,
            "lat": report.latitude,
            "lon": report.longitude,
        }
    )
    if assessment.get("error"):
        return None, {
            "status": "assessment_error",
            "event_type": "assessment_error",
            "message": assessment["error"],
            "report_id": report.id,
            **_case_response_fields(None),
        }

    eta_minutes = parse_eta_minutes(assessment.get("eta_formatted"))
    if eta_minutes is None:
        return None, {
            "status": "assessment_error",
            "event_type": "assessment_error",
            "message": "assessment response does not contain a usable ETA",
            "report_id": report.id,
            **_case_response_fields(None),
        }

    etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
    eta_target_time = base_time + timedelta(minutes=eta_minutes)
    pluem_etr_target_time = (
        base_time + timedelta(minutes=etr_minutes)
        if etr_minutes is not None
        else None
    )

    new_case = OutageCase.objects.create(
        title=f"แจ้งไฟดับจาก CA {ca_number}",
        case_type=CASE_TYPE_NORMAL,
        latitude=report.latitude,
        longitude=report.longitude,
        eta_target_time=eta_target_time,
        sla_reference_time=base_time,
        sla_target_time=base_time + timedelta(hours=OutageCase.SLA_HOURS),
        sla_reason="case_created",
        assessment_fastest_branch=assessment.get("fastest_branch") or "",
        assessment_eta_formatted=assessment.get("eta_formatted")
        or format_minutes_label(eta_minutes)
        or "",
        assessment_eta_minutes=eta_minutes,
        pluem_etr_minutes=etr_minutes,
        pluem_etr_target_time=pluem_etr_target_time,
        assessment_payload=assessment,
    )

    task = check_eta_timeout.apply_async(
        args=[new_case.case_id, report.id], eta=eta_target_time
    )
    new_case.celery_eta_task_id = task.id
    new_case.save(update_fields=["celery_eta_task_id"])
    return new_case, None


def _format_time_label(target_time):
    if not target_time:
        return None
    return timezone.localtime(target_time).strftime("%H:%M น.")


def _ensure_case_model_etr(case, ca_number=None):
    if not case or case.effective_etr_time():
        return case

    payload = {
        "lat": case.latitude,
        "lon": case.longitude,
    }
    if ca_number:
        payload["ca_number"] = ca_number

    assessment = get_pea_assessment(payload)
    etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
    if assessment.get("error") or etr_minutes is None:
        return case

    case.pluem_etr_minutes = etr_minutes
    case.pluem_etr_target_time = timezone.now() + timedelta(minutes=etr_minutes)
    if assessment.get("fastest_branch"):
        case.assessment_fastest_branch = assessment["fastest_branch"]
    if assessment.get("eta_formatted"):
        case.assessment_eta_formatted = assessment["eta_formatted"]
    case.assessment_payload = assessment
    case.save(
        update_fields=[
            "pluem_etr_minutes",
            "pluem_etr_target_time",
            "assessment_fastest_branch",
            "assessment_eta_formatted",
            "assessment_payload",
            "updated_at",
        ]
    )
    return case


def _mass_outage_message(case):
    etr_label = _format_time_label(case.effective_etr_time())
    if etr_label:
        return (
            "เกิดเหตุไฟดับวงกว้างในพื้นที่ครับ "
            f"คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ครับ"
        )
    return (
        "เกิดเหตุไฟดับวงกว้างในพื้นที่ครับ "
        "ระบบกำลังประเมินเวลาไฟกลับล่าสุด"
    )


def _revoke_case_timers(case):
    for task_id in [case.celery_eta_task_id, case.celery_etr_task_id]:
        if not task_id:
            continue
        try:
            celery_app.control.revoke(task_id, terminate=True)
        except Exception:
            pass


def _active_ca_numbers_for_cases(cases):
    return set(
        CustomerReport.objects.filter(
            related_case__in=cases,
            is_resolved=False,
        )
        .exclude(ca_number="")
        .values_list("ca_number", flat=True)
        .distinct()
    )


def _notify_mass_outage_sessions(case, message, exclude_session_id=None):
    reports = (
        CustomerReport.objects.filter(related_case=case, is_resolved=False)
        .exclude(session_id__isnull=True)
        .exclude(session_id="")
        .order_by("created_at", "id")
    )

    sent_session_ids = set()
    for report in reports:
        if report.session_id == exclude_session_id:
            continue
        if report.session_id in sent_session_ids:
            continue

        sent_session_ids.add(report.session_id)
        send_proactive_alert.delay(
            report_id=report.id,
            message=message,
            event_type="mass_outage",
        )
    return sent_session_ids


def _promote_mass_outage_if_threshold(anchor_case, trigger_report):
    normal_cases = _normal_cases_in_anchor_radius(anchor_case)
    if len(_active_ca_numbers_for_cases(normal_cases)) < MASS_OUTAGE_CONFIRMATION_COUNT:
        return None

    now = timezone.now()
    child_cases = [case for case in normal_cases if case.case_id != anchor_case.case_id]

    for child_case in child_cases:
        _revoke_case_timers(child_case)

    if child_cases:
        CustomerReport.objects.filter(related_case__in=child_cases).update(
            related_case=anchor_case
        )
        OutageCase.objects.filter(case_id__in=[case.case_id for case in child_cases]).update(
            status=STATUS_MERGED,
            merged_into=anchor_case,
            merged_at=now,
            celery_eta_task_id=None,
            celery_etr_task_id=None,
            updated_at=now,
        )

    anchor_case.case_type = CASE_TYPE_MASS_OUTAGE
    anchor_case.save(update_fields=["case_type", "updated_at"])
    anchor_case = _ensure_case_model_etr(anchor_case, trigger_report.ca_number)
    anchor_case.sync_affected_ca_numbers()

    _notify_mass_outage_sessions(
        anchor_case,
        _mass_outage_message(anchor_case),
        exclude_session_id=trigger_report.session_id,
    )
    return anchor_case


def _should_include_model_etr(case):
    return bool(
        case
        and (
            case.case_type == CASE_TYPE_MASS_OUTAGE
            or (case.eta_target_time and timezone.now() >= case.eta_target_time)
        )
        and not case.oms_etr
    )


def _latest_outage_for_report(report):
    case = report.related_case
    if not case:
        return None

    event_type = (
        "restored" if case.status in INACTIVE_CASE_STATUSES else "active_case_exists"
    )

    effective_etr = case.oms_etr
    etr_source = "oms" if case.oms_etr else None
    now = timezone.now()
    is_active = case.status not in INACTIVE_CASE_STATUSES

    if is_active and case.case_type == CASE_TYPE_MASS_OUTAGE:
        event_type = "mass_outage"
        effective_etr = case.effective_etr_time()
        etr_source = case.effective_etr_source()
    elif is_active and case.eta_target_time and now >= case.eta_target_time:
        event_type = "eta_timeout"

    if not effective_etr and event_type == "eta_timeout":
        effective_etr = case.pluem_etr_target_time
        etr_source = case.effective_etr_source()

    if is_active and effective_etr and now >= effective_etr:
        event_type = "etr_timeout_sla"
    elif is_active and case.case_type == CASE_TYPE_FAST_TRACK:
        event_type = "fast_track_existing"

    return {
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
        "case_type": case.case_type,
        "oms_etr_updated_at": _datetime_iso(case.oms_etr_updated_at),
        "sla_reference_time": _datetime_iso(case.sla_reference_time),
        "sla_target_time": _datetime_iso(case.sla_target_time),
        "sla_reason": case.sla_reason or None,
        "pluem_etr_minutes": case.pluem_etr_minutes
        if event_type == "eta_timeout" and not case.oms_etr
        else None,
        "pluem_etr_target_time": _datetime_iso(case.pluem_etr_target_time)
        if event_type == "eta_timeout" and not case.oms_etr
        else None,
    }


def _session_context_payload(session_id, report):
    if not report:
        return {
            "status": "not_found",
            "session_id": session_id,
            "chat_history": [],
            "latest_outage": None,
        }

    return {
        "status": "success",
        "session_id": session_id,
        "report_id": report.id,
        "ca_number": report.ca_number,
        "chat_history": report.chat_history or [],
        "latest_outage": _latest_outage_for_report(report),
    }


def _select_session_report(session_id, ca_number=None):
    reports = CustomerReport.objects.filter(session_id=session_id)
    if ca_number:
        reports = reports.filter(ca_number=ca_number)

    report = reports.filter(is_resolved=False).order_by("-updated_at").first()
    if not report:
        report = reports.order_by("-updated_at").first()
    return report


@api_view(["POST"])
def sync_agent_report(request):
    serializer = AgentReportSerializer(data=request.data)
    if serializer.is_valid():
        data = serializer.validated_data
        session_id = data.get("session_id")
        ca_number = data.get("ca_number")

        report, created = _get_or_create_active_report(session_id, ca_number)

        _apply_customer_location(report, ca_number)

        if data.get("latitude") is not None:
            report.latitude = data.get("latitude")
        if data.get("longitude") is not None:
            report.longitude = data.get("longitude")
        if data.get("time_stamp"):
            report.time_stamp = data.get("time_stamp")
        if data.get("pdpa_consent"):
            _grant_pdpa_consent(report)

        event_type = "new_event"

        if _has_active_related_case(report) and not created:
            event_type = _existing_case_event_type(report.related_case)
        elif _attach_active_ca_case(report):
            event_type = _existing_case_event_type(report.related_case)

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
            mass_case = _nearest_mass_outage_case(report.latitude, report.longitude)
            if mass_case:
                mass_case = _ensure_case_model_etr(mass_case, ca_number)
                report.related_case = mass_case
                event_type = "mass_outage"
            else:
                anchor_case = _normal_anchor_case(report.latitude, report.longitude)
                new_case, error_data = _create_case_for_report(report, ca_number)
                if error_data:
                    report.save()
                    return Response(error_data)

                report.related_case = new_case
                report.save()
                promoted_case = (
                    _promote_mass_outage_if_threshold(anchor_case, report)
                    if anchor_case
                    else None
                )
                if promoted_case:
                    report.related_case = promoted_case
                    report.save(update_fields=["related_case", "updated_at"])
                    event_type = "mass_outage"

        report.save()
        if report.related_case:
            _attach_waiting_same_ca_reports(report)
            report.related_case.sync_affected_ca_numbers()

        if (
            report.related_case
            and report.related_case.status not in INACTIVE_CASE_STATUSES
        ):
            effective_etr = report.related_case.effective_etr_time()
            if effective_etr and timezone.now() >= effective_etr:
                event_type = "etr_timeout_sla"

        response_data = {
            "status": "success",
            "event_type": event_type,
            "report_id": report.id,
            **_case_response_fields(
                report.related_case,
                include_model_etr=_should_include_model_etr(report.related_case),
            ),
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
        report = _latest_active_report_for_ca(ca_number)
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


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def validate_ca_login(request):
    serializer = CaValidationSerializer(data=request.query_params)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    ca_number = serializer.validated_data["ca_number"]
    customer_location = CustomerLocation.objects.filter(ca_number=ca_number).first()
    if not customer_location:
        return Response(
            {
                "status": "not_found",
                "message": "ไม่พบหมายเลข CA นี้ในฐานข้อมูลลูกค้า",
            },
            status=404,
        )

    return Response(
        {
            "status": "success",
            "ca_number": ca_number,
            "customer_name": customer_location.fullname,
        }
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def register_session_login(request):
    serializer = SessionLoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    data = serializer.validated_data
    if not CustomerLocation.objects.filter(ca_number=data["ca_number"]).exists():
        return Response(
            {
                "status": "not_found",
                "message": "ไม่พบหมายเลข CA นี้ในฐานข้อมูลลูกค้า",
            },
            status=404,
        )

    report, _created = _get_or_create_active_report(
        data["session_id"], data["ca_number"]
    )
    _apply_customer_location(report, data["ca_number"])
    _grant_pdpa_consent(report)
    _attach_active_ca_case(report)
    report.save()

    if report.related_case:
        _attach_waiting_same_ca_reports(report)
        report.related_case.sync_affected_ca_numbers()

    return Response(_session_context_payload(data["session_id"], report))


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
    ca_number = request.query_params.get("ca_number")
    if ca_number:
        try:
            ca_number = validate_ca_number_format(ca_number)
        except serializers.ValidationError as e:
            return Response({"error": str(e)}, status=400)

    report = _select_session_report(session_id, ca_number=ca_number)
    return Response(_session_context_payload(session_id, report))


# --- API ใหม่สำหรับ Anti-Loop (Fast Track) ---
@api_view(["POST"])
def fast_track_report(request):
    """
    เปิดเคสเร่งด่วนเมื่อลูกค้ายังไม่มีไฟหลังระบบปิดเคสเดิมแล้ว
    """
    ca_number = request.data.get("ca_number")
    session_id = str(request.data.get("session_id") or "").strip() or None
    if session_id == "unknown":
        session_id = None
    try:
        ca_number = validate_ca_number_format(ca_number)
    except serializers.ValidationError as e:
        return Response({"error": str(e)}, status=400)

    report = _select_fast_track_report(ca_number, session_id=session_id)

    if not report:
        return Response({"event_type": "fallback", "message": "ไม่พบข้อมูลประวัติ"})

    _apply_customer_location(report, ca_number)

    active_fast_track_case = _active_fast_track_case_for_ca(ca_number)
    if active_fast_track_case:
        report.is_resolved = False
        report.related_case = active_fast_track_case
        report.save()
        _attach_waiting_same_ca_reports(report)
        active_fast_track_case.sync_affected_ca_numbers()
        return Response(
            {
                "event_type": "fast_track_existing",
                "message": "รับเรื่องไว้ในเคสเร่งด่วนเดิมแล้วครับ",
                "case_id": str(active_fast_track_case.case_id),
                "lv_group_id": active_fast_track_case.lv_group_id,
                "sla_target_time": _datetime_iso(active_fast_track_case.sla_target_time),
                "sla_reference_time": _datetime_iso(
                    active_fast_track_case.sla_reference_time
                ),
                "sla_reason": active_fast_track_case.sla_reason,
            }
        )

    base_time = _fast_track_sla_reference(report)
    new_case = OutageCase.objects.create(
        title=f"[ด่วน! ไฟดับซ้ำซ้อน] CA {ca_number}",
        case_type=CASE_TYPE_FAST_TRACK,
        status="reported",
        latitude=report.latitude or 13.0,
        longitude=report.longitude or 100.0,
        sla_reference_time=base_time,
        sla_target_time=base_time + timedelta(hours=OutageCase.SLA_HOURS),
        sla_reason="fast_track",
    )
    report.is_resolved = False
    report.related_case = new_case
    report.save()
    new_case.sync_affected_ca_numbers()

    return Response(
        {
            "event_type": "fast_track_created",
            "message": "เปิดเคสเร่งด่วนให้แล้วครับ",
            "case_id": str(new_case.case_id),
            "lv_group_id": new_case.lv_group_id,
            "sla_target_time": _datetime_iso(new_case.sla_target_time),
            "sla_reference_time": _datetime_iso(new_case.sla_reference_time),
            "sla_reason": new_case.sla_reason,
        }
    )
