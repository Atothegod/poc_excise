from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
import requests


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
