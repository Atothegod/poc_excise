from datetime import timedelta
from uuid import UUID

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .case_logic import (
    CASE_TYPE_MASS_OUTAGE,
    CASE_TYPE_NORMAL,
    INACTIVE_CASE_STATUSES,
    STATUS_MERGED,
    STATUS_REPORTED,
    STATUS_RESTORED,
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
from .tasks import check_eta_timeout, check_etr_timeout, send_proactive_alert
from pea_project.celery import app as celery_app


OMS_GROUP_MIN_CA_COUNT = 2


def _parse_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime_iso(value):
    return value.isoformat() if value else None


def _parse_oms_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


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
            "external_event_id": None,
            "outage_time": None,
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
        "external_event_id": case.external_event_id or None,
        "outage_time": _datetime_iso(case.outage_time),
        "oms_etr_updated_at": _datetime_iso(case.oms_etr_updated_at),
        "sla_reference_time": _datetime_iso(case.sla_reference_time),
        "sla_target_time": _datetime_iso(case.sla_target_time),
        "sla_reason": case.sla_reason or None,
    }


def _include_model_etr_for_response(case, event_type=None):
    if event_type == "mass_outage":
        return True
    return _should_include_model_etr(case)


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


def _grant_pdpa_consent(report):
    report.pdpa_consent = True
    if not report.pdpa_consent_at:
        report.pdpa_consent_at = timezone.now()


def _customer_location_for_ca(ca_number):
    return CustomerLocation.objects.filter(ca_number=ca_number).first()


def _apply_customer_location(report, customer):
    report.customer_name = customer.fullname or ""
    report.latitude = customer.latitude
    report.longitude = customer.longitude


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


def _attach_active_ca_case(report):
    if _has_active_related_case(report):
        return False

    active_report = _latest_active_report_for_ca(
        report.ca_number, exclude_report_id=report.id
    )
    if active_report and active_report.related_case:
        report.related_case = active_report.related_case
        return True

    for case in (
        OutageCase.objects.exclude(status__in=INACTIVE_CASE_STATUSES)
        .order_by("-updated_at", "-created_at")
        .only("case_id", "affected_ca_numbers", "status")
    ):
        if report.ca_number in (case.affected_ca_numbers or []):
            report.related_case = case
            return True
    return False


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


def _attach_reports_for_case(case):
    affected_ca_numbers = case.affected_ca_numbers or []
    if not affected_ca_numbers:
        return 0
    return CustomerReport.objects.filter(
        ca_number__in=affected_ca_numbers,
        is_resolved=False,
    ).update(related_case=case)


def _existing_case_event_type(case):
    if case and case.case_type == CASE_TYPE_MASS_OUTAGE:
        return "mass_outage"
    return "existing_ca_case"


def _case_id_from_oms(oms_case, required=True):
    raw_case_id = oms_case.get("case_id")
    if not raw_case_id:
        if required:
            raise ValueError("OMS response does not contain case_id")
        return None
    try:
        return UUID(str(raw_case_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("OMS response contains invalid case_id") from exc


def _case_type_from_oms(oms_case, affected_ca_numbers):
    case_type = oms_case.get("case_type")
    if affected_ca_numbers:
        if len(affected_ca_numbers) >= OMS_GROUP_MIN_CA_COUNT:
            return CASE_TYPE_MASS_OUTAGE
        return CASE_TYPE_NORMAL
    if case_type in {CASE_TYPE_NORMAL, CASE_TYPE_MASS_OUTAGE}:
        return case_type
    return CASE_TYPE_NORMAL


def _find_oms_case(case_id, external_event_id):
    if external_event_id:
        case = OutageCase.objects.filter(external_event_id=external_event_id).first()
        if case:
            return case
    if case_id:
        return OutageCase.objects.filter(case_id=case_id).first()
    return None


def _upsert_oms_case(oms_case, trigger_signals=False):
    case_id = _case_id_from_oms(oms_case, required=False)
    external_event_id = (oms_case.get("external_event_id") or "").strip() or None
    status = oms_case.get("status") or "reported"
    affected_ca_numbers = sorted(set(oms_case.get("affected_ca_numbers") or []))
    case_type = _case_type_from_oms(oms_case, affected_ca_numbers)
    outage_time = _parse_oms_datetime(oms_case.get("outage_time"))
    oms_etr = _parse_oms_datetime(
        oms_case.get("oms_etr") or oms_case.get("etr_target_time")
    )

    if not case_id and not external_event_id:
        raise ValueError("OMS event must contain case_id or external_event_id")

    defaults = {
        "title": f"OMS case {external_event_id or case_id}",
        "case_type": case_type,
        "status": status,
        "external_event_id": external_event_id,
        "affected_ca_numbers": affected_ca_numbers,
        "outage_time": outage_time,
        "oms_etr": oms_etr,
        "sla_reference_time": timezone.now(),
        "sla_target_time": timezone.now() + timedelta(hours=OutageCase.SLA_HOURS),
        "sla_reason": "case_created",
    }
    if case_id:
        defaults["case_id"] = case_id

    case = _find_oms_case(case_id, external_event_id)
    if not case:
        case = OutageCase.objects.create(**defaults)
        return case, True

    fields = {"status": status}
    if "case_type" in oms_case or "affected_ca_numbers" in oms_case:
        fields["case_type"] = case_type
    if external_event_id:
        fields["external_event_id"] = external_event_id
    if "affected_ca_numbers" in oms_case:
        fields["affected_ca_numbers"] = affected_ca_numbers
    if "outage_time" in oms_case:
        fields["outage_time"] = outage_time
    if "oms_etr" in oms_case or "etr_target_time" in oms_case:
        fields["oms_etr"] = oms_etr

    if trigger_signals:
        for field, value in fields.items():
            setattr(case, field, value)
        case.save(update_fields=[*fields.keys(), "updated_at"])
    else:
        OutageCase.objects.filter(pk=case.pk).update(**fields, updated_at=timezone.now())
        case.refresh_from_db()
    return case, False


def _assessment_request_payload(ca_number, report):
    payload = {"ca_number": ca_number}
    if report.latitude is not None and report.longitude is not None:
        payload.update({"lat": report.latitude, "lon": report.longitude})
    return payload


def _prepare_assessment_fields(ca_number, base_time, report):
    assessment = get_pea_assessment(_assessment_request_payload(ca_number, report)) or {}
    assessment_error = assessment.get("error")
    eta_minutes = None if assessment_error else parse_eta_minutes(
        assessment.get("eta_formatted")
    )

    if eta_minutes is None:
        return (
            assessment_error or "assessment response does not contain a usable ETA",
            None,
        )

    etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
    eta_target_time = base_time + timedelta(minutes=eta_minutes)
    pluem_etr_target_time = (
        base_time + timedelta(minutes=etr_minutes)
        if etr_minutes is not None
        else None
    )

    return (
        None,
        {
            "eta_target_time": eta_target_time,
            "sla_reference_time": base_time,
            "sla_target_time": base_time + timedelta(hours=OutageCase.SLA_HOURS),
            "sla_reason": "case_created",
            "assessment_fastest_branch": assessment.get("fastest_branch") or "",
            "assessment_eta_formatted": (
                assessment.get("eta_formatted")
                or format_minutes_label(eta_minutes)
                or ""
            ),
            "assessment_eta_minutes": eta_minutes,
            "pluem_etr_minutes": etr_minutes,
            "pluem_etr_target_time": pluem_etr_target_time,
            "assessment_payload": assessment,
        },
    )


def _apply_assessment_fields_to_case(case, assessment_fields, report):
    for field, value in assessment_fields.items():
        setattr(case, field, value)
    OutageCase.objects.filter(pk=case.pk).update(
        **assessment_fields,
        updated_at=timezone.now(),
    )
    case.refresh_from_db()

    task = check_eta_timeout.apply_async(
        args=[case.case_id, report.id],
        eta=assessment_fields["eta_target_time"],
    )
    case.celery_eta_task_id = task.id
    case.save(update_fields=["celery_eta_task_id", "updated_at"])
    return None


def _apply_assessment_to_case(case, ca_number, base_time, report):
    if case.eta_target_time:
        return None

    assessment_error, assessment_fields = _prepare_assessment_fields(
        ca_number, base_time, report
    )
    if assessment_error:
        return assessment_error
    return _apply_assessment_fields_to_case(case, assessment_fields, report)


def _format_time_label(target_time):
    if not target_time:
        return None
    return timezone.localtime(target_time).strftime("%H:%M น.")


def _mass_outage_message(case):
    etr_label = _format_time_label(case.effective_etr_time())
    if etr_label:
        return (
            "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ "
            f"คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ค่ะ"
        )
    return (
        "ขณะนี้เกิดเหตุไฟดับวงกว้างในพื้นที่ค่ะ "
        "ระบบกำลังประเมินเวลาไฟกลับล่าสุดค่ะ"
    )


def _notify_mass_outage_sessions(case, exclude_session_id=None):
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
            message=_mass_outage_message(case),
            event_type="mass_outage",
        )
    return sent_session_ids


def _revoke_case_timers(case):
    if case.celery_eta_task_id:
        celery_app.control.revoke(case.celery_eta_task_id, terminate=True)
    if case.celery_etr_task_id:
        celery_app.control.revoke(case.celery_etr_task_id, terminate=True)


def _schedule_initial_oms_etr(case):
    if (
        not case.oms_etr
        or case.celery_etr_task_id
        or case.status in INACTIVE_CASE_STATUSES
    ):
        return False

    task = check_etr_timeout.apply_async(args=[case.case_id], eta=case.oms_etr)
    etr_updated_at = timezone.now()
    case.oms_etr_updated_at = etr_updated_at
    case.celery_etr_task_id = task.id
    OutageCase.objects.filter(pk=case.pk).update(
        oms_etr_updated_at=etr_updated_at,
        celery_etr_task_id=task.id,
    )
    return True


def _mark_superseded_cases_as_merged(case, affected_ca_numbers):
    if not affected_ca_numbers or case.case_type != CASE_TYPE_MASS_OUTAGE:
        return 0

    superseded_cases = (
        OutageCase.objects.filter(
            affected_customers__ca_number__in=affected_ca_numbers,
            affected_customers__is_resolved=False,
        )
        .exclude(pk=case.pk)
        .exclude(status__in=INACTIVE_CASE_STATUSES)
        .distinct()
    )

    updated_count = 0
    for superseded_case in superseded_cases:
        _revoke_case_timers(superseded_case)
        superseded_case.status = STATUS_MERGED
        superseded_case.merged_into = case
        superseded_case.merged_at = timezone.now()
        superseded_case.celery_eta_task_id = None
        superseded_case.celery_etr_task_id = None
        superseded_case.save(
            update_fields=[
                "status",
                "merged_into",
                "merged_at",
                "celery_eta_task_id",
                "celery_etr_task_id",
                "updated_at",
            ]
        )
        updated_count += 1
    return updated_count


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
        "customer_name": report.customer_name,
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
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    data = serializer.validated_data
    session_id = data.get("session_id")
    ca_number = data.get("ca_number")
    customer = _customer_location_for_ca(ca_number)
    if not customer:
        return Response(
            {
                "status": "not_found",
                "event_type": "ca_not_found",
                "message": "ไม่พบหมายเลข CA นี้ในฐานข้อมูลลูกค้า",
                "report_id": None,
                **_case_response_fields(None),
            },
            status=404,
        )

    report, _created = _get_or_create_active_report(session_id, ca_number)
    _apply_customer_location(report, customer)
    if data.get("time_stamp"):
        report.time_stamp = data.get("time_stamp")
    if data.get("pdpa_consent"):
        _grant_pdpa_consent(report)
    report.save()

    if not _has_active_related_case(report):
        _attach_active_ca_case(report)
        if report.related_case_id:
            report.save(update_fields=["related_case", "updated_at"])

    case = report.related_case if _has_active_related_case(report) else None
    event_type = _existing_case_event_type(case) if case else "new_event"

    if event_type == "new_event":
        base_time = report.time_stamp if report.time_stamp else timezone.now()
        assessment_error, assessment_fields = _prepare_assessment_fields(
            ca_number, base_time, report
        )
        if assessment_error:
            return Response(
                {
                    "status": "assessment_error",
                    "event_type": "assessment_error",
                    "message": assessment_error,
                    "report_id": report.id,
                    **_case_response_fields(None),
                }
            )

        case = OutageCase.objects.create(
            title=f"ไฟดับ CA {ca_number}",
            case_type=CASE_TYPE_NORMAL,
            status=STATUS_REPORTED,
            affected_ca_numbers=[ca_number],
            latitude=customer.latitude,
            longitude=customer.longitude,
        )
        report.related_case = case
        report.save(update_fields=["related_case", "updated_at"])
        _attach_waiting_same_ca_reports(report)
        case.sync_affected_ca_numbers()
        _apply_assessment_fields_to_case(case, assessment_fields, report)

    if case.status not in INACTIVE_CASE_STATUSES:
        effective_etr = case.effective_etr_time()
        if effective_etr and timezone.now() >= effective_etr:
            event_type = "etr_timeout_sla"

    return Response(
        {
            "status": "success",
            "event_type": event_type,
            "report_id": report.id,
            **_case_response_fields(
                case,
                include_model_etr=_include_model_etr_for_response(
                    case, event_type=event_type
                ),
            ),
        }
    )


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
    customer = _customer_location_for_ca(ca_number)
    if not customer:
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
            "customer_name": customer.fullname or "",
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
    customer = _customer_location_for_ca(data["ca_number"])
    if not customer:
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
    _apply_customer_location(report, customer)
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


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def oms_event_callback(request):
    event_type = request.data.get("event_type")
    oms_case = request.data.get("case") or {}
    if not event_type or not oms_case:
        return Response({"status": "error", "message": "event_type and case are required"}, status=400)

    try:
        case, created = _upsert_oms_case(
            oms_case,
            trigger_signals=True,
        )
    except ValueError as exc:
        return Response({"status": "error", "message": str(exc)}, status=400)

    merged_count = 0
    if event_type == "case_opened":
        merged_count = _mark_superseded_cases_as_merged(
            case, case.affected_ca_numbers or []
        )

    _attach_reports_for_case(case)
    case.sync_affected_ca_numbers()
    etr_timer_scheduled = _schedule_initial_oms_etr(case) if created else False
    notified_sessions = set()
    if (
        event_type == "case_opened"
        and case.case_type == CASE_TYPE_MASS_OUTAGE
        and case.status not in INACTIVE_CASE_STATUSES
    ):
        notified_sessions = _notify_mass_outage_sessions(case)

    return Response(
        {
            "status": "success",
            "event_type": event_type,
            "created": created,
            "merged_count": merged_count,
            "etr_timer_scheduled": etr_timer_scheduled,
            "notified_session_count": len(notified_sessions),
            **_case_response_fields(case, include_model_etr=True),
        }
    )
