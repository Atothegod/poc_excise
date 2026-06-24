from django.db import models
import uuid


class OutageCase(models.Model):
    """เก็บข้อมูลเหตุการณ์ไฟดับหลัก (1 เคสใหญ่ สามารถมีผู้ได้รับผลกระทบหลาย CA)"""

    STATUS_CHOICES = [
        ("reported", "ได้รับแจ้งเหตุ"),
        ("investigating", "กำลังตรวจสอบพิกัด"),
        ("repairing", "กำลังดำเนินการซ่อมแซม"),
        ("restored", "จ่ายไฟคืนกระแสสำเร็จ"),
    ]

    case_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, default="ไฟดับบริเวณใกล้เคียง")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="reported")
    latitude = models.FloatField()
    longitude = models.FloatField()
    oms_etr = models.DateTimeField(
        null=True, blank=True, help_text="เวลาซ่อมเสร็จจาก OMS"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Case {self.case_id} - [{self.get_status_display()}]"


class CustomerReport(models.Model):
    """เก็บข้อมูลการแจ้งเรื่องของลูกค้าแต่ละรายตามหมายเลข CA และ Session"""

    # --- Identifiers ---
    # ให้ Django จัดการ ID อัตโนมัติเป็น Primary Key
    session_id = models.CharField(
        max_length=255, null=True, blank=True, help_text="รหัสผู้ใช้งานจากหน้าเว็บ"
    )
    ca_number = models.CharField(
        max_length=12, help_text="หมายเลขผู้ใช้ไฟ (หลาย Session สามารถแจ้ง CA เดียวกันได้)"
    )
    customer_name = models.CharField(max_length=255, null=True, blank=True)

    # --- Location & Relation ---
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    related_case = models.ForeignKey(
        OutageCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="affected_customers",
    )

    # --- Chat & Intent ---
    chat_history = models.TextField(
        default="[]", help_text="เก็บประวัติสนทนาล่าสุดของ Session นี้"
    )
    needs_eta = models.BooleanField(default=False, help_text="ต้องการทราบเวลาช่างมาถึง")
    needs_etr = models.BooleanField(default=False, help_text="ต้องการทราบเวลาไฟมา")

    # --- Status ---
    is_resolved = models.BooleanField(
        default=False, help_text="จบการสนทนาหรือไฟมาปกติแล้ว"
    )

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)  # เพิ่มไว้ดูว่าเริ่มแชทตอนไหน
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Report {self.ca_number} (Session: {self.session_id[:8]}...)"
