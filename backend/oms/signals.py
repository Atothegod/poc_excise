from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone
from .models import OutageCase, CustomerReport
from .tasks import check_eta_timeout, send_proactive_alert
from pea_project.celery import app as celery_app


@receiver(pre_save, sender=OutageCase)
def track_eta_changes(sender, instance, **kwargs):
    """
    ตรวจจับก่อนบันทึกลง Database ว่าเวลา ETA ถูกพนักงานแก้ไขผ่านหน้า Admin หรือไม่
    """
    if not instance.pk:
        return  # เป็นการสร้างเคสใหม่ ข้ามไปก่อน

    try:
        old_instance = OutageCase.objects.get(pk=instance.pk)

        # 1. เช็คว่าเวลา ETA มีการเปลี่ยนแปลงไหม (พนักงานแก้เวลาหน้า Admin)
        if old_instance.eta_target_time != instance.eta_target_time:
            # ถ้ายกเลิกเวลา หรือแก้เวลาใหม่ ให้สั่งยกเลิก Task เก่าที่รออยู่ใน Redis ทิ้งทันที!
            if old_instance.celery_eta_task_id:
                celery_app.control.revoke(
                    old_instance.celery_eta_task_id, terminate=True
                )
                instance.celery_eta_task_id = None

            # เก็บ Flag ไว้ให้ post_save รู้ว่าต้องตั้งเวลาใหม่
            instance._needs_new_eta_task = True if instance.eta_target_time else False

        # 2. เช็คว่าถ้าสถานะเปลี่ยนเป็น restored ให้เก็บ Flag ไว้
        if old_instance.status != "restored" and instance.status == "restored":
            instance._is_just_restored = True

        # 3. เช็คว่า OMS เพิ่งส่ง/แก้ ETR มาไหม เพื่อแจ้งลูกค้าอัตโนมัติ
        if old_instance.oms_etr != instance.oms_etr and instance.oms_etr:
            instance._has_new_oms_etr = True

    except OutageCase.DoesNotExist:
        pass


@receiver(post_save, sender=OutageCase)
def process_outage_case_updates(sender, instance, created, **kwargs):
    """
    จัดการหลังบันทึก Database เสร็จสิ้น (ตั้งเวลา Celery ใหม่ และจัดการ Closed-Loop)
    """
    # --- กรณีพนักงานแก้ไขเวลา ETA หน้า Admin ---
    if getattr(instance, "_needs_new_eta_task", False) and instance.eta_target_time:
        report = instance.affected_customers.filter(is_resolved=False).last()
        if report:
            # สร้างซองจดหมาย Task ใหม่
            task = check_eta_timeout.apply_async(
                args=[instance.case_id, report.id], eta=instance.eta_target_time
            )
            # อัปเดต Task ID ใหม่ลงไปแบบไม่ trigger signal ซ้ำ
            OutageCase.objects.filter(pk=instance.pk).update(celery_eta_task_id=task.id)
        instance._needs_new_eta_task = False

    # --- กรณี OMS/Admin เติมหรือแก้ ETR ---
    if getattr(instance, "_has_new_oms_etr", False):
        etr_label = timezone.localtime(instance.oms_etr).strftime("%H:%M น.")
        message = (
            "ระบบได้รับข้อมูล ETR ล่าสุดจาก OMS แล้วครับ "
            f"เวลาที่คาดว่าจะแก้ไขเสร็จและจ่ายไฟคืนคือประมาณ {etr_label} ครับ"
        )
        sent_session_ids = set()
        affected_customers = CustomerReport.objects.filter(
            related_case=instance, is_resolved=False
        ).exclude(session_id__isnull=True).exclude(session_id="")

        for report in affected_customers:
            if report.session_id in sent_session_ids:
                continue
            sent_session_ids.add(report.session_id)
            send_proactive_alert.delay(
                report_id=report.id, message=message, event_type="etr_update"
            )

        instance._has_new_oms_etr = False

    # --- กรณีการปิดเคส (Closed-Loop & State Cleansing) ---
    if getattr(instance, "_is_just_restored", False):
        # 1. ยกเลิก Timers ที่ค้างอยู่ของเคสนี้ทิ้งทั้งหมด (State Cleansing)
        if instance.celery_eta_task_id:
            celery_app.control.revoke(instance.celery_eta_task_id, terminate=True)
        if instance.celery_etr_task_id:
            celery_app.control.revoke(instance.celery_etr_task_id, terminate=True)

        OutageCase.objects.filter(pk=instance.pk).update(
            celery_eta_task_id=None, celery_etr_task_id=None
        )

        # 2. ค้นหาลูกค้าทุกคนในเคสนี้
        affected_customers = CustomerReport.objects.filter(
            related_case=instance, is_resolved=False
        )

        for report in affected_customers:
            report.is_resolved = True
            report.save()

            # 3. ส่งข้อความยืนยันไฟมาเชิงรุกไปหาลูกค้า
            message = (
                "ขณะนี้ระบบแจ้งว่าการไฟฟ้าได้ดำเนินการจ่ายไฟคืนระบบเรียบร้อยแล้ว "
                "ขออภัยในความไม่สะดวกครับ 🙏\n\n"
                "หากบ้านของท่านยังคงไม่มีไฟใช้ รบกวนแจ้งว่า 'ยังใช้งานไม่ได้' "
                "เพื่อให้ระบบตรวจสอบเพิ่มเติมครับ"
            )
            send_proactive_alert.delay(
                report_id=report.id, message=message, event_type="closed_loop_prompt"
            )

        instance._is_just_restored = False
