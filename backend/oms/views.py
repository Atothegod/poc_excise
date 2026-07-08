import json
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
import requests

from .case_logic import (
    CASE_LINK_RADIUS_KM,
    CASE_TYPE_MASS_OUTAGE,
    INACTIVE_CASE_STATUSES,
    STATUS_RESTORED,
)
from .csv_exports import csv_response, write_csv
from .models import CustomerLocation, CustomerReport, OutageCase


def login_page(request):
    return render(request, "oms/login.html")


def chat_page(request):
    return render(
        request,
        "oms/chat.html",
        {"agent_base_url": settings.DSPY_AGENT_PUBLIC_URL},
    )


def _agent_url(path):
    return f"{settings.DSPY_AGENT_INTERNAL_URL.rstrip('/')}/{path.lstrip('/')}"


def _proxy_agent_response(response):
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text or "Agent returned an invalid response."}
    return JsonResponse(payload, status=response.status_code, safe=False)


@csrf_exempt
@require_POST
def agent_ask_proxy(request):
    try:
        response = requests.post(
            _agent_url("/ask"),
            data=request.body,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            timeout=60,
        )
        return _proxy_agent_response(response)
    except requests.exceptions.RequestException as exc:
        return JsonResponse(
            {"detail": f"Cannot connect to DSPy agent: {exc}"},
            status=502,
        )


def ops_webhook_page(request):
    return render(
        request,
        "oms/ops_webhook.html",
        {
            "cases_url": reverse("ops_cases_api"),
            "action_url": reverse("ops_cases_action_api"),
            "export_url": reverse("ops_cases_export_csv"),
        },
    )


def ops_map_page(request):
    return render(
        request,
        "oms/ops_map.html",
        {
            "map_data_url": reverse("ops_map_data_api"),
            "ops_webhook_url": reverse("ops_webhook"),
            "case_link_radius_km": CASE_LINK_RADIUS_KM,
        },
    )


def _isoformat(value):
    return value.isoformat() if value else None


def _case_payload(case):
    return {
        "case_id": str(case.case_id),
        "lv_group_id": case.lv_group_id,
        "title": case.title,
        "case_type": case.case_type,
        "status": case.status,
        "status_display": case.get_status_display(),
        "latitude": case.latitude,
        "longitude": case.longitude,
        "affected_ca_numbers": case.affected_ca_numbers or [],
        "eta_target_time": _isoformat(case.eta_target_time),
        "oms_etr": _isoformat(case.oms_etr),
        "oms_etr_updated_at": _isoformat(case.oms_etr_updated_at),
        "effective_etr_time": _isoformat(case.effective_etr_time()),
        "effective_etr_source": case.effective_etr_source(),
        "sla_reference_time": _isoformat(case.sla_reference_time),
        "sla_target_time": _isoformat(case.sla_target_time),
        "sla_reason": case.sla_reason,
        "created_at": _isoformat(case.created_at),
        "updated_at": _isoformat(case.updated_at),
    }


OPS_CASE_CSV_COLUMNS = (
    ("lv_group_id", "LV"),
    ("case_id", "Case ID"),
    ("title", "Title"),
    ("case_type", "Case Type"),
    ("status", "Status"),
    ("status_display", "Status Label"),
    ("affected_ca_numbers", "Affected CA"),
    ("latitude", "Latitude"),
    ("longitude", "Longitude"),
    ("eta_target_time", "ETA"),
    ("oms_etr", "OMS ETR"),
    ("oms_etr_updated_at", "OMS ETR Updated"),
    ("effective_etr_time", "Effective ETR"),
    ("effective_etr_source", "ETR Source"),
    ("sla_reference_time", "SLA Reference"),
    ("sla_target_time", "SLA Target"),
    ("sla_reason", "SLA Reason"),
    ("created_at", "Created"),
    ("updated_at", "Updated"),
)


def _case_queryset_for_request(request):
    include_restored = request.GET.get("include_restored") == "true"
    status = (request.GET.get("status") or "").strip()
    cases = OutageCase.objects.all().order_by("-created_at")
    if not include_restored:
        cases = cases.exclude(status__in=INACTIVE_CASE_STATUSES)

    if status == "active":
        cases = cases.exclude(status__in=INACTIVE_CASE_STATUSES)
    elif status and status != "all":
        valid_statuses = {choice[0] for choice in OutageCase.STATUS_CHOICES}
        if status in valid_statuses:
            cases = cases.filter(status=status)
    return cases


def _case_matches_search(case, search_term):
    if not search_term:
        return True
    haystack = [
        case.lv_group_id,
        case.case_id,
        case.title,
        case.case_type,
        case.get_status_display(),
        *(case.affected_ca_numbers or []),
    ]
    return search_term in " ".join(str(value) for value in haystack).lower()


def _filtered_cases_for_request(request, limit=None):
    search_term = (request.GET.get("search") or "").strip().lower()
    cases = _case_queryset_for_request(request)
    if search_term:
        matched_cases = [
            case for case in cases if _case_matches_search(case, search_term)
        ]
        return matched_cases[:limit] if limit is not None else matched_cases
    return cases[:limit] if limit is not None else cases


MAP_STATUS_META = {
    "reported": {"label": "ได้รับแจ้งเหตุ", "color": "#dc2626"},
    "investigating": {"label": "กำลังตรวจสอบ", "color": "#f59e0b"},
    "repairing": {"label": "กำลังซ่อมแซม", "color": "#2563eb"},
    "restored": {"label": "จ่ายไฟคืนแล้ว", "color": "#16a34a"},
    "merged": {"label": "ถูกรวมเคส", "color": "#64748b"},
    "no_case": {"label": "ไม่มีเคส", "color": "#94a3b8"},
}


def _bool_param(request, name, default=False):
    value = request.GET.get(name)
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def _map_case_queryset_for_request(request):
    status = (request.GET.get("status") or "active").strip()
    case_type = (request.GET.get("case_type") or "all").strip()
    created_from = parse_date((request.GET.get("created_from") or "").strip())
    created_to = parse_date((request.GET.get("created_to") or "").strip())

    cases = OutageCase.objects.all().order_by("-created_at")

    if status == "active":
        cases = cases.exclude(status__in=INACTIVE_CASE_STATUSES)
    elif status and status != "all":
        valid_statuses = {choice[0] for choice in OutageCase.STATUS_CHOICES}
        if status in valid_statuses:
            cases = cases.filter(status=status)

    if case_type and case_type != "all":
        valid_case_types = {choice[0] for choice in OutageCase.CASE_TYPE_CHOICES}
        if case_type in valid_case_types:
            cases = cases.filter(case_type=case_type)

    if created_from:
        cases = cases.filter(created_at__date__gte=created_from)
    if created_to:
        cases = cases.filter(created_at__date__lte=created_to)

    return list(cases)


def _case_ca_numbers(case, report_ca_numbers_by_case):
    ca_numbers = set(case.affected_ca_numbers or [])
    ca_numbers.update(report_ca_numbers_by_case.get(case.case_id, set()))
    return sorted(ca for ca in ca_numbers if ca)


def _report_sort_key(report):
    updated_at = report.updated_at or timezone.now()
    return (
        report.related_case.status in INACTIVE_CASE_STATUSES,
        -updated_at.timestamp(),
    )


def _case_context_by_ca(cases):
    if not cases:
        return {}, {}

    reports = list(
        CustomerReport.objects.select_related("related_case")
        .filter(related_case__in=cases)
        .exclude(ca_number="")
    )
    reports.sort(key=_report_sort_key)

    report_ca_numbers_by_case = {}
    case_by_ca = {}
    for report in reports:
        report_ca_numbers_by_case.setdefault(report.related_case_id, set()).add(
            report.ca_number
        )
        case_by_ca.setdefault(report.ca_number, report.related_case)

    for case in cases:
        for ca_number in _case_ca_numbers(case, report_ca_numbers_by_case):
            case_by_ca.setdefault(ca_number, case)

    return case_by_ca, report_ca_numbers_by_case


def _map_case_matches_search(case, search_term, case_ca_numbers):
    if not search_term:
        return True

    haystack = [
        case.lv_group_id,
        case.case_id,
        case.title,
        case.case_type,
        case.get_case_type_display(),
        case.status,
        case.get_status_display(),
        *case_ca_numbers,
    ]
    return search_term in " ".join(str(value) for value in haystack).lower()


def _map_search_ca_numbers(search_term, cases, report_ca_numbers_by_case):
    if not search_term:
        return None

    customer_matches = CustomerLocation.objects.filter(
        Q(ca_number__icontains=search_term)
        | Q(fullname__icontains=search_term)
        | Q(pea_area__icontains=search_term)
        | Q(address__icontains=search_term)
    ).values_list("ca_number", flat=True)
    ca_numbers = set(customer_matches)

    for case in cases:
        case_ca_numbers = _case_ca_numbers(case, report_ca_numbers_by_case)
        if _map_case_matches_search(case, search_term, case_ca_numbers):
            ca_numbers.update(case_ca_numbers)

    return ca_numbers


def _map_case_payload(case):
    if not case:
        return None

    return {
        "case_id": str(case.case_id),
        "lv_group_id": case.lv_group_id,
        "title": case.title,
        "case_type": case.case_type,
        "case_type_display": case.get_case_type_display(),
        "status": case.status,
        "status_display": case.get_status_display(),
        "affected_count": len(case.affected_ca_numbers or []),
        "eta_target_time": _isoformat(case.eta_target_time),
        "effective_etr_time": _isoformat(case.effective_etr_time()),
        "sla_target_time": _isoformat(case.sla_target_time),
        "created_at": _isoformat(case.created_at),
        "updated_at": _isoformat(case.updated_at),
    }


def _map_marker_payload(location, case):
    marker_status = case.status if case else "no_case"
    status_meta = MAP_STATUS_META.get(marker_status, MAP_STATUS_META["no_case"])

    return {
        "ca_number": location.ca_number,
        "customer_name": location.fullname,
        "address": location.address,
        "pea_area": location.pea_area,
        "user_type": location.user_type,
        "outage_freq_yearly": location.outage_freq_yearly,
        "is_ready": location.is_ready,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "marker": {
            "status": marker_status,
            "label": status_meta["label"],
            "color": status_meta["color"],
            "is_mass_outage": bool(case and case.case_type == CASE_TYPE_MASS_OUTAGE),
        },
        "case": _map_case_payload(case),
    }


def _map_summary(markers):
    case_ids = {
        marker["case"]["case_id"]
        for marker in markers
        if marker.get("case")
    }
    active_case_ids = {
        marker["case"]["case_id"]
        for marker in markers
        if marker.get("case")
        and marker["case"]["status"] not in INACTIVE_CASE_STATUSES
    }
    restored_case_ids = {
        marker["case"]["case_id"]
        for marker in markers
        if marker.get("case") and marker["case"]["status"] == STATUS_RESTORED
    }

    return {
        "total_locations": len(markers),
        "case_locations": sum(1 for marker in markers if marker.get("case")),
        "no_case_locations": sum(1 for marker in markers if not marker.get("case")),
        "cases": len(case_ids),
        "active_cases": len(active_case_ids),
        "restored_cases": len(restored_case_ids),
    }


@require_GET
def ops_map_data_api(request):
    search_term = (request.GET.get("search") or "").strip().lower()
    show_all = _bool_param(request, "show_all", default=False)
    cases = _map_case_queryset_for_request(request)
    case_by_ca, report_ca_numbers_by_case = _case_context_by_ca(cases)
    case_ca_numbers = set(case_by_ca.keys())

    locations = CustomerLocation.objects.exclude(latitude__isnull=True).exclude(
        longitude__isnull=True
    )

    if not show_all:
        locations = locations.filter(ca_number__in=case_ca_numbers)

    search_ca_numbers = _map_search_ca_numbers(
        search_term, cases, report_ca_numbers_by_case
    )
    if search_ca_numbers is not None:
        locations = locations.filter(ca_number__in=search_ca_numbers)

    markers = [
        _map_marker_payload(location, case_by_ca.get(location.ca_number))
        for location in locations.order_by("ca_number")[:5000]
    ]

    return JsonResponse(
        {
            "markers": markers,
            "summary": _map_summary(markers),
            "legend": MAP_STATUS_META,
            "case_link_radius_km": CASE_LINK_RADIUS_KM,
            "filters": {
                "search": search_term,
                "status": request.GET.get("status") or "active",
                "case_type": request.GET.get("case_type") or "all",
                "created_from": request.GET.get("created_from") or "",
                "created_to": request.GET.get("created_to") or "",
                "show_all": show_all,
            },
        }
    )


@require_GET
def ops_cases_api(request):
    cases = _filtered_cases_for_request(request, limit=500)

    return JsonResponse({"cases": [_case_payload(case) for case in cases]})


@require_GET
def ops_cases_export_csv(request):
    response = csv_response("ops-cases")
    headers = [label for _, label in OPS_CASE_CSV_COLUMNS]
    rows = (
        [_case_payload(case)[key] for key, _ in OPS_CASE_CSV_COLUMNS]
        for case in _filtered_cases_for_request(request)
    )
    return write_csv(response, headers, rows)


def _load_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON body") from exc


def _normalize_case_ids(raw_case_ids):
    if not isinstance(raw_case_ids, list):
        raise ValueError("case_ids must be a list")

    case_ids = []
    for raw_case_id in raw_case_ids:
        try:
            case_ids.append(str(UUID(str(raw_case_id))))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid case_id: {raw_case_id}") from exc
    return case_ids


def _target_cases(data):
    target_mode = data.get("target_mode") or "selected"
    if target_mode == "all_active":
        return OutageCase.objects.exclude(status__in=INACTIVE_CASE_STATUSES).order_by(
            "lv_group_id"
        )

    case_ids = _normalize_case_ids(data.get("case_ids") or [])
    if not case_ids:
        raise ValueError("No cases selected")
    return OutageCase.objects.filter(case_id__in=case_ids).order_by("lv_group_id")


def _parse_etr_target(data):
    etr_mode = data.get("etr_mode") or "minutes"
    if etr_mode == "datetime":
        etr_at = parse_datetime(str(data.get("etr_at") or ""))
        if not etr_at:
            raise ValueError("Invalid ETR datetime")
        if timezone.is_naive(etr_at):
            etr_at = timezone.make_aware(etr_at, timezone.get_current_timezone())
        return etr_at

    try:
        etr_minutes = float(data.get("etr_minutes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid ETR minutes") from exc

    if etr_minutes <= 0:
        raise ValueError("ETR minutes must be greater than 0")
    return timezone.now() + timedelta(minutes=etr_minutes)


def _action_case_payload(case):
    return {
        "case_id": str(case.case_id),
        "lv_group_id": case.lv_group_id,
        "status": case.status,
        "status_display": case.get_status_display(),
    }


@require_POST
def ops_cases_action_api(request):
    try:
        data = _load_json_body(request)
        action = data.get("action")
        cases = list(_target_cases(data))
        if not cases:
            raise ValueError("No matching cases")
    except ValueError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)

    updated_cases = []
    skipped_cases = []

    if action == "set_etr":
        try:
            etr_target = _parse_etr_target(data)
        except ValueError as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)

        for case in cases:
            if case.status in INACTIVE_CASE_STATUSES:
                skipped_cases.append(_action_case_payload(case))
                continue
            case.oms_etr = etr_target
            case.save(update_fields=["oms_etr", "updated_at"])
            updated_cases.append(_action_case_payload(case))

        return JsonResponse(
            {
                "status": "success",
                "action": action,
                "etr_target_time": _isoformat(etr_target),
                "updated_count": len(updated_cases),
                "skipped_count": len(skipped_cases),
                "updated_cases": updated_cases,
                "skipped_cases": skipped_cases,
            }
        )

    if action == "set_eta_now":
        eta_target = timezone.now()

        for case in cases:
            if case.status in INACTIVE_CASE_STATUSES:
                skipped_cases.append(_action_case_payload(case))
                continue
            case.eta_target_time = eta_target
            case.save(update_fields=["eta_target_time", "updated_at"])
            updated_cases.append(_action_case_payload(case))

        return JsonResponse(
            {
                "status": "success",
                "action": action,
                "eta_target_time": _isoformat(eta_target),
                "updated_count": len(updated_cases),
                "skipped_count": len(skipped_cases),
                "updated_cases": updated_cases,
                "skipped_cases": skipped_cases,
            }
        )

    if action == "restore":
        for case in cases:
            if case.status in INACTIVE_CASE_STATUSES:
                skipped_cases.append(_action_case_payload(case))
                continue
            case.status = STATUS_RESTORED
            case.save(update_fields=["status", "updated_at"])
            updated_cases.append(_action_case_payload(case))

        return JsonResponse(
            {
                "status": "success",
                "action": action,
                "updated_count": len(updated_cases),
                "skipped_count": len(skipped_cases),
                "updated_cases": updated_cases,
                "skipped_cases": skipped_cases,
            }
        )

    return JsonResponse(
        {"status": "error", "message": "Unsupported action"},
        status=400,
    )


@require_GET
def agent_notifications_proxy(request, session_id):
    try:
        response = requests.get(
            _agent_url(f"/notifications/{session_id}"),
            timeout=10,
        )
        return _proxy_agent_response(response)
    except requests.exceptions.RequestException as exc:
        return JsonResponse(
            {"detail": f"Cannot connect to DSPy agent: {exc}"},
            status=502,
        )
