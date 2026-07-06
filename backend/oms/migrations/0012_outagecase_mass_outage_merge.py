# Generated for threshold-based mass outage merge support.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0011_remove_customerreport_legacy_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="outagecase",
            name="case_type",
            field=models.CharField(
                choices=[
                    ("normal", "เคสปกติ"),
                    ("fast_track", "เคสเร่งด่วน"),
                    ("mass_outage", "เหตุไฟดับวงกว้าง"),
                ],
                db_index=True,
                default="normal",
                help_text="ประเภทเคส เช่น normal, fast_track หรือ mass_outage",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="outagecase",
            name="status",
            field=models.CharField(
                choices=[
                    ("reported", "ได้รับแจ้งเหตุ"),
                    ("investigating", "กำลังดำเนินการตรวจสอบ"),
                    ("repairing", "กำลังดำเนินการซ่อมแซม"),
                    ("restored", "จ่ายไฟคืนกระแสสำเร็จ"),
                    ("merged", "ถูกรวมเข้าเคสอื่น"),
                ],
                default="reported",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="outagecase",
            name="merged_at",
            field=models.DateTimeField(
                blank=True,
                help_text="เวลาที่เคสนี้ถูกรวมเข้า anchor case",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="outagecase",
            name="merged_into",
            field=models.ForeignKey(
                blank=True,
                help_text="Anchor case ที่รับ reports หลังจากเคสนี้ถูกรวม",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="merged_cases",
                to="oms.outagecase",
            ),
        ),
    ]
