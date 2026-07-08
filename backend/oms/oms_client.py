from urllib.parse import urljoin

import requests
from django.conf import settings


class OmsClientError(Exception):
    pass


def _url(path):
    base_url = settings.OMS_API_URL.rstrip("/") + "/"
    return urljoin(base_url, path.lstrip("/"))


def _request(method, path, **kwargs):
    try:
        response = requests.request(method, _url(path), timeout=15, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise OmsClientError(str(exc)) from exc

    if response.status_code == 404:
        return None

    try:
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise OmsClientError(str(exc)) from exc


def get_customer(ca_number):
    return _request("GET", f"/customers/{ca_number}")


def report_outage(ca_number):
    return _request("POST", "/cases/report", json={"ca_number": ca_number})
