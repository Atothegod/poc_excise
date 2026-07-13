from datetime import timedelta
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from oms.models import AgentJob
from oms.tasks import process_agent_job


class Command(BaseCommand):
    help = "Requeue queued/running agent jobs left stale after a worker crash."

    def add_arguments(self, parser):
        parser.add_argument("--stale-minutes", type=int, default=5)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options["stale_minutes"])
        job_ids = list(
            AgentJob.objects.filter(
                status__in=AgentJob.ACTIVE_STATUSES,
                updated_at__lt=cutoff,
            )
            .order_by("updated_at")
            .values_list("id", flat=True)[: options["limit"]]
        )
        requeued = 0
        for job_id in job_ids:
            task_id = str(uuid4())
            with transaction.atomic():
                job = AgentJob.objects.select_for_update().filter(pk=job_id).first()
                if not job or job.status not in AgentJob.ACTIVE_STATUSES:
                    continue
                if job.updated_at >= cutoff:
                    continue
                job.status = AgentJob.STATUS_QUEUED
                job.celery_task_id = task_id
                job.error_code = ""
                job.error_message = ""
                job.save(
                    update_fields=[
                        "status",
                        "celery_task_id",
                        "error_code",
                        "error_message",
                        "updated_at",
                    ]
                )
            try:
                process_agent_job.apply_async(
                    args=[str(job_id)],
                    task_id=task_id,
                    queue="agent",
                )
            except Exception as exc:
                AgentJob.objects.filter(pk=job_id, celery_task_id=task_id).update(
                    error_code="requeue_publish_failed",
                    error_message=str(exc)[:2000],
                )
                self.stderr.write(f"Could not requeue {job_id}: {exc}")
                continue
            requeued += 1

        self.stdout.write(self.style.SUCCESS(f"Requeued {requeued} agent job(s)."))
