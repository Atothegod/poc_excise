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
    STATUS_RESTORED,
)
from .models import CustomerReport, OutageCase
from .oms_client import OmsClientError, get_customer, report_outage
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


def _case_id_from_oms(oms_case):
    raw_case_id = oms_case.get("case_id")
    if not raw_case_id:
        raise ValueError("OMS response does not contain case_id")
    return UUID(str(raw_case_id))


def _mirror_oms_case(oms_case, trigger_signals=False):
    case_id = _case_id_from_oms(oms_case)
    status = oms_case.get("status") or "reported"
    case_type = oms_case.get("case_type") or CASE_TYPE_NORMAL
    affected_ca_numbers = sorted(set(oms_case.get("affected_ca_numbers") or []))
    oms_etr = _parse_oms_datetime(
        oms_case.get("oms_etr") or oms_case.get("etr_target_time")
    )

    defaults = {
        "title": f"OMS case {case_id}",
        "case_type": case_type,
        "status": status,
        "affected_ca_numbers": affected_ca_numbers,
        "oms_etr": oms_etr,
        "sla_reference_time": timezone.now(),
        "sla_target_time": timezone.now() + timedelta(hours=OutageCase.SLA_HOURS),
        "sla_reason": "case_created",
    }

    case, created = OutageCase.objects.get_or_create(case_id=case_id, defaults=defaults)
    if created:
        return case

    fields = {
        "case_type": case_type,
        "status": status,
        "affected_ca_numbers": affected_ca_numbers,
        "oms_etr": oms_etr,
    }
    if trigger_signals:
        for field, value in fields.items():
            setattr(case, field, value)
        case.save(update_fields=[*fields.keys(), "updated_at"])
    else:
        OutageCase.objects.filter(pk=case.pk).update(**fields, updated_at=timezone.now())
        case.refresh_from_db()
    return case


def _apply_assessment_to_case(case, ca_number, base_time, report):
    if case.eta_target_time:
        return None

    assessment = get_pea_assessment({"ca_number": ca_number}) or {}
    assessment_error = assessment.get("error")
    eta_minutes = None if assessment_error else parse_eta_minutes(
        assessment.get("eta_formatted")
    )

    if eta_minutes is None:
        return assessment_error or "assessment response does not contain a usable ETA"

    etr_minutes = _parse_float(assessment.get("estimated_etr_minutes"))
    eta_target_time = base_time + timedelta(minutes=eta_minutes)
    pluem_etr_target_time = (
        base_time + timedelta(minutes=etr_minutes)
        if etr_minutes is not None
        else None
    )

    case.eta_target_time = eta_target_time
    case.sla_reference_time = base_time
    case.sla_target_time = base_time + timedelta(hours=OutageCase.SLA_HOURS)
    case.sla_reason = "case_created"
    case.assessment_fastest_branch = assessment.get("fastest_branch") or ""
    case.assessment_eta_formatted = (
        assessment.get("eta_formatted")
        or format_minutes_label(eta_minutes)
        or ""
    )
    case.assessment_eta_minutes = eta_minutes
    case.pluem_etr_minutes = etr_minutes
    case.pluem_etr_target_time = pluem_etr_target_time
    case.assessment_payload = assessment
    case.save(
        update_fields=[
            "eta_target_time",
            "sla_reference_time",
            "sla_target_time",
            "sla_reason",
            "assessment_fastest_branch",
            "assessment_eta_formatted",
            "assessment_eta_minutes",
            "pluem_etr_minutes",
            "pluem_etr_target_time",
            "assessment_payload",
            "updated_at",
        ]
    )

    task = check_eta_timeout.apply_async(args=[case.case_id, report.id], eta=eta_target_time)
    case.celery_eta_task_id = task.id
    case.save(update_fields=["celery_eta_task_id", "updated_at"])
    return None


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

    report, _created = _get_or_create_active_report(session_id, ca_number)
    if data.get("time_stamp"):
        report.time_stamp = data.get("time_stamp")
    if data.get("pdpa_consent"):
        _grant_pdpa_consent(report)
    report.save()

    try:
        oms_response = report_outage(ca_number)
    except OmsClientError:
        return Response({"status": "error", "event_type": "api_error"})

    if not oms_response:
        return Response(
            {
                "status": "not_found",
                "event_type": "ca_not_found",
                "message": "ไม่พบ CA ในฐานข้อมูลพิกัดลูกค้า OMS",
                "report_id": report.id,
                **_case_response_fields(None),
            }
        )

    event_type = oms_response.get("event_type") or "new_event"
    oms_case = oms_response.get("case") or oms_response
    try:
        case = _mirror_oms_case(oms_case, trigger_signals=False)
    except ValueError as exc:
        return Response({"status": "error", "event_type": "api_error", "message": str(exc)})

    report.related_case = case
    report.save(update_fields=["related_case", "updated_at"])
    _attach_waiting_same_ca_reports(report)
    _attach_reports_for_case(case)
    case.sync_affected_ca_numbers()

    if event_type == "new_event":
        base_time = report.time_stamp if report.time_stamp else timezone.now()
        assessment_error = _apply_assessment_to_case(case, ca_number, base_time, report)
        if assessment_error:
            return Response(
                {
                    "status": "assessment_error",
                    "event_type": "assessment_error",
                    "message": assessment_error,
                    "report_id": report.id,
                    **_case_response_fields(case),
                }
            )

    if oms_response.get("newly_promoted"):
        case.refresh_from_db()
        _notify_mass_outage_sessions(case, exclude_session_id=session_id)

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
    try:
        customer = get_customer(ca_number)
    except OmsClientError:
        return Response({"status": "error", "message": "ไม่สามารถเชื่อมต่อ OMS"}, status=502)

    if not customer:
        return Response(
            {
                "status": "not_found",
                "message": "ไม่พบหมายเลข CA นี้ในฐานข้อมูลลูกค้า OMS",
            },
            status=404,
        )

    return Response(
        {
            "status": "success",
            "ca_number": ca_number,
            "customer_name": customer.get("fullname") or "",
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
    try:
        customer = get_customer(data["ca_number"])
    except OmsClientError:
        return Response({"status": "error", "message": "ไม่สามารถเชื่อมต่อ OMS"}, status=502)

    if not customer:
        return Response(
            {
                "status": "not_found",
                "message": "ไม่พบหมายเลข CA นี้ในฐานข้อมูลลูกค้า OMS",
            },
            status=404,
        )

    report, _created = _get_or_create_active_report(
        data["session_id"], data["ca_number"]
    )
    report.customer_name = customer.get("fullname") or ""
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
        case = _mirror_oms_case(
            oms_case,
            trigger_signals=event_type in {"etr_updated", "case_closed"},
        )
    except ValueError as exc:
        return Response({"status": "error", "message": str(exc)}, status=400)

    _attach_reports_for_case(case)
    case.sync_affected_ca_numbers()
    return Response(
        {
            "status": "success",
            "event_type": event_type,
            **_case_response_fields(case, include_model_etr=True),
        }
    )
