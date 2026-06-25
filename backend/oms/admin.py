from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from .models import OutageCase, CustomerReport


@admin.register(OutageCase)
class OutageCaseAdmin(admin.ModelAdmin):
    # นำ countdown_eta และ countdown_etr มาแสดงคู่กันในหน้าตาราง
    list_display = (
        "case_id",
        "title",
        "status",
        "countdown_eta",
        "countdown_etr",
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
        คำนวณเวลาที่เหลือจาก oms_etr แบบ Real-time (เวลาไฟมา)
        แสดงผลเป็นสีต่างๆ ตามความเร่งด่วน
        """
        if not obj.oms_etr:
            return format_html('<span style="color: gray;">ยังไม่ประเมิน ETR</span>')

        now = timezone.now()

        # กรณีเลยเวลาเป้าหมายไปแล้ว
        if now > obj.oms_etr:
            return format_html(
                '<span style="color: red; font-weight: bold;">เลยกำหนดไฟมา!</span>'
            )

        # คำนวณเวลาที่เหลือเป็นนาที
        diff = obj.oms_etr - now
        minutes_left = int(diff.total_seconds() // 60)

        # ถ้าน้อยกว่าหรือเท่ากับ 15 นาที ให้เตือนสีส้ม
        if minutes_left <= 15:
            return format_html(
                '<span style="color: orange; font-weight: bold;">คาดว่าไฟจะถูกจ่ายคืนในอีก {} นาที</span>',
                minutes_left,
            )

        # เวลาปกติให้แสดงสีเขียว
        return format_html(
            '<span style="color: green;">คาดว่าไฟจะถูกจ่ายคืนในอีก {} นาที</span>', minutes_left
        )

    countdown_etr.short_description = "ETR Countdown"


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
