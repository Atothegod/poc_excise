import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:////tmp/oms_api_tests.sqlite3"
os.environ["DJANGO_OMS_EVENT_URL"] = ""

from fastapi.testclient import TestClient

from app import Base, OmsCase, SessionLocal, app, engine, load_customers


class OmsApiTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        load_customers()
        self.client = TestClient(app)

    def test_customer_lookup_uses_csv(self):
        response = self.client.get("/customers/123456789012")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ca_number"], "123456789012")

    def test_nearby_customers_share_case_and_promote_to_mass_outage(self):
        first = self.client.post("/cases/report", json={"ca_number": "123456789012"})
        second = self.client.post("/cases/report", json={"ca_number": "123456789013"})
        third = self.client.post("/cases/report", json={"ca_number": "123456789014"})

        self.assertEqual(first.json()["event_type"], "new_event")
        self.assertEqual(second.json()["event_type"], "existing_ca_case")
        self.assertEqual(third.json()["event_type"], "mass_outage")
        self.assertTrue(third.json()["newly_promoted"])
        self.assertEqual(first.json()["case_id"], second.json()["case_id"])
        self.assertEqual(first.json()["case_id"], third.json()["case_id"])
        self.assertEqual(set(third.json()["affected_ca_numbers"]), {
            "123456789012",
            "123456789013",
            "123456789014",
        })

    def test_update_etr_and_close_case(self):
        opened = self.client.post("/cases/report", json={"ca_number": "123456789012"})
        case_id = opened.json()["case_id"]
        etr = "2026-07-08T10:30:00+00:00"

        updated = self.client.patch(f"/cases/{case_id}", json={"oms_etr": etr})
        closed = self.client.post(f"/cases/{case_id}/close")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["oms_etr"], etr)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()["status"], "restored")

        with SessionLocal() as db:
            case = db.get(OmsCase, case_id)
            self.assertEqual(case.status, "restored")


if __name__ == "__main__":
    unittest.main()
