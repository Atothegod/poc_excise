from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta
from .case_logic import INACTIVE_CASE_STATUSES
from .models import OutageCase, CustomerReport, OutageRestorationLog
from .tasks import (
    check_eta_timeout,
    check_etr_timeout,
    check_sla_timeout,
    schedule_case_timer,
    send_proactive_alert,
)
from pea_project.celery import app as celery_app


def _create_restoration_log(instance):
    restored_at = timezone.now()
    effective_etr = instance.effective_etr_time()
    etr_delta_minutes = None
    if effective_etr:
        etr_delta_minutes = (restored_at - effective_etr).total_seconds() / 60

    affected_ca_numbers = instance.sync_affected_ca_numbers()
    OutageRestorationLog.objects.get_or_create(
        case=instance,
        defaults={
            "lv_group_id": instance.lv_group_id,
            "affected_ca_numbers": affected_ca_numbers,
            "restored_at": restored_at,
            "eta_target_time_at_restore": instance.eta_target_time,
            "oms_etr_at_restore": instance.oms_etr,
            "pluem_etr_target_time_at_restore": instance.pluem_etr_target_time,
            "effective_etr_at_restore": effective_etr,
            "etr_source": instance.effective_etr_source() or "",
            "etr_delta_minutes": etr_delta_minutes,
            "case_status_at_restore": instance.status,
        },
    )


def _format_time_label(target_time):
    if not target_time:
        return None
    return timezone.localtime(target_time).strftime("%H:%M น.")


def _closed_loop_message(ca_number, closed_loop_kind=None):
    if closed_loop_kind == "sla_expired":
        return (
            f"เรียนผู้ใช้ไฟฟ้าหมายเลข CA {ca_number} "
            "ครบกำหนดเวลาดำเนินการของเคสแล้วค่ะ "
            "ขณะนี้ไฟฟ้ากลับมาใช้งานได้หรือยังคะ"
        )
    return (
        f"เรียนผู้ใช้ไฟฟ้าหมายเลข CA {ca_number} "
        "เจ้าหน้าที่ได้ทำการแก้ไขและจ่ายไฟคืนเรียบร้อยแล้วค่ะ "
        "ขณะนี้ไฟฟ้ากลับมาใช้งานได้หรือยังคะ"
    )


def _closed_loop_recipient_reports(case):
    case_ids = [case.pk]
    if case.pk:
        case_ids.extend(
            OutageCase.objects.filter(merged_into=case).values_list("pk", flat=True)
        )

    return (
        CustomerReport.objects.filter(
            related_case_id__in=case_ids,
            is_resolved=False,
        )
        .select_related("related_case")
        .order_by("-updated_at", "-id")
    )


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
                OutageCase.objects.filter(pk=instance.pk).update(
                    celery_eta_task_id=None
                )

            # เก็บ Flag ไว้ให้ post_save รู้ว่าต้องตั้งเวลาใหม่
            instance._needs_new_eta_task = True if instance.eta_target_time else False

        # 2. เช็คว่าถ้าสถานะเปลี่ยนเป็น restored ให้เก็บ Flag ไว้
        if old_instance.status != "restored" and instance.status == "restored":
            instance._is_just_restored = True
        if (
            old_instance.status not in INACTIVE_CASE_STATUSES
            and instance.status in INACTIVE_CASE_STATUSES
        ):
            instance._is_just_inactive = True

        # 3. เช็คว่า OMS เพิ่งส่ง/แก้ ETR มาไหม เพื่อแจ้งลูกค้าอัตโนมัติ
        if old_instance.oms_etr and (
            not instance.oms_etr or instance.oms_etr <= old_instance.oms_etr
        ):
            instance.oms_etr = old_instance.oms_etr
        elif old_instance.oms_etr != instance.oms_etr and instance.oms_etr:
            instance._has_new_oms_etr = True
            instance._old_celery_etr_task_id = old_instance.celery_etr_task_id

        # 4. เช็คว่า SLA ถูกแก้ผ่าน Admin หรือ flow อื่นที่ save model โดยตรงหรือไม่
        if old_instance.sla_target_time != instance.sla_target_time:
            instance._needs_new_sla_task = bool(instance.sla_target_time)
            instance._old_celery_sla_task_id = old_instance.celery_sla_task_id

    except OutageCase.DoesNotExist:
        pass


@receiver(post_save, sender=OutageCase)
def process_outage_case_updates(sender, instance, created, **kwargs):
    """
    จัดการหลังบันทึก Database เสร็จสิ้น (ตั้งเวลา Celery ใหม่ และจัดการ Closed-Loop)
    """
    if created and not instance.sla_target_time:
        reference_time = instance.created_at or timezone.now()
        sla_target_time = reference_time + timedelta(hours=OutageCase.SLA_HOURS)
        instance.sla_reference_time = reference_time
        instance.sla_target_time = sla_target_time
        instance.sla_reason = "case_created"
        OutageCase.objects.filter(pk=instance.pk).update(
            sla_reference_time=reference_time,
            sla_target_time=sla_target_time,
            sla_reason="case_created",
        )

    # --- กรณีพนักงานแก้ไขเวลา ETA หน้า Admin ---
    if getattr(instance, "_needs_new_eta_task", False) and instance.eta_target_time:
        report = instance.affected_customers.filter(is_resolved=False).last()
        if report:
            # สร้างซองจดหมาย Task ใหม่
            schedule_case_timer(
                instance,
                check_eta_timeout,
                "celery_eta_task_id",
                [instance.case_id, report.id],
                instance.eta_target_time,
            )
        instance._needs_new_eta_task = False

    # --- กรณี OMS/Admin เติมหรือแก้ ETR ---
    if getattr(instance, "_has_new_oms_etr", False):
        etr_updated_at = timezone.now()
        old_etr_task_id = getattr(instance, "_old_celery_etr_task_id", None)
        if old_etr_task_id:
            celery_app.control.revoke(old_etr_task_id, terminate=True)

        etr_task_id = None
        if instance.status not in INACTIVE_CASE_STATUSES:
            schedule_case_timer(
                instance,
                check_etr_timeout,
                "celery_etr_task_id",
                [instance.case_id],
                instance.oms_etr,
            )
            etr_task_id = instance.celery_etr_task_id

        instance.oms_etr_updated_at = etr_updated_at
        instance.celery_etr_task_id = etr_task_id
        OutageCase.objects.filter(pk=instance.pk).update(
            oms_etr_updated_at=etr_updated_at,
            celery_etr_task_id=etr_task_id,
        )

        if instance.status not in INACTIVE_CASE_STATUSES:
            instance.sync_affected_ca_numbers()
            etr_label = _format_time_label(instance.oms_etr)
            message = f"อัปเดตล่าสุด คาดว่าจะจ่ายไฟคืนประมาณ {etr_label} ค่ะ"
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
            instance._etr_update_session_ids = sent_session_ids

        instance._has_new_oms_etr = False

    # --- กรณี SLA target ถูกแก้ไขผ่าน Admin หรือ model save โดยตรง ---
    if hasattr(instance, "_old_celery_sla_task_id") or getattr(
        instance, "_needs_new_sla_task", False
    ):
        old_sla_task_id = getattr(instance, "_old_celery_sla_task_id", None)
        if old_sla_task_id:
            celery_app.control.revoke(old_sla_task_id, terminate=True)

        sla_task_id = None
        if (
            getattr(instance, "_needs_new_sla_task", False)
            and instance.sla_target_time
            and instance.status not in INACTIVE_CASE_STATUSES
        ):
            schedule_case_timer(
                instance,
                check_sla_timeout,
                "celery_sla_task_id",
                [instance.case_id],
                instance.sla_target_time,
            )
            sla_task_id = instance.celery_sla_task_id

        instance.celery_sla_task_id = sla_task_id
        OutageCase.objects.filter(pk=instance.pk).update(
            celery_sla_task_id=sla_task_id
        )
        instance._needs_new_sla_task = False

    # --- กรณีการปิดเคส (Closed-Loop & State Cleansing) ---
    if getattr(instance, "_is_just_restored", False):
        closed_loop_kind = getattr(instance, "_closed_loop_kind", None)
        _create_restoration_log(instance)

        # 1. ยกเลิก Timers ที่ค้างอยู่ของเคสนี้ทิ้งทั้งหมด (State Cleansing)
        if instance.celery_eta_task_id:
            celery_app.control.revoke(instance.celery_eta_task_id, terminate=True)
        if instance.celery_etr_task_id:
            celery_app.control.revoke(instance.celery_etr_task_id, terminate=True)
        if instance.celery_sla_task_id:
            celery_app.control.revoke(instance.celery_sla_task_id, terminate=True)

        OutageCase.objects.filter(pk=instance.pk).update(
            celery_eta_task_id=None, celery_etr_task_id=None, celery_sla_task_id=None
        )

        # 2. ค้นหาลูกค้าทุกคนในเคสนี้ รวมเคสย่อยที่เคยถูก merge เข้า anchor
        affected_customers = _closed_loop_recipient_reports(instance)
        sent_session_ids = set()

        for report in affected_customers:
            report.is_resolved = True
            report.save()

            if not report.session_id or report.session_id in sent_session_ids:
                continue
            sent_session_ids.add(report.session_id)

            # 3. ส่งข้อความยืนยันไฟมาเชิงรุกไปหาลูกค้า
            message = _closed_loop_message(report.ca_number, closed_loop_kind)
            send_proactive_alert.delay(
                report_id=report.id,
                message=message,
                event_type="closed_loop_prompt",
                closed_loop_kind=closed_loop_kind,
            )

        instance._is_just_restored = False
        instance._is_just_inactive = False

    if getattr(instance, "_is_just_inactive", False) and not getattr(
        instance, "_is_just_restored", False
    ):
        if instance.celery_eta_task_id:
            celery_app.control.revoke(instance.celery_eta_task_id, terminate=True)
        if instance.celery_etr_task_id:
            celery_app.control.revoke(instance.celery_etr_task_id, terminate=True)
        if instance.celery_sla_task_id:
            celery_app.control.revoke(instance.celery_sla_task_id, terminate=True)

        OutageCase.objects.filter(pk=instance.pk).update(
            celery_eta_task_id=None, celery_etr_task_id=None, celery_sla_task_id=None
        )
        instance._is_just_inactive = False


@receiver(post_save, sender=CustomerReport)
def sync_case_affected_ca(sender, instance, **kwargs):
    if instance.related_case_id:
        instance.related_case.sync_affected_ca_numbers()
