from django.db import migrations, models


INACTIVE_STATUSES = {"restored", "merged"}


def _active_anchor(case):
    seen = set()
    while case and case.status == "merged" and case.merged_into_id:
        if case.pk in seen:
            return None
        seen.add(case.pk)
        case = case.merged_into
    if case and case.status not in INACTIVE_STATUSES:
        return case
    return None


def normalize_active_reports(apps, schema_editor):
    CustomerReport = apps.get_model("oms", "CustomerReport")
    session_ids = (
        CustomerReport.objects.filter(
            is_resolved=False,
            session_id__isnull=False,
        )
        .exclude(session_id="")
        .values_list("session_id", flat=True)
        .distinct()
    )

    for session_id in session_ids.iterator():
        reports = list(
            CustomerReport.objects.filter(
                session_id=session_id,
                is_resolved=False,
            )
            .select_related("related_case", "related_case__merged_into")
            .order_by("-updated_at", "-id")
        )
        if not reports:
            continue

        merged_candidates = [
            report
            for report in reports
            if report.related_case and report.related_case.status == "merged"
        ]
        direct_candidates = [
            report for report in reports if report not in merged_candidates
        ]
        canonical = None
        for report in [*merged_candidates, *direct_candidates]:
            anchor = _active_anchor(report.related_case)
            if anchor:
                canonical = report
                if report.related_case_id != anchor.pk:
                    report.related_case_id = anchor.pk
                    report.save(update_fields=["related_case"])
                break

        canonical = canonical or reports[0]
        stale_ids = [report.id for report in reports if report.id != canonical.id]
        if stale_ids:
            CustomerReport.objects.filter(id__in=stale_ids).update(is_resolved=True)


class Migration(migrations.Migration):
    dependencies = [("oms", "0016_outagecase_celery_sla_task_id")]

    operations = [
        migrations.RunPython(normalize_active_reports, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="customerreport",
            constraint=models.UniqueConstraint(
                fields=("session_id",),
                condition=(
                    models.Q(is_resolved=False, session_id__isnull=False)
                    & ~models.Q(session_id="")
                ),
                name="unique_active_customer_report_per_session",
            ),
        ),
    ]
