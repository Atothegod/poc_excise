# Service APIs and Webhooks

เอกสารนี้ list endpoint/webhook แยกตาม service และอธิบายว่าใครเรียกใคร

## backend: Django

Base URL:

- Internal Docker: `http://backend:8000`
- Local host: `http://localhost:8000`

### Public/Browser Pages

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | Browser | Login page |
| `GET` | `/chat/` | Browser | Chat UI |
| `GET` | `/ops/webhook/` | Operator | Ops webhook/case console |
| `GET` | `/ops/map/` | Operator | Ops map |
| `GET` | `/admin/` | Admin | Django admin |

### Django APIs Used by UI/Agent

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/reports/validate-ca/?ca_number=...` | Login UI | Validate CA from Django `CustomerLocation` |
| `POST` | `/api/reports/session-login/` | Login UI | Register session CA + PDPA consent |
| `POST` | `/api/reports/sync/` | `dspy-agent` tool | Create/reuse single-CA outage case in Django |
| `POST` | `/api/reports/chat-history/` | `dspy-agent` | Persist normalized chat history |
| `GET` | `/api/reports/session-context/{session_id}/` | `dspy-agent` | Restore chat/session/latest outage context |
| `GET` | `/api/reports/status/?ca_number=...` | Agent/UI | Return active case status for CA |

### Django OMS Event Ingestion

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/oms/events/` | `oms_api` | Authoritative open/update/close event from OMS |

Payload shape:

```json
{
  "event_type": "case_opened | etr_updated | case_closed",
  "case": {
    "case_id": "11111111-1111-1111-1111-111111111111",
    "external_event_id": "PEA-OUTAGE-001",
    "status": "reported | restored",
    "case_type": "normal | mass_outage",
    "affected_ca_numbers": ["123456789012", "123456789013"],
    "outage_time": "2026-07-09T08:30:00+07:00",
    "oms_etr": "2026-07-09T10:30:00+07:00"
  }
}
```

Notes:

- `case_id` is optional if `external_event_id` is provided.
- Django upserts by `external_event_id` first, then `case_id`.
- `case_opened` with `mass_outage` attaches active reports and sends mass outage proactive alerts.
- `etr_updated` and `case_closed` use Django `OutageCase.save()` so signals, Celery and webhooks still run.

### Django Ops APIs

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `GET` | `/ops/map/data/` | Ops map page | Marker/case data for Django ops map |
| `GET` | `/ops/cases/` | Ops page | List cases |
| `GET` | `/ops/cases/export/` | Ops page | Export cases as CSV |
| `POST` | `/ops/cases/action/` | Ops page | Set ETR, set ETA now, close selected cases |

### Django Proxies to dspy-agent

| Method | Path | Proxies To | Purpose |
| --- | --- | --- | --- |
| `POST` | `/agent/ask/` | `dspy-agent /ask` | Browser asks chatbot |
| `GET` | `/agent/notifications/{session_id}/` | `dspy-agent /notifications/{session_id}` | Fetch pending proactive notifications |
| `POST` | `/agent/notifications/{session_id}/ack/` | `dspy-agent /notifications/{session_id}/ack` | Acknowledge notifications |
| `GET` | `/agent/notifications/{session_id}/latest-closed-loop/` | `dspy-agent /notifications/{session_id}/latest-closed-loop` | Get latest closed-loop prompt |

## oms: FastAPI `oms_api`

Base URL:

- Internal Docker: `http://oms:8000`
- Local host: `http://localhost:8002`

Important env:

- `DJANGO_OMS_EVENT_URL=http://backend:8000/api/oms/events/`
- `OMS_API_TOKEN` optional; if set, `/api/v1/oms/outage/sync` requires `Authorization: Bearer <token>`
- `CA_CSV_PATH=/app/ca_lat_lon_2.csv`

### OMS UI and Customer Data

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | Ops/monitoring | Health, customer count, config summary |
| `GET` | `/` | Browser | Same as OMS UI |
| `GET` | `/ui` | Operator | Map radius-selection UI plus status dropdown for group case open/update/close |
| `GET` | `/customers` | OMS UI | List all CA data from mounted CSV by default |
| `GET` | `/customers/{ca_number}` | OMS UI/API | Validate or fetch CA from CSV |

### OMS Case/Event APIs

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `POST` | `/cases/open` | OMS UI/operator | Build `case_opened` event and POST to Django |
| `PATCH` | `/cases/{case_id}` | OMS UI/operator | Build `etr_updated` event and POST to Django |
| `POST` | `/cases/{case_id}/close` | OMS UI/operator | Build `case_closed` event and POST to Django |
| `POST` | `/api/v1/oms/outage/sync` | External OMS/PEA | API-spec sync adapter, maps external event to Django OMS event |
| `POST` | `/cases/report` | Deprecated/blocked | Returns `410 Gone`; Django owns single-CA report flow |

`POST /api/v1/oms/outage/sync` request:

```json
{
  "eventId": "PEA-OUTAGE-20260709-001",
  "caList": ["123456789012", "123456789013"],
  "outageTime": "2026-07-09T08:30:00+07:00",
  "etr": "2026-07-09T10:30:00+07:00",
  "status": "OPEN"
}
```

Response:

```json
{
  "statusCode": 200,
  "message": "Outage event synced successfully",
  "data": {
    "eventId": "PEA-OUTAGE-20260709-001",
    "updatedAt": "2026-07-09T..."
  }
}
```

Mapping:

| Spec Field | Django Event Field |
| --- | --- |
| `eventId` | `external_event_id` |
| `caList` | `affected_ca_numbers` |
| `outageTime` | `outage_time` |
| `etr` | `oms_etr` |
| `OPEN` | `status=reported`, `event_type=case_opened` |
| `CLOSED` | `status=restored`, `event_type=case_closed` |

## dspy-agent: FastAPI

Base URL:

- Internal Docker: `http://dspy-agent:8000`
- Local host: `http://localhost:8001`

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `POST` | `/ask` | Django proxy | Run DSPy chatbot with session, CA and PDPA context |
| `POST` | `/webhook/notify` | Django Celery/tasks/signals | Receive proactive system notification |
| `GET` | `/notifications/{session_id}` | Django proxy/UI | Return pending notifications |
| `GET` | `/notifications/{session_id}/latest-closed-loop` | Django proxy/UI | Return latest closed-loop prompt |
| `POST` | `/notifications/{session_id}/ack` | Django proxy/UI | Ack pending notifications |
| `GET` | `/health` | Monitoring | Health check |

## Service-to-Service Calls

| From | To | API/Webhook | When |
| --- | --- | --- | --- |
| Browser | Django | `/agent/ask/` | Customer sends chat message |
| Django | dspy-agent | `/ask` | Django proxies chat to agent |
| dspy-agent | Django | `/api/reports/sync/` | Agent calls outage tool |
| dspy-agent | Django | `/api/reports/session-context/{session_id}/` | Agent restores context |
| dspy-agent | Django | `/api/reports/chat-history/` | Agent persists chat history |
| Celery/Django signals | dspy-agent | `/webhook/notify` | ETA timeout, ETR update, closed-loop prompt |
| Browser | dspy-agent via Django proxy | `/notifications/...` | Chat UI polls/acks alerts |
| OMS UI | oms_api | `/cases/open`, `/cases/{case_id}`, `/cases/{case_id}/close` | Operator manages group case |
| External OMS/PEA | oms_api | `/api/v1/oms/outage/sync` | External outage sync |
| oms_api | Django | `/api/oms/events/` | Open/update/close authoritative Django case |
