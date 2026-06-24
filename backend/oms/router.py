from django.urls import path
from . import views_api

urlpatterns = [
    path("reports/sync/", views_api.sync_agent_report, name="sync_report"),
    path("reports/status/", views_api.get_action_status, name="get_status"),  
]

