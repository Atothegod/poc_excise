from django.core.management.base import BaseCommand
from django.utils import timezone

from oms.case_logic import INACTIVE_CASE_STATUSES
from oms.models import OutageCase
from oms.tasks import check_sla_timeout, schedule_case_timer


class Command(BaseCommand):
    help = "Requeue active cases whose SLA target has already expired."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        cases = list(
            OutageCase.objects.exclude(status__in=INACTIVE_CASE_STATUSES)
            .filter(sla_target_time__lte=timezone.now())
            .order_by("sla_target_time", "case_id")
        )
        if options["dry_run"]:
            self.stdout.write(f"Expired active SLA cases: {len(cases)}")
            return

        for case in cases:
            if case.celery_sla_task_id:
                check_sla_timeout.apply_async(
                    args=[case.case_id],
                    task_id=case.celery_sla_task_id,
                )
            else:
                schedule_case_timer(
                    case,
                    check_sla_timeout,
                    "celery_sla_task_id",
                    [case.case_id],
                    case.sla_target_time,
                )

        self.stdout.write(self.style.SUCCESS(f"Requeued SLA cases: {len(cases)}"))
