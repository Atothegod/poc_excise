import csv
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


CA_CSV_PATH = Path(os.getenv("CA_CSV_PATH", "/app/ca_lat_lon_2.csv"))
DJANGO_OMS_EVENT_URL = os.getenv("DJANGO_OMS_EVENT_URL", "")
OMS_API_TOKEN = os.getenv("OMS_API_TOKEN", "")
CASE_LINK_RADIUS_KM = float(os.getenv("CASE_LINK_RADIUS_KM", "0.5"))
MASS_OUTAGE_CONFIRMATION_COUNT = int(
    os.getenv("MASS_OUTAGE_CONFIRMATION_COUNT", "3")
)

API_STATUS_OPEN = "OPEN"
API_STATUS_CLOSED = "CLOSED"
STATUS_REPORTED = "reported"
STATUS_RESTORED = "restored"
CASE_TYPE_NORMAL = "normal"
CASE_TYPE_MASS_OUTAGE = "mass_outage"

CUSTOMERS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="PEA OMS API")


class ReportRequest(BaseModel):
    ca_number: str = Field(min_length=12, max_length=12)


class CaseMutationRequest(BaseModel):
    case_id: str | None = None
    affected_ca_numbers: list[str] = Field(default_factory=list)
    oms_etr: datetime | None = None
    status: str | None = None


class CasePatchRequest(BaseModel):
    affected_ca_numbers: list[str] | None = None
    oms_etr: datetime | None = None
    status: str | None = None


class OutageSyncRequest(BaseModel):
    eventId: str = Field(min_length=1)
    caList: list[str] = Field(min_length=1)
    outageTime: datetime
    etr: datetime | None = None
    status: str


@app.on_event("startup")
def startup():
    load_customers()


def load_customers():
    CUSTOMERS.clear()
    if not CA_CSV_PATH.exists():
        return

    with CA_CSV_PATH.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = {"ca_number", "lat", "lon"}
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise RuntimeError(
                f"Missing required CA CSV columns: {', '.join(sorted(missing_columns))}"
            )

        for row in reader:
            ca_number = clean_ca(row.get("ca_number"))
            lat = parse_float(row.get("lat"))
            lon = parse_float(row.get("lon"))
            if not ca_number or lat is None or lon is None:
                continue
            CUSTOMERS[ca_number] = {
                "ca_number": ca_number,
                "lat": lat,
                "lon": lon,
                "fullname": (row.get("fullname") or "").strip(),
            }


def clean_ca(value):
    ca_number = str(value or "").strip()
    return ca_number if len(ca_number) == 12 and ca_number.isdigit() else ""


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def serialize_datetime(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def calculate_distance_km(lat1, lon1, lat2, lon2):
    if None in [lat1, lon1, lat2, lon2]:
        return float("inf")

    earth_radius_km = 6371.0
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalize_ca_list(ca_numbers):
    seen = set()
    normalized = []
    for raw_ca in ca_numbers or []:
        ca_number = clean_ca(raw_ca)
        if ca_number and ca_number not in seen:
            seen.add(ca_number)
            normalized.append(ca_number)
    return normalized


def customer_or_404(ca_number):
    normalized = clean_ca(ca_number)
    customer = CUSTOMERS.get(normalized)
    if not customer:
        raise HTTPException(status_code=404, detail="CA not found in OMS customer CSV")
    return customer


def validate_bearer_token(authorization: str | None):
    if not OMS_API_TOKEN:
        return
    expected = f"Bearer {OMS_API_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def case_type_for(affected_ca_numbers: list[str]):
    if len(affected_ca_numbers) >= MASS_OUTAGE_CONFIRMATION_COUNT:
        return CASE_TYPE_MASS_OUTAGE
    return CASE_TYPE_NORMAL


def api_status_to_case_status(value: str):
    normalized = str(value or "").strip().upper()
    if normalized == API_STATUS_OPEN:
        return STATUS_REPORTED
    if normalized == API_STATUS_CLOSED:
        return STATUS_RESTORED
    raise HTTPException(status_code=422, detail="status must be OPEN or CLOSED")


def event_type_for_status(case_status: str, has_etr: bool = False):
    if case_status == STATUS_RESTORED:
        return "case_closed"
    if has_etr:
        return "etr_updated"
    return "case_opened"


def build_case_payload(
    *,
    affected_ca_numbers: list[str] | None = None,
    case_id: str | None = None,
    status: str = STATUS_REPORTED,
    oms_etr: datetime | None = None,
    external_event_id: str | None = None,
    outage_time: datetime | None = None,
):
    affected = normalize_ca_list(affected_ca_numbers or [])
    anchor = CUSTOMERS.get(affected[0]) if affected else None
    payload = {
        "status": status,
        "case_type": case_type_for(affected),
        "external_event_id": external_event_id,
        "affected_ca_numbers": affected,
        "outage_time": serialize_datetime(outage_time),
        "oms_etr": serialize_datetime(oms_etr),
        "etr_target_time": serialize_datetime(oms_etr),
        "etr_source": "oms" if oms_etr else None,
        "anchor_ca_number": affected[0] if affected else None,
        "updated_at": serialize_datetime(datetime.now(timezone.utc)),
    }
    if case_id:
        payload["case_id"] = case_id
    if anchor:
        payload["anchor_latitude"] = anchor["lat"]
        payload["anchor_longitude"] = anchor["lon"]
    return payload


def django_event(event_type: str, case_payload: dict[str, Any]):
    if not DJANGO_OMS_EVENT_URL:
        raise HTTPException(
            status_code=503,
            detail="DJANGO_OMS_EVENT_URL is not configured",
        )

    try:
        response = requests.post(
            DJANGO_OMS_EVENT_URL,
            json={"event_type": event_type, "case": case_payload},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Django callback failed: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Django rejected OMS event",
                "django_status": response.status_code,
                "django_body": response.text,
            },
        )

    try:
        return response.json()
    except ValueError:
        return {"status": "success"}


def proxy_case_event(event_type: str, case_payload: dict[str, Any]):
    django_response = django_event(event_type, case_payload)
    return {
        **case_payload,
        "event_type": event_type,
        "case": case_payload,
        "django": django_response,
    }


def spec_response(event_id: str, updated_at):
    return {
        "statusCode": 200,
        "message": "Outage event synced successfully",
        "data": {
            "eventId": event_id,
            "updatedAt": serialize_datetime(updated_at),
        },
    }


def provided_fields(model):
    fields = getattr(model, "model_fields_set", None)
    if fields is not None:
        return fields
    return getattr(model, "__fields_set__", set())


@app.get("/health")
def health():
    return {
        "status": "ok",
        "customers_loaded": len(CUSTOMERS),
        "case_link_radius_km": CASE_LINK_RADIUS_KM,
        "mass_outage_confirmation_count": MASS_OUTAGE_CONFIRMATION_COUNT,
        "django_oms_event_url_configured": bool(DJANGO_OMS_EVENT_URL),
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OMS Case Console</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root { color-scheme: light; --line: #d7dde8; --ink: #172033; --muted: #5e6b82; --accent: #0f766e; --blue: #2563eb; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #f6f8fb; }
    header { height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 18px; border-bottom: 1px solid var(--line); background: #fff; }
    h1 { margin: 0; font-size: 18px; font-weight: 750; }
    main { display: grid; grid-template-columns: minmax(460px, 1fr) 390px; min-height: calc(100vh - 56px); }
    #map { min-height: calc(100vh - 56px); }
    aside { border-left: 1px solid var(--line); background: #fff; padding: 16px; overflow: auto; }
    fieldset { border: 0; padding: 0; margin: 0 0 18px; display: grid; gap: 12px; }
    legend { font-size: 12px; text-transform: uppercase; color: var(--muted); font-weight: 800; margin-bottom: 8px; }
    label { display: grid; gap: 6px; font-size: 13px; font-weight: 650; }
    input, textarea, button { width: 100%; font: inherit; border-radius: 6px; border: 1px solid #c9d2df; padding: 9px 10px; background: #fff; }
    textarea { min-height: 118px; resize: vertical; }
    button { cursor: pointer; font-weight: 750; color: #fff; border-color: var(--accent); background: var(--accent); }
    button.secondary { color: var(--ink); border-color: #c9d2df; background: #fff; }
    button.close { border-color: #b91c1c; background: #b91c1c; }
    .actions { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .row { display: grid; grid-template-columns: 1fr 92px; gap: 8px; align-items: end; }
    .meta { color: var(--muted); font-size: 12px; line-height: 1.45; }
    pre { white-space: pre-wrap; min-height: 130px; margin: 0; padding: 12px; border-radius: 6px; color: #dbeafe; background: #101827; overflow: auto; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      #map { min-height: 54vh; }
      aside { border-left: 0; border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <h1>OMS Case Console</h1>
    <span class="meta" id="summary">Loading customers...</span>
  </header>
  <main>
    <div id="map"></div>
    <aside>
      <fieldset>
        <legend>Selection</legend>
        <div class="row">
          <label>Radius KM <input id="radiusKm" type="number" min="0.1" step="0.1" value="0.5"></label>
          <button class="secondary" id="selectNearby" type="button">Select</button>
        </div>
        <div class="meta" id="selectedMeta">Select a CA marker on the map.</div>
      </fieldset>
      <fieldset>
        <legend>Case</legend>
        <label>Case ID <input id="caseId"></label>
        <label>Affected CA Numbers <textarea id="affectedCaNumbers"></textarea></label>
        <label>OMS ETR <input id="omsEtr" type="datetime-local"></label>
        <div class="actions">
          <button id="openCase" type="button">Open</button>
          <button class="secondary" id="updateCase" type="button">Update</button>
          <button class="close" id="closeCase" type="button">Close</button>
        </div>
      </fieldset>
      <fieldset>
        <legend>Result</legend>
        <pre id="result">Ready</pre>
      </fieldset>
    </aside>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const result = document.getElementById("result");
    const caseId = document.getElementById("caseId");
    const affectedCaNumbers = document.getElementById("affectedCaNumbers");
    const omsEtr = document.getElementById("omsEtr");
    const radiusKm = document.getElementById("radiusKm");
    const selectedMeta = document.getElementById("selectedMeta");
    let selectedCustomer = null;
    let radiusCircle = null;
    const map = L.map("map").setView([14.0, 100.0], 14);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap"
    }).addTo(map);

    const splitCa = (value) => value.split(/[\\s,]+/).map((item) => item.trim()).filter(Boolean);
    const isoOrNull = (value) => value ? new Date(value).toISOString() : null;
    const uuid = () => crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, c => (+c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> +c / 4).toString(16));
    const show = (data) => { result.textContent = JSON.stringify(data, null, 2); };

    async function loadCustomers() {
      const response = await fetch("/customers?limit=5000");
      const data = await response.json();
      document.getElementById("summary").textContent = `${data.total} customers loaded`;
      const bounds = [];
      data.customers.forEach((customer) => {
        const marker = L.circleMarker([customer.lat, customer.lon], {
          radius: 6,
          color: "#1d4ed8",
          weight: 2,
          fillColor: "#60a5fa",
          fillOpacity: 0.8
        }).addTo(map);
        marker.bindTooltip(customer.ca_number);
        marker.on("click", () => {
          selectedCustomer = customer;
          selectedMeta.textContent = `${customer.ca_number} selected`;
          if (radiusCircle) radiusCircle.remove();
          radiusCircle = L.circle([customer.lat, customer.lon], {
            radius: Number(radiusKm.value || 0.5) * 1000,
            color: "#2563eb",
            fillColor: "#93c5fd",
            fillOpacity: 0.18,
            weight: 2
          }).addTo(map);
        });
        bounds.push([customer.lat, customer.lon]);
      });
      if (bounds.length) map.fitBounds(bounds, { padding: [24, 24] });
    }

    radiusKm.addEventListener("input", () => {
      if (radiusCircle) radiusCircle.setRadius(Number(radiusKm.value || 0.5) * 1000);
    });

    document.getElementById("selectNearby").addEventListener("click", async () => {
      if (!selectedCustomer) return;
      const response = await fetch(`/customers/nearby?ca_number=${encodeURIComponent(selectedCustomer.ca_number)}&radius_km=${encodeURIComponent(radiusKm.value || "0.5")}`);
      const data = await response.json();
      affectedCaNumbers.value = data.customers.map((customer) => customer.ca_number).join("\\n");
      selectedMeta.textContent = `${data.customers.length} CA selected`;
      show(data);
    });

    async function submit(action) {
      const id = caseId.value.trim() || uuid();
      if (!caseId.value.trim()) caseId.value = id;
      const payload = {
        case_id: id,
        affected_ca_numbers: splitCa(affectedCaNumbers.value),
        oms_etr: isoOrNull(omsEtr.value)
      };
      let url = "/cases/open";
      let method = "POST";
      if (action === "update") {
        url = `/cases/${encodeURIComponent(id)}`;
        method = "PATCH";
      }
      if (action === "close") {
        url = `/cases/${encodeURIComponent(id)}/close`;
        method = "POST";
      }
      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      show(data);
    }

    document.getElementById("openCase").addEventListener("click", () => submit("open"));
    document.getElementById("updateCase").addEventListener("click", () => submit("update"));
    document.getElementById("closeCase").addEventListener("click", () => submit("close"));
    loadCustomers().catch((error) => show({ error: error.message }));
  </script>
</body>
</html>
        """
    )


@app.get("/customers")
def list_customers(q: str | None = None, limit: int = Query(default=1000, ge=1, le=20000)):
    query = (q or "").strip().lower()
    customers = sorted(CUSTOMERS.values(), key=lambda item: item["ca_number"])
    if query:
        customers = [
            customer
            for customer in customers
            if query in customer["ca_number"]
            or query in (customer.get("fullname") or "").lower()
        ]
    return {"total": len(customers), "customers": customers[:limit]}


@app.get("/customers/nearby")
def nearby_customers(
    ca_number: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = Query(default=CASE_LINK_RADIUS_KM, gt=0, le=50),
):
    if ca_number:
        center = customer_or_404(ca_number)
        lat = center["lat"]
        lon = center["lon"]
    elif lat is None or lon is None:
        raise HTTPException(status_code=400, detail="ca_number or lat/lon is required")

    matches = []
    for customer in CUSTOMERS.values():
        distance = calculate_distance_km(lat, lon, customer["lat"], customer["lon"])
        if distance <= radius_km:
            matches.append({**customer, "distance_km": distance})
    matches.sort(key=lambda item: (item["distance_km"], item["ca_number"]))
    return {
        "center": {"lat": lat, "lon": lon, "ca_number": clean_ca(ca_number)},
        "radius_km": radius_km,
        "customers": matches,
    }


@app.get("/customers/{ca_number}")
def get_customer(ca_number: str):
    return customer_or_404(ca_number)


@app.post("/cases/report")
def report_case(_payload: ReportRequest):
    raise HTTPException(
        status_code=410,
        detail="Django handles single-CA report flow; OMS no longer auto-merges reports.",
    )


@app.post("/api/v1/oms/outage/sync")
def sync_outage_event(
    payload: OutageSyncRequest,
    authorization: str | None = Header(default=None),
):
    validate_bearer_token(authorization)
    affected_ca_numbers = normalize_ca_list(payload.caList)
    if not affected_ca_numbers:
        raise HTTPException(status_code=400, detail="caList must contain valid CA numbers")

    case_status = api_status_to_case_status(payload.status)
    case_payload = build_case_payload(
        external_event_id=payload.eventId,
        affected_ca_numbers=affected_ca_numbers,
        outage_time=payload.outageTime,
        oms_etr=payload.etr,
        status=case_status,
    )
    event_type = "case_closed" if case_status == STATUS_RESTORED else "case_opened"
    django_event(event_type, case_payload)
    return spec_response(payload.eventId, datetime.now(timezone.utc))


@app.post("/cases/open")
def open_case(payload: CaseMutationRequest):
    affected_ca_numbers = normalize_ca_list(payload.affected_ca_numbers)
    if not affected_ca_numbers:
        raise HTTPException(status_code=400, detail="affected_ca_numbers is required")
    for ca_number in affected_ca_numbers:
        customer_or_404(ca_number)

    case_id = payload.case_id or str(uuid.uuid4())
    case_payload = build_case_payload(
        case_id=case_id,
        affected_ca_numbers=affected_ca_numbers,
        oms_etr=payload.oms_etr,
        status=STATUS_REPORTED,
    )
    return proxy_case_event("case_opened", case_payload)


@app.patch("/cases/{case_id}")
def update_case(case_id: str, payload: CasePatchRequest):
    case_payload = build_case_payload(
        case_id=case_id,
        affected_ca_numbers=payload.affected_ca_numbers,
        oms_etr=payload.oms_etr,
        status=STATUS_REPORTED,
    )
    if payload.affected_ca_numbers is None:
        case_payload.pop("affected_ca_numbers", None)
        case_payload.pop("case_type", None)
        case_payload.pop("anchor_ca_number", None)
    if "oms_etr" not in provided_fields(payload):
        case_payload.pop("oms_etr", None)
        case_payload.pop("etr_target_time", None)
        case_payload.pop("etr_source", None)
    return proxy_case_event("etr_updated", case_payload)


@app.post("/cases/{case_id}/close")
def close_case(case_id: str):
    case_payload = build_case_payload(case_id=case_id, status=STATUS_RESTORED)
    case_payload.pop("affected_ca_numbers", None)
    case_payload.pop("case_type", None)
    case_payload.pop("anchor_ca_number", None)
    case_payload.pop("oms_etr", None)
    case_payload.pop("etr_target_time", None)
    case_payload.pop("etr_source", None)
    return proxy_case_event("case_closed", case_payload)
