from django.conf import settings
from django.shortcuts import render


def chat_page(request):
    return render(
        request,
        "oms/chat.html",
        {"agent_base_url": settings.DSPY_AGENT_PUBLIC_URL},
    )
