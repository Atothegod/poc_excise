import csv
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from oms.models import CustomerLocation


class Command(BaseCommand):
    help = "Import CA latitude/longitude master data from ca_lat_lon.csv"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to ca_lat_lon.csv inside the container")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing CustomerLocation rows before import",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_path"])
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        if options["clear"]:
            deleted, _ = CustomerLocation.objects.all().delete()
            self.stdout.write(f"Deleted {deleted} existing customer location rows")

        created_count = 0
        updated_count = 0
        skipped_count = 0
        duplicate_count = 0
        seen_ca_numbers = set()

        with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            required_columns = {"ca_number", "lat", "lon"}
            missing_columns = required_columns - set(reader.fieldnames or [])
            if missing_columns:
                raise CommandError(
                    f"Missing required column(s): {', '.join(sorted(missing_columns))}"
                )

            for row_number, row in enumerate(reader, start=2):
                ca_number = (row.get("ca_number") or "").strip()
                lat = self._parse_float(row.get("lat"))
                lon = self._parse_float(row.get("lon"))

                if not ca_number or lat is None or lon is None:
                    skipped_count += 1
                    self.stderr.write(
                        f"Skipping row {row_number}: missing ca_number/lat/lon"
                    )
                    continue

                if ca_number in seen_ca_numbers:
                    duplicate_count += 1
                    self.stderr.write(
                        f"Skipping row {row_number}: duplicate ca_number {ca_number}"
                    )
                    continue
                seen_ca_numbers.add(ca_number)

                _, created = CustomerLocation.objects.update_or_create(
                    ca_number=ca_number,
                    defaults={
                        "timestamp": self._parse_timestamp(row.get("timestamp")),
                        "prefix": self._clean(row.get("prefix")),
                        "fullname": self._clean(row.get("fullname")),
                        "address": self._clean(row.get("address")),
                        "phone_number": self._clean(row.get("phone_number")),
                        "pea_area": self._clean(row.get("pea_area")),
                        "user_type": self._clean(row.get("user_type")),
                        "outage_freq_yearly": self._clean(
                            row.get("outage_freq_yearly")
                        ),
                        "report_channel": self._clean(row.get("report_channel")),
                        "is_ready": self._clean(row.get("is_ready")),
                        "latitude": lat,
                        "longitude": lon,
                        "eta_result": self._parse_float(row.get("eta_results")),
                    },
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Import complete: "
                f"{created_count} created, {updated_count} updated, "
                f"{skipped_count} skipped, {duplicate_count} duplicates skipped"
            )
        )

    def _clean(self, value):
        return (value or "").strip()

    def _parse_float(self, value):
        value = self._clean(value)
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _parse_timestamp(self, value):
        value = self._clean(value)
        if not value:
            return None

        for date_format in ("%d/%m/%Y, %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(value, date_format)
                return timezone.make_aware(parsed, timezone.get_current_timezone())
            except ValueError:
                continue
        return None
