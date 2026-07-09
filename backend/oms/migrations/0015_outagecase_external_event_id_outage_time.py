from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0014_outagecase_nullable_coordinates"),
    ]

    operations = [
        migrations.AddField(
            model_name="outagecase",
            name="external_event_id",
            field=models.CharField(
                blank=True,
                help_text="รหัส event ภายนอกจาก OMS/PEA",
                max_length=100,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="outagecase",
            name="outage_time",
            field=models.DateTimeField(
                blank=True,
                help_text="เวลาที่ OMS แจ้งว่าเริ่มเกิดเหตุไฟดับ",
                null=True,
            ),
        ),
    ]
