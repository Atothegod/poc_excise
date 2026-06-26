from django.contrib import admin
from django.urls import path, include
from oms import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("chat/", views.chat_page, name="chat"),
    path("agent/ask/", views.agent_ask_proxy, name="agent_ask_proxy"),
    path(
        "agent/notifications/<str:session_id>/",
        views.agent_notifications_proxy,
        name="agent_notifications_proxy",
    ),
    path("admin/", admin.site.urls),
    path("api/", include("oms.router")),
]
