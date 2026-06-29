import json
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
import requests

from .csv_exports import csv_response, write_csv
from .models import OutageCase


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


def _isoformat(value):
    return value.isoformat() if value else None


def _case_payload(case):
    return {
        "case_id": str(case.case_id),
        "lv_group_id": case.lv_group_id,
        "title": case.title,
        "status": case.status,
        "status_display": case.get_status_display(),
        "latitude": case.latitude,
        "longitude": case.longitude,
        "affected_ca_numbers": case.affected_ca_numbers or [],
        "eta_target_time": _isoformat(case.eta_target_time),
        "oms_etr": _isoformat(case.oms_etr),
        "effective_etr_time": _isoformat(case.effective_etr_time()),
        "effective_etr_source": case.effective_etr_source(),
        "created_at": _isoformat(case.created_at),
        "updated_at": _isoformat(case.updated_at),
    }


OPS_CASE_CSV_COLUMNS = (
    ("lv_group_id", "LV"),
    ("case_id", "Case ID"),
    ("title", "Title"),
    ("status", "Status"),
    ("status_display", "Status Label"),
    ("affected_ca_numbers", "Affected CA"),
    ("latitude", "Latitude"),
    ("longitude", "Longitude"),
    ("eta_target_time", "ETA"),
    ("oms_etr", "OMS ETR"),
    ("effective_etr_time", "Effective ETR"),
    ("effective_etr_source", "ETR Source"),
    ("created_at", "Created"),
    ("updated_at", "Updated"),
)


def _case_queryset_for_request(request):
    include_restored = request.GET.get("include_restored") == "true"
    status = (request.GET.get("status") or "").strip()
    cases = OutageCase.objects.all().order_by("-created_at")
    if not include_restored:
        cases = cases.exclude(status="restored")

    if status == "active":
        cases = cases.exclude(status="restored")
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
        return OutageCase.objects.exclude(status="restored").order_by("lv_group_id")

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
            if case.status == "restored":
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
            if case.status == "restored":
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
            if case.status == "restored":
                skipped_cases.append(_action_case_payload(case))
                continue
            case.status = "restored"
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
