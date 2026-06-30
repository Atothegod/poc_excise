from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from .csv_exports import csv_response, write_csv
from .models import CustomerLocation, OutageCase, CustomerReport, OutageRestorationLog


class CsvExportAdminMixin:
    actions = ("export_selected_csv",)
    csv_fields = ()

    def get_csv_fields(self):
        if self.csv_fields:
            return self.csv_fields
        return [field.name for field in self.model._meta.fields]

    @admin.action(description="Export selected rows to CSV")
    def export_selected_csv(self, request, queryset):
        fields = self.get_csv_fields()
        response = csv_response(self.model._meta.model_name)
        rows = ([getattr(obj, field) for field in fields] for obj in queryset)
        return write_csv(response, fields, rows)


@admin.register(OutageCase)
class OutageCaseAdmin(CsvExportAdminMixin, admin.ModelAdmin):
    # นำ countdown_eta และ countdown_etr มาแสดงคู่กันในหน้าตาราง
    list_display = (
        "lv_group_id",
        "case_id",
        "case_type",
        "status",
        "affected_CA",
        "countdown_eta",
        "countdown_etr",
        "countdown_sla",
        "assessment_fastest_branch",
        "assessment_eta_formatted",
        "pluem_etr_minutes",
        "created_at",
    )
    list_filter = ("status", "case_type", "lv_group_id")
    search_fields = ("case_id", "title", "affected_customers__ca_number")
    csv_fields = (
        "case_id",
        "lv_group_id",
        "title",
        "case_type",
        "status",
        "affected_ca_numbers",
        "latitude",
        "longitude",
        "eta_target_time",
        "oms_etr",
        "oms_etr_updated_at",
        "sla_reference_time",
        "sla_target_time",
        "sla_reason",
        "assessment_fastest_branch",
        "assessment_eta_formatted",
        "assessment_eta_minutes",
        "pluem_etr_minutes",
        "pluem_etr_target_time",
        "celery_eta_task_id",
        "celery_etr_task_id",
        "created_at",
        "updated_at",
    )

    def affected_CA(self, obj):
        ca_numbers = obj.affected_ca_numbers or []
        if not ca_numbers:
            return format_html('<span style="color: gray;">ยังไม่มี CA</span>')

        preview = ", ".join(ca_numbers[:5])
        if len(ca_numbers) > 5:
            preview = f"{preview}, ..."
        return format_html("<strong>{}</strong> CA: {}", len(ca_numbers), preview)

    affected_CA.short_description = "affected_CA"

    def countdown_eta(self, obj):
        """
        คำนวณเวลาที่เหลือจาก eta_target_time แบบ Real-time
        แสดงผลเป็นสีต่างๆ ตามความเร่งด่วน
        """
        if not obj.eta_target_time:
            return format_html('<span style="color: gray;">ยังไม่กำหนด ETA</span>')

        now = timezone.now()

        # กรณีเลยเวลาเป้าหมายไปแล้ว
        if now > obj.eta_target_time:
            return format_html(
                '<span style="color: red; font-weight: bold;">เลยกำหนดเวลาแล้ว!</span>'
            )

        # คำนวณเวลาที่เหลือเป็นนาที
        diff = obj.eta_target_time - now
        minutes_left = int(diff.total_seconds() // 60)

        # ถ้าน้อยกว่าหรือเท่ากับ 15 นาที ให้เตือนสีส้ม
        if minutes_left <= 15:
            return format_html(
                '<span style="color: orange; font-weight: bold;">คาดว่าช่างจะถึงในอีก {} นาที</span>',
                minutes_left,
            )

        # เวลาปกติให้แสดงสีเขียว
        return format_html(
            '<span style="color: green;">คาดว่าช่างจะถึงในอีก {} นาที</span>', minutes_left
        )

    countdown_eta.short_description = "ETA Countdown"

    def countdown_etr(self, obj):
        """
        คำนวณเวลาที่เหลือจาก ETR แบบ Real-time (OMS มาก่อน, ถ้าไม่มีใช้โมเดลพี่ปลื้ม)
        แสดงผลเป็นสีต่างๆ ตามความเร่งด่วน
        """
        etr_target_time = obj.effective_etr_time()
        if not etr_target_time:
            return format_html('<span style="color: gray;">ยังไม่ประเมิน ETR</span>')

        now = timezone.now()
        source_label = "OMS" if obj.effective_etr_source() == "oms" else "พี่ปลื้ม"

        # กรณีเลยเวลาเป้าหมายไปแล้ว
        if now > etr_target_time:
            return format_html(
                '<span style="color: red; font-weight: bold;">เลยกำหนดไฟมา! ({})</span>',
                source_label,
            )

        # คำนวณเวลาที่เหลือเป็นนาที
        diff = etr_target_time - now
        minutes_left = int(diff.total_seconds() // 60)

        # ถ้าน้อยกว่าหรือเท่ากับ 15 นาที ให้เตือนสีส้ม
        if minutes_left <= 15:
            return format_html(
                '<span style="color: orange; font-weight: bold;">คาดว่าไฟจะถูกจ่ายคืนในอีก {} นาที ({})</span>',
                minutes_left,
                source_label,
            )

        # เวลาปกติให้แสดงสีเขียว
        return format_html(
            '<span style="color: green;">คาดว่าไฟจะถูกจ่ายคืนในอีก {} นาที ({})</span>',
            minutes_left,
            source_label,
        )

    countdown_etr.short_description = "ETR Countdown"

    def countdown_sla(self, obj):
        if not obj.sla_target_time:
            return format_html('<span style="color: gray;">ยังไม่กำหนด SLA</span>')

        now = timezone.now()
        if now > obj.sla_target_time:
            return format_html(
                '<span style="color: red; font-weight: bold;">เลย SLA แล้ว</span>'
            )

        diff = obj.sla_target_time - now
        minutes_left = int(diff.total_seconds() // 60)
        hours = minutes_left // 60
        minutes = minutes_left % 60
        label = f"{hours} ชม. {minutes} นาที" if hours else f"{minutes} นาที"

        if minutes_left <= 30:
            return format_html(
                '<span style="color: orange; font-weight: bold;">เหลือ {}</span>',
                label,
            )

        return format_html('<span style="color: green;">เหลือ {}</span>', label)

    countdown_sla.short_description = "SLA Countdown"


@admin.register(CustomerLocation)
class CustomerLocationAdmin(CsvExportAdminMixin, admin.ModelAdmin):
    list_display = (
        "ca_number",
        "fullname",
        "phone_number",
        "pea_area",
        "latitude",
        "longitude",
        "eta_result",
        "imported_at",
    )
    search_fields = ("ca_number", "fullname", "phone_number", "address")
    list_filter = ("pea_area", "user_type", "report_channel", "is_ready")
    readonly_fields = ("imported_at",)
    csv_fields = (
        "id",
        "timestamp",
        "prefix",
        "fullname",
        "address",
        "ca_number",
        "phone_number",
        "pea_area",
        "user_type",
        "outage_freq_yearly",
        "report_channel",
        "is_ready",
        "latitude",
        "longitude",
        "eta_result",
        "imported_at",
    )


@admin.register(CustomerReport)
class CustomerReportAdmin(CsvExportAdminMixin, admin.ModelAdmin):
    list_display = (
        "ca_number",
        "customer_name",
        "session_id",
        "pdpa_consent",
        "pdpa_consent_at",
        "related_case",
        "chat_dialog_preview",
        "updated_at",
    )
    search_fields = ("ca_number", "customer_name", "session_id")
    readonly_fields = ("chat_dialog",)
    csv_fields = (
        "id",
        "session_id",
        "ca_number",
        "customer_name",
        "latitude",
        "longitude",
        "related_case_id",
        "chat_history",
        "pdpa_consent",
        "pdpa_consent_at",
        "is_resolved",
        "time_stamp",
        "created_at",
        "updated_at",
    )

    def chat_dialog_preview(self, obj):
        history = obj.chat_history or []
        if not history:
            return format_html('<span style="color: gray;">ยังไม่มีบทสนทนา</span>')
        return f"{len(history)} messages"

    chat_dialog_preview.short_description = "Chat History"

    def chat_dialog(self, obj):
        history = obj.chat_history or []
        if not history:
            return format_html('<span style="color: gray;">ยังไม่มีบทสนทนา</span>')

        return format_html_join(
            "",
            (
                '<div style="margin: 0 0 10px; padding: 10px; border-left: 4px solid {}; background: #f8f9fa;">'
                '<strong>{}</strong>'
                '<span style="color: #666; margin-left: 8px;">{}</span>'
                '<div style="margin-top: 6px; white-space: pre-wrap;">{}</div>'
                "</div>"
            ),
            (
                (
                    "#0d6efd" if item.get("role") == "user" else "#198754",
                    "User" if item.get("role") == "user" else "Agent",
                    item.get("timestamp") or "",
                    item.get("message") or "",
                )
                for item in history
            ),
        )

    chat_dialog.short_description = "Dialog"


@admin.register(OutageRestorationLog)
class OutageRestorationLogAdmin(CsvExportAdminMixin, admin.ModelAdmin):
    list_display = (
        "lv_group_id",
        "case",
        "restored_at",
        "etr_source",
        "effective_etr_at_restore",
        "etr_delta_minutes",
        "affected_CA",
    )
    list_filter = ("etr_source", "lv_group_id")
    search_fields = ("case__case_id",)
    readonly_fields = (
        "case",
        "lv_group_id",
        "affected_ca_numbers",
        "restored_at",
        "eta_target_time_at_restore",
        "oms_etr_at_restore",
        "pluem_etr_target_time_at_restore",
        "effective_etr_at_restore",
        "etr_source",
        "etr_delta_minutes",
        "case_status_at_restore",
        "created_at",
    )
    csv_fields = (
        "id",
        "case_id",
        "lv_group_id",
        "affected_ca_numbers",
        "restored_at",
        "eta_target_time_at_restore",
        "oms_etr_at_restore",
        "pluem_etr_target_time_at_restore",
        "effective_etr_at_restore",
        "etr_source",
        "etr_delta_minutes",
        "case_status_at_restore",
        "created_at",
    )

    def affected_CA(self, obj):
        ca_numbers = obj.affected_ca_numbers or []
        if not ca_numbers:
            return "-"
        preview = ", ".join(ca_numbers[:5])
        if len(ca_numbers) > 5:
            preview = f"{preview}, ..."
        return f"{len(ca_numbers)} CA: {preview}"

    affected_CA.short_description = "affected_CA"
