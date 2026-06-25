from django.db import models
import uuid


class OutageCase(models.Model):
    STATUS_CHOICES = [
        ("reported", "ได้รับแจ้งเหตุ"),
        ("investigating", "กำลังดำเนินการตรวจสอบ"),
        ("repairing", "กำลังดำเนินการซ่อมแซม"),
        ("restored", "จ่ายไฟคืนกระแสสำเร็จ"),
    ]

    case_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, default="ไฟดับบริเวณใกล้เคียง")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="reported")
    latitude = models.FloatField()
    longitude = models.FloatField()

    eta_target_time = models.DateTimeField(
        null=True, blank=True, help_text="เวลาเป้าหมายที่ช่างจะไปถึงหน้างาน (ETA)"
    )
    oms_etr = models.DateTimeField(
        null=True, blank=True, help_text="เวลาซ่อมเสร็จจาก OMS (ISO Format)"
    )

    # --- เพิ่มฟิลด์สำหรับเก็บ Task ID ของ Celery ---
    celery_eta_task_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="ID ของ Celery Task สำหรับนับเวลา ETA",
    )
    celery_etr_task_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="ID ของ Celery Task สำหรับนับเวลา ETR",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Case {self.case_id} - [{self.get_status_display()}]"


class CustomerReport(models.Model):
    session_id = models.CharField(
        max_length=255, null=True, blank=True, help_text="รหัสผู้ใช้งานจากหน้าเว็บ"
    )
    ca_number = models.CharField(
        max_length=12, help_text="หมายเลขผู้ใช้ไฟ (หลาย Session สามารถแจ้ง CA เดียวกันได้)"
    )
    customer_name = models.CharField(max_length=255, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    related_case = models.ForeignKey(
        OutageCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="affected_customers",
    )

    chat_history = models.TextField(
        default="[]", help_text="เก็บประวัติสนทนาล่าสุดของ Session นี้"
    )
    needs_eta = models.BooleanField(default=False, help_text="ต้องการทราบเวลาช่างมาถึง")
    needs_etr = models.BooleanField(default=False, help_text="ต้องการทราบเวลาไฟมา")
    pdpa_consent = models.BooleanField(
        default=False, help_text="ลูกค้าให้ความยินยอมให้ตรวจสอบข้อมูลด้วยหมายเลข CA"
    )
    pdpa_consent_at = models.DateTimeField(
        null=True, blank=True, help_text="เวลาที่ลูกค้าให้ PDPA consent"
    )
    is_resolved = models.BooleanField(
        default=False, help_text="จบการสนทนาหรือไฟมาปกติแล้ว"
    )

    # --- เพิ่มฟิลด์สำหรับระบบ Anti-Loop ---
    fast_track_quota = models.IntegerField(
        default=1, help_text="โควต้าการแจ้งไฟดับซ้ำซ้อน (1 ครั้ง/เคส)"
    )

    time_stamp = models.DateTimeField(
        null=True, blank=True, help_text="เวลาอ้างอิงที่ส่งมาจากระบบ Agent (ISO Format)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Report {self.ca_number} (Session: {self.session_id[:8]}...)"
