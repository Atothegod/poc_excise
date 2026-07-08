import csv
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Column, DateTime, Float, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://myuser:mypassword@db:5432/mydb",
)
CA_CSV_PATH = Path(os.getenv("CA_CSV_PATH", "/app/ca_lat_lon_2.csv"))
DJANGO_OMS_EVENT_URL = os.getenv("DJANGO_OMS_EVENT_URL", "")
CASE_LINK_RADIUS_KM = float(os.getenv("CASE_LINK_RADIUS_KM", "0.5"))
MASS_OUTAGE_CONFIRMATION_COUNT = int(
    os.getenv("MASS_OUTAGE_CONFIRMATION_COUNT", "3")
)

STATUS_REPORTED = "reported"
STATUS_RESTORED = "restored"
CASE_TYPE_NORMAL = "normal"
CASE_TYPE_MASS_OUTAGE = "mass_outage"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
CUSTOMERS: dict[str, dict[str, Any]] = {}


class OmsCase(Base):
    __tablename__ = "oms_cases"

    case_id = Column(String(36), primary_key=True)
    status = Column(String(20), index=True, nullable=False, default=STATUS_REPORTED)
    case_type = Column(String(20), index=True, nullable=False, default=CASE_TYPE_NORMAL)
    anchor_ca_number = Column(String(12), nullable=True)
    anchor_latitude = Column(Float, nullable=True)
    anchor_longitude = Column(Float, nullable=True)
    affected_ca_numbers = Column(JSON, nullable=False, default=list)
    oms_etr = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


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


app = FastAPI(title="PEA OMS API")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
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


def active_cases(db: Session):
    return (
        db.query(OmsCase)
        .filter(OmsCase.status != STATUS_RESTORED)
        .order_by(OmsCase.created_at, OmsCase.case_id)
        .all()
    )


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


def case_payload(case: OmsCase, event_type: str | None = None, **extra):
    payload = {
        "case_id": case.case_id,
        "status": case.status,
        "case_type": case.case_type,
        "affected_ca_numbers": case.affected_ca_numbers or [],
        "oms_etr": serialize_datetime(case.oms_etr),
        "etr_target_time": serialize_datetime(case.oms_etr),
        "etr_source": "oms" if case.oms_etr else None,
        "anchor_ca_number": case.anchor_ca_number,
        "created_at": serialize_datetime(case.created_at),
        "updated_at": serialize_datetime(case.updated_at),
    }
    if event_type:
        payload["event_type"] = event_type
    payload.update(extra)
    payload["case"] = {
        key: payload[key]
        for key in [
            "case_id",
            "status",
            "case_type",
            "affected_ca_numbers",
            "oms_etr",
            "etr_target_time",
            "etr_source",
            "anchor_ca_number",
            "created_at",
            "updated_at",
        ]
    }
    return payload


def serialize_datetime(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def find_case_for_ca(db: Session, ca_number: str):
    for case in active_cases(db):
        if ca_number in (case.affected_ca_numbers or []):
            return case
    return None


def nearest_case(db: Session, customer, case_type=None):
    matches = []
    for case in active_cases(db):
        if case_type and case.case_type != case_type:
            continue
        distance = calculate_distance_km(
            customer["lat"],
            customer["lon"],
            case.anchor_latitude,
            case.anchor_longitude,
        )
        if distance <= CASE_LINK_RADIUS_KM:
            matches.append((distance, case.created_at or datetime.now(timezone.utc), case))
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item[0], item[1], item[2].case_id))[0][2]


def add_ca_to_case(case: OmsCase, ca_number: str):
    affected = normalize_ca_list([*(case.affected_ca_numbers or []), ca_number])
    case.affected_ca_numbers = affected
    if len(affected) >= MASS_OUTAGE_CONFIRMATION_COUNT:
        case.case_type = CASE_TYPE_MASS_OUTAGE
    case.updated_at = datetime.now(timezone.utc)
    return case


def notify_django(event_type: str, case: OmsCase):
    if not DJANGO_OMS_EVENT_URL:
        return
    try:
        requests.post(
            DJANGO_OMS_EVENT_URL,
            json={"event_type": event_type, "case": case_payload(case)["case"]},
            timeout=5,
        )
    except requests.exceptions.RequestException:
        pass


@app.get("/health")
def health():
    return {
        "status": "ok",
        "customers_loaded": len(CUSTOMERS),
        "case_link_radius_km": CASE_LINK_RADIUS_KM,
        "mass_outage_confirmation_count": MASS_OUTAGE_CONFIRMATION_COUNT,
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(
        """
<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OMS Case Console</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }
    main { max-width: 760px; margin: 48px auto; padding: 0 20px; }
    form { display: grid; gap: 16px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 24px; }
    label { display: grid; gap: 6px; font-weight: 600; }
    input, select, textarea, button { font: inherit; border-radius: 6px; border: 1px solid #cbd5e1; padding: 10px 12px; }
    textarea { min-height: 92px; resize: vertical; }
    button { color: #fff; background: #0f766e; border-color: #0f766e; cursor: pointer; font-weight: 700; }
    pre { white-space: pre-wrap; background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 16px; min-height: 120px; }
  </style>
</head>
<body>
  <main>
    <h1>OMS Case Console</h1>
    <form id="caseForm">
      <label>Case ID <input id="caseId" placeholder="blank for new case"></label>
      <label>Affected CA Numbers <textarea id="affectedCaNumbers" placeholder="123456789012, 123456789013"></textarea></label>
      <label>OMS ETR <input id="omsEtr" type="datetime-local"></label>
      <label>Status
        <select id="status">
          <option value="open_case">open_case</option>
          <option value="update_etr">update_etr</option>
          <option value="close_case">close_case</option>
        </select>
      </label>
      <button type="submit">Submit</button>
    </form>
    <h2>Result</h2>
    <pre id="result">Ready</pre>
  </main>
  <script>
    const result = document.getElementById("result");
    const splitCa = (value) => value.split(/[\\s,]+/).map((item) => item.trim()).filter(Boolean);
    const isoOrNull = (value) => value ? new Date(value).toISOString() : null;
    document.getElementById("caseForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const caseId = document.getElementById("caseId").value.trim();
      const status = document.getElementById("status").value;
      const payload = {
        affected_ca_numbers: splitCa(document.getElementById("affectedCaNumbers").value),
        oms_etr: isoOrNull(document.getElementById("omsEtr").value),
      };
      let url = "/cases/open";
      let method = "POST";
      if (status === "update_etr") {
        url = `/cases/${encodeURIComponent(caseId)}`;
        method = "PATCH";
      }
      if (status === "close_case") {
        url = `/cases/${encodeURIComponent(caseId)}/close`;
        method = "POST";
      }
      if (caseId && status === "open_case") payload.case_id = caseId;
      try {
        const response = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        result.textContent = JSON.stringify(data, null, 2);
      } catch (error) {
        result.textContent = error.message;
      }
    });
  </script>
</body>
</html>
        """
    )


@app.get("/customers/{ca_number}")
def get_customer(ca_number: str):
    return customer_or_404(ca_number)


@app.post("/cases/report")
def report_case(payload: ReportRequest, db: Session = Depends(get_db)):
    ca_number = clean_ca(payload.ca_number)
    customer = customer_or_404(ca_number)

    existing_case = find_case_for_ca(db, ca_number)
    if existing_case:
        event_type = (
            "mass_outage"
            if existing_case.case_type == CASE_TYPE_MASS_OUTAGE
            else "existing_ca_case"
        )
        return case_payload(existing_case, event_type=event_type, newly_promoted=False)

    mass_case = nearest_case(db, customer, case_type=CASE_TYPE_MASS_OUTAGE)
    if mass_case:
        add_ca_to_case(mass_case, ca_number)
        db.commit()
        db.refresh(mass_case)
        return case_payload(mass_case, event_type="mass_outage", newly_promoted=False)

    normal_case = nearest_case(db, customer, case_type=CASE_TYPE_NORMAL)
    if normal_case:
        was_mass_outage = normal_case.case_type == CASE_TYPE_MASS_OUTAGE
        add_ca_to_case(normal_case, ca_number)
        db.commit()
        db.refresh(normal_case)
        is_mass_outage = normal_case.case_type == CASE_TYPE_MASS_OUTAGE
        return case_payload(
            normal_case,
            event_type="mass_outage" if is_mass_outage else "existing_ca_case",
            newly_promoted=is_mass_outage and not was_mass_outage,
        )

    new_case = OmsCase(
        case_id=str(uuid.uuid4()),
        status=STATUS_REPORTED,
        case_type=CASE_TYPE_NORMAL,
        anchor_ca_number=ca_number,
        anchor_latitude=customer["lat"],
        anchor_longitude=customer["lon"],
        affected_ca_numbers=[ca_number],
    )
    db.add(new_case)
    db.commit()
    db.refresh(new_case)
    notify_django("case_opened", new_case)
    return case_payload(new_case, event_type="new_event", newly_promoted=False)


@app.post("/cases/open")
def open_case(payload: CaseMutationRequest, db: Session = Depends(get_db)):
    affected_ca_numbers = normalize_ca_list(payload.affected_ca_numbers)
    if not affected_ca_numbers:
        raise HTTPException(status_code=400, detail="affected_ca_numbers is required")

    anchor = customer_or_404(affected_ca_numbers[0])
    case_id = payload.case_id or str(uuid.uuid4())
    existing = db.get(OmsCase, case_id)
    if existing:
        case = existing
        case.status = STATUS_REPORTED
        case.affected_ca_numbers = affected_ca_numbers
        case.oms_etr = payload.oms_etr
    else:
        case = OmsCase(
            case_id=case_id,
            status=STATUS_REPORTED,
            anchor_ca_number=affected_ca_numbers[0],
            anchor_latitude=anchor["lat"],
            anchor_longitude=anchor["lon"],
            affected_ca_numbers=affected_ca_numbers,
            oms_etr=payload.oms_etr,
        )
        db.add(case)

    case.case_type = (
        CASE_TYPE_MASS_OUTAGE
        if len(affected_ca_numbers) >= MASS_OUTAGE_CONFIRMATION_COUNT
        else CASE_TYPE_NORMAL
    )
    case.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)
    notify_django("case_opened", case)
    if case.oms_etr:
        notify_django("etr_updated", case)
    return case_payload(case, event_type="case_opened")


@app.patch("/cases/{case_id}")
def update_case(case_id: str, payload: CasePatchRequest, db: Session = Depends(get_db)):
    case = db.get(OmsCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")

    old_etr = case.oms_etr
    if payload.affected_ca_numbers is not None:
        case.affected_ca_numbers = normalize_ca_list(payload.affected_ca_numbers)
        if len(case.affected_ca_numbers) >= MASS_OUTAGE_CONFIRMATION_COUNT:
            case.case_type = CASE_TYPE_MASS_OUTAGE
    if payload.oms_etr is not None:
        case.oms_etr = payload.oms_etr
    if payload.status:
        case.status = STATUS_RESTORED if payload.status == "close_case" else payload.status
    case.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)

    if case.status == STATUS_RESTORED:
        notify_django("case_closed", case)
    elif case.oms_etr and case.oms_etr != old_etr:
        notify_django("etr_updated", case)
    return case_payload(case, event_type="case_updated")


@app.post("/cases/{case_id}/close")
def close_case(case_id: str, db: Session = Depends(get_db)):
    case = db.get(OmsCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    case.status = STATUS_RESTORED
    case.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)
    notify_django("case_closed", case)
    return case_payload(case, event_type="case_closed")


@app.get("/cases")
def list_cases(db: Session = Depends(get_db)):
    cases = db.query(OmsCase).order_by(OmsCase.created_at.desc()).limit(500).all()
    return {"cases": [case_payload(case)["case"] for case in cases]}
