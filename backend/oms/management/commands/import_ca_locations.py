import csv
import re
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

        fieldnames, rows = self._read_rows(csv_path)
        missing_columns = self._missing_required_columns(fieldnames)
        if missing_columns:
            raise CommandError(
                f"Missing required column(s): {', '.join(sorted(missing_columns))}"
            )

        for row_number, row in rows:
            ca_number = self._row_value(
                row,
                "ca_number",
                "หมายเลขผู้ใช้ไฟฟ้า (CA 12 หลัก)",
                "หมายเลขผู้ใช้ไฟฟ้า",
                "CA",
            )
            lat, lon = self._coordinates_for_row(row)

            if (
                not ca_number
                or not ca_number.isdigit()
                or len(ca_number) != 12
                or lat is None
                or lon is None
            ):
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

            prefix, fullname = self._name_fields(row)
            _, created = CustomerLocation.objects.update_or_create(
                ca_number=ca_number,
                defaults={
                    "timestamp": self._parse_timestamp(row.get("timestamp")),
                    "prefix": prefix,
                    "fullname": fullname,
                    "address": self._row_value(row, "address", "หน่วยงาน"),
                    "phone_number": self._clean(row.get("phone_number")),
                    "pea_area": self._clean(row.get("pea_area")),
                    "user_type": self._row_value(row, "user_type", "หน่วยงาน"),
                    "outage_freq_yearly": self._clean(row.get("outage_freq_yearly")),
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

    def _read_rows(self, csv_path):
        with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            raw_rows = list(csv.reader(csv_file))

        header_index = None
        for index, raw_row in enumerate(raw_rows):
            columns = {self._clean(value) for value in raw_row}
            if "ca_number" in columns or "หมายเลขผู้ใช้ไฟฟ้า (CA 12 หลัก)" in columns:
                header_index = index
                break
        if header_index is None:
            return [], []

        fieldnames = [self._clean(value) for value in raw_rows[header_index]]
        rows = []
        for row_number, values in enumerate(
            raw_rows[header_index + 1 :],
            start=header_index + 2,
        ):
            if not any(self._clean(value) for value in values):
                continue
            padded_values = [*values, *[""] * max(0, len(fieldnames) - len(values))]
            rows.append((row_number, dict(zip(fieldnames, padded_values))))
        return fieldnames, rows

    def _missing_required_columns(self, fieldnames):
        columns = set(fieldnames or [])
        missing = []
        if not (
            "ca_number" in columns
            or "หมายเลขผู้ใช้ไฟฟ้า (CA 12 หลัก)" in columns
            or "หมายเลขผู้ใช้ไฟฟ้า" in columns
        ):
            missing.append("ca_number")
        has_lat_lon = "lat" in columns and "lon" in columns
        has_coordinate_note = "หมายเหตุ" in columns
        if not has_lat_lon and not has_coordinate_note:
            missing.extend(["lat", "lon"])
        return missing

    def _row_value(self, row, *keys):
        for key in keys:
            value = self._clean(row.get(key))
            if value:
                return value
        return ""

    def _coordinates_for_row(self, row):
        lat = self._parse_float(row.get("lat"))
        lon = self._parse_float(row.get("lon"))
        if lat is not None and lon is not None:
            return lat, lon

        coordinate_text = self._row_value(row, "หมายเหตุ", "coordinates")
        match = re.fullmatch(
            r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*",
            coordinate_text,
        )
        if not match:
            return None, None
        return self._parse_float(match.group(1)), self._parse_float(match.group(2))

    def _name_fields(self, row):
        prefix = self._clean(row.get("prefix"))
        fullname = self._row_value(row, "fullname", "ชื่อ - สกุล", "name")
        if not prefix and fullname.startswith("คุณ"):
            return "คุณ", fullname[len("คุณ") :].strip()
        return prefix, fullname

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
