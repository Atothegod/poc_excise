from django.contrib import admin
from django.urls import path, include
from oms import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("chat/", views.chat_page, name="chat"),
    path("admin/", admin.site.urls),
    path("api/", include("oms.router")),
]
