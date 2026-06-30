from django.urls import path
from . import views_api

urlpatterns = [
    path("reports/sync/", views_api.sync_agent_report, name="sync_report"),
    path("reports/validate-ca/", views_api.validate_ca_login, name="validate_ca_login"),
    path(
        "reports/session-login/",
        views_api.register_session_login,
        name="session_login",
    ),
    path("reports/chat-history/", views_api.sync_chat_history, name="sync_chat_history"),
    path(
        "reports/session-context/<str:session_id>/",
        views_api.get_session_context,
        name="session_context",
    ),
    path("reports/status/", views_api.get_action_status, name="get_status"),
    path("reports/fast-track/", views_api.fast_track_report, name="fast_track"),
]
