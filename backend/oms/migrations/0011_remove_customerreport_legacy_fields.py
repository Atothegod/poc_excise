from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0010_normalize_sla_to_case_start"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="customerreport",
            name="fast_track_quota",
        ),
        migrations.RemoveField(
            model_name="customerreport",
            name="needs_eta",
        ),
        migrations.RemoveField(
            model_name="customerreport",
            name="needs_etr",
        ),
    ]
