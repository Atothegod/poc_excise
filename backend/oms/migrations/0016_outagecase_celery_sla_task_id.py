from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0015_outagecase_external_event_id_outage_time"),
    ]

    operations = [
        migrations.AddField(
            model_name="outagecase",
            name="celery_sla_task_id",
            field=models.CharField(
                blank=True,
                help_text="ID ของ Celery Task สำหรับนับเวลา SLA",
                max_length=255,
                null=True,
            ),
        ),
    ]
