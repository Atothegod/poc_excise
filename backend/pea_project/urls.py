from django.contrib import admin
from django.urls import path, include
from oms import views

urlpatterns = [
    path("", views.login_page, name="login"),
    path("chat/", views.chat_page, name="chat"),

    path("ops/webhook/", views.ops_webhook_page, name="ops_webhook"),
    path("ops/map/", views.ops_map_page, name="ops_map"),
    path("ops/map/data/", views.ops_map_data_api, name="ops_map_data_api"),
    path("ops/cases/", views.ops_cases_api, name="ops_cases_api"),
    path("ops/cases/export/", views.ops_cases_export_csv, name="ops_cases_export_csv"),
    path("ops/cases/action/", views.ops_cases_action_api, name="ops_cases_action_api"),
    
    path("agent/ask/", views.agent_ask_proxy, name="agent_ask_proxy"),
    path(
        "agent/notifications/<str:session_id>/",
        views.agent_notifications_proxy,
        name="agent_notifications_proxy",
    ),
    path(
        "agent/notifications/<str:session_id>/ack/",
        views.agent_notifications_ack_proxy,
        name="agent_notifications_ack_proxy",
    ),
    path(
        "agent/notifications/<str:session_id>/latest-closed-loop/",
        views.agent_latest_closed_loop_proxy,
        name="agent_latest_closed_loop_proxy",
    ),
    path("admin/", admin.site.urls),
    path("api/", include("oms.router")),
]
