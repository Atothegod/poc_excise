# Generated for removing fast-track case behavior.

from django.db import migrations, models


def reclassify_fast_track_cases(apps, schema_editor):
    OutageCase = apps.get_model("oms", "OutageCase")
    OutageCase.objects.filter(case_type="fast_track").update(case_type="normal")
    OutageCase.objects.filter(sla_reason="fast_track").update(sla_reason="case_created")


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0012_outagecase_mass_outage_merge"),
    ]

    operations = [
        migrations.RunPython(reclassify_fast_track_cases, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="outagecase",
            name="case_type",
            field=models.CharField(
                choices=[
                    ("normal", "เคสปกติ"),
                    ("mass_outage", "เหตุไฟดับวงกว้าง"),
                ],
                db_index=True,
                default="normal",
                help_text="ประเภทเคส เช่น normal หรือ mass_outage",
                max_length=20,
            ),
        ),
    ]
