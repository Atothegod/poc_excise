from django.contrib import admin

# Register your models here.

from django.contrib import admin
from .models import OutageCase, CustomerReport

@admin.register(OutageCase)
class OutageCaseAdmin(admin.ModelAdmin):
    list_display = ('case_id', 'title', 'status', 'oms_etr', 'created_at')
    list_filter = ('status',)

@admin.register(CustomerReport)
class CustomerReportAdmin(admin.ModelAdmin):
    list_display = ('ca_number', 'customer_name', 'related_case', 'updated_at')
    search_fields = ('ca_number', 'customer_name')