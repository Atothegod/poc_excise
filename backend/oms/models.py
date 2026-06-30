from django.db import models
from django.utils import timezone
from datetime import timedelta
import uuid


class OutageCase(models.Model):
    SLA_HOURS = 4

    STATUS_CHOICES = [
        ("reported", "ได้รับแจ้งเหตุ"),
        ("investigating", "กำลังดำเนินการตรวจสอบ"),
        ("repairing", "กำลังดำเนินการซ่อมแซม"),
        ("restored", "จ่ายไฟคืนกระแสสำเร็จ"),
    ]
    CASE_TYPE_CHOICES = [
        ("normal", "เคสปกติ"),
        ("fast_track", "เคสเร่งด่วน"),
    ]

    case_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lv_group_id = models.PositiveIntegerField(
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="เลขกลุ่มเคสแบบรัน 1-n สำหรับ filter/readability",
    )
    title = models.CharField(max_length=255, default="ไฟดับบริเวณใกล้เคียง")
    case_type = models.CharField(
        max_length=20,
        choices=CASE_TYPE_CHOICES,
        default="normal",
        db_index=True,
        help_text="ประเภทเคส เช่น normal หรือ fast_track",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="reported")
    affected_ca_numbers = models.JSONField(
        default=list,
        blank=True,
        help_text="Snapshot รายการ CA ที่ผูกกับเคสนี้",
    )
    latitude = models.FloatField()
    longitude = models.FloatField()

    eta_target_time = models.DateTimeField(
        null=True, blank=True, help_text="เวลาเป้าหมายที่ช่างจะไปถึงหน้างาน (ETA)"
    )
    oms_etr = models.DateTimeField(
        null=True, blank=True, help_text="เวลาซ่อมเสร็จจาก OMS (ISO Format)"
    )
    oms_etr_updated_at = models.DateTimeField(
        null=True, blank=True, help_text="เวลาที่ OMS อัปเดต ETR ล่าสุด"
    )
    sla_reference_time = models.DateTimeField(
        null=True, blank=True, help_text="เวลาอ้างอิงสำหรับ SLA 4 ชั่วโมง"
    )
    sla_target_time = models.DateTimeField(
        null=True, blank=True, help_text="เวลาเป้าหมาย SLA 4 ชั่วโมง"
    )
    sla_reason = models.CharField(
        max_length=50, blank=True, default="", help_text="เหตุผลที่ตั้ง SLA ล่าสุด"
    )
    assessment_fastest_branch = models.CharField(
        max_length=255, blank=True, help_text="สาขาที่ประเมินว่าไปถึงเร็วที่สุด"
    )
    assessment_eta_formatted = models.CharField(
        max_length=50, blank=True, help_text="ETA label จาก pea-estimated.services"
    )
    assessment_eta_minutes = models.FloatField(
        null=True, blank=True, help_text="ETA เป็นนาทีจาก pea-estimated.services"
    )
    pluem_etr_minutes = models.FloatField(
        null=True, blank=True, help_text="ETR เป็นนาทีจากโมเดลพี่ปลื้ม"
    )
    pluem_etr_target_time = models.DateTimeField(
        null=True, blank=True, help_text="เวลาไฟกลับโดยประมาณจากโมเดลพี่ปลื้ม"
    )
    assessment_payload = models.JSONField(
        default=dict, blank=True, help_text="ผลลัพธ์ล่าสุดจาก pea-estimated.services"
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

    def save(self, *args, **kwargs):
        if self.lv_group_id is None:
            latest_id = (
                OutageCase.objects.exclude(lv_group_id__isnull=True).aggregate(
                    models.Max("lv_group_id")
                )["lv_group_id__max"]
                or 0
            )
            self.lv_group_id = latest_id + 1
        super().save(*args, **kwargs)

    def effective_etr_time(self):
        return self.oms_etr or self.pluem_etr_target_time

    def effective_etr_source(self):
        if self.oms_etr:
            return "oms"
        if self.pluem_etr_target_time:
            return "pluem_model"
        return None

    def set_sla_target(self, reference_time=None, reason=""):
        if reference_time is None:
            case_start_times = [
                value for value in [self.sla_reference_time, self.created_at] if value
            ]
            reference_time = min(case_start_times) if case_start_times else timezone.now()
        self.sla_reference_time = reference_time
        self.sla_target_time = reference_time + timedelta(hours=self.SLA_HOURS)
        self.sla_reason = reason
        return self.sla_target_time

    def get_affected_ca_numbers(self):
        if not self.pk:
            return []

        ca_numbers = (
            self.affected_customers.exclude(ca_number__isnull=True)
            .exclude(ca_number="")
            .values_list("ca_number", flat=True)
            .distinct()
        )
        return sorted(ca_numbers)

    def sync_affected_ca_numbers(self):
        ca_numbers = self.get_affected_ca_numbers()
        if self.affected_ca_numbers != ca_numbers:
            self.affected_ca_numbers = ca_numbers
            OutageCase.objects.filter(pk=self.pk).update(
                affected_ca_numbers=ca_numbers
            )
        return ca_numbers


class CustomerLocation(models.Model):
    timestamp = models.DateTimeField(null=True, blank=True)
    prefix = models.CharField(max_length=50, blank=True)
    fullname = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True)
    ca_number = models.CharField(max_length=12, unique=True, db_index=True)
    phone_number = models.CharField(max_length=50, blank=True)
    pea_area = models.CharField(max_length=255, blank=True)
    user_type = models.CharField(max_length=255, blank=True)
    outage_freq_yearly = models.CharField(max_length=100, blank=True)
    report_channel = models.CharField(max_length=255, blank=True)
    is_ready = models.CharField(max_length=100, blank=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    eta_result = models.FloatField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ca_number"]

    def __str__(self):
        return f"{self.ca_number} - {self.fullname}"


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

    chat_history = models.JSONField(
        default=list, blank=True, help_text="เก็บประวัติสนทนาแบบ dialog"
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
        session_label = self.session_id[:8] if self.session_id else "no-session"
        return f"Report {self.ca_number} (Session: {session_label}...)"


class OutageRestorationLog(models.Model):
    case = models.OneToOneField(
        OutageCase,
        on_delete=models.CASCADE,
        related_name="restoration_log",
    )
    lv_group_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    affected_ca_numbers = models.JSONField(default=list, blank=True)
    restored_at = models.DateTimeField(default=timezone.now)
    eta_target_time_at_restore = models.DateTimeField(null=True, blank=True)
    oms_etr_at_restore = models.DateTimeField(null=True, blank=True)
    pluem_etr_target_time_at_restore = models.DateTimeField(null=True, blank=True)
    effective_etr_at_restore = models.DateTimeField(null=True, blank=True)
    etr_source = models.CharField(max_length=50, blank=True)
    etr_delta_minutes = models.FloatField(null=True, blank=True)
    case_status_at_restore = models.CharField(max_length=20, default="restored")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-restored_at"]

    def __str__(self):
        return f"Restoration log LV {self.lv_group_id or '-'} at {self.restored_at}"
