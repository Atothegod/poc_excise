from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from .models import CustomerLocation, OutageCase, CustomerReport


@admin.register(OutageCase)
class OutageCaseAdmin(admin.ModelAdmin):
    # นำ countdown_eta และ countdown_etr มาแสดงคู่กันในหน้าตาราง
    list_display = (
        "case_id",
        "title",
        "status",
        "countdown_eta",
        "countdown_etr",
        "assessment_fastest_branch",
        "assessment_eta_formatted",
        "pluem_etr_minutes",
        "created_at",
    )
    list_filter = ("status",)

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


@admin.register(CustomerLocation)
class CustomerLocationAdmin(admin.ModelAdmin):
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


@admin.register(CustomerReport)
class CustomerReportAdmin(admin.ModelAdmin):
    list_display = (
        "ca_number",
        "customer_name",
        "pdpa_consent",
        "pdpa_consent_at",
        "related_case",
        "updated_at",
    )
    search_fields = ("ca_number", "customer_name")
