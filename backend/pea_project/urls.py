from django.contrib import admin
from django.urls import path, include
from oms import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("chat/", views.chat_page, name="chat"),

    path("ops/webhook/", views.ops_webhook_page, name="ops_webhook"),
    path("ops/cases/", views.ops_cases_api, name="ops_cases_api"),
    path("ops/cases/export/", views.ops_cases_export_csv, name="ops_cases_export_csv"),
    path("ops/cases/action/", views.ops_cases_action_api, name="ops_cases_action_api"),
    
    path("agent/ask/", views.agent_ask_proxy, name="agent_ask_proxy"),
    path(
        "agent/notifications/<str:session_id>/",
        views.agent_notifications_proxy,
        name="agent_notifications_proxy",
    ),
    path("admin/", admin.site.urls),
    path("api/", include("oms.router")),
]
