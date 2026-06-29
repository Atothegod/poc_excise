import csv
import json
from datetime import date, datetime

from django.http import HttpResponse
from django.utils import timezone


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def csv_response(filename_prefix):
    exported_at = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M%S")
    filename = f"{filename_prefix}-{exported_at}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    return response


def write_csv(response, headers, rows):
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_value(value) for value in row])
    return response
