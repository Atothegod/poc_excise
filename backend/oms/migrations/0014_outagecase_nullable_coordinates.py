from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("oms", "0013_remove_fast_track_case_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="outagecase",
            name="latitude",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="outagecase",
            name="longitude",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
