"""Opt-in real MongoDB + HTTP tests, using only application dependencies.

Set RUN_MONGO_TESTS=1 and optionally MONGO_TEST_URI. These tests create and drop
only a randomly named attendance_test_<uuid> database. They never seed MONGO_DB.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from pymongo import MongoClient


def epoch(local):
    return int(datetime.fromisoformat(local + "+05:30").timestamp()) * 1000


def employee(code, department="Engineering", joined="2026-07-01", night=False):
    return {
        "emp_code": code, "name": code, "email": f"{code}@example.com",
        "department": department, "joined_on": joined,
        "shift_start": "22:00" if night else "09:30",
        "shift_end": "06:00" if night else "18:30",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def log(code, day, status="PRESENT", hours=8.0, late=0, half=False, overtime=0):
    # Deliberately store values not derivable from these punch times: analytics
    # must use the stored metrics, not silently recalculate historical records.
    presence = status in ("PRESENT", "WFH", "ON_DUTY")
    return {
        "emp_code": code, "date": day, "status": status,
        "punch_in": datetime.fromtimestamp(epoch(day + "T09:30:00") / 1000, timezone.utc)
        if presence else None,
        "punch_out": datetime.fromtimestamp(epoch(day + "T18:30:00") / 1000, timezone.utc)
        if presence and hours is not None else None,
        "work_hours": hours if presence else None,
        "late_minutes": late, "overtime_minutes": overtime,
        "half_day": half, "history": [],
    }


@unittest.skipUnless(os.getenv("RUN_MONGO_TESTS") == "1", "set RUN_MONGO_TESTS=1 for real MongoDB/HTTP tests")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        uri = os.getenv("MONGO_TEST_URI", "mongodb://127.0.0.1:27017")
        cls.mongo = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2000)
        cls.addClassCleanup(cls.mongo.close)
        cls.mongo.admin.command("ping")  # Opting in without MongoDB is a failure, not a pass.
        if cls.mongo.server_info()["versionArray"][0] < 6:
            raise RuntimeError("MongoDB 6.0 or newer is required")
        cls.database = cls.mongo["attendance_test_" + uuid4().hex]
        cls.addClassCleanup(cls.mongo.drop_database, cls.database.name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        env = os.environ | {"MONGO_URI": uri, "MONGO_DB": cls.database.name}
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "error"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError("API exited during startup; run uvicorn directly to diagnose")
            try:
                if cls.request("GET", "/health")[0] == 200:
                    return
            except (URLError, TimeoutError):
                pass
            time.sleep(0.1)
        raise RuntimeError("API did not become ready within 20 seconds")

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)

    @classmethod
    def request(cls, method, path, body=None, **query):
        if query:
            path += "?" + urlencode(query)
        request = Request(cls.base + path, method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"unparsed_response": raw}
            return response.status, payload

    def get(self, path, **query):
        status, payload = self.request("GET", path, **query)
        self.assertEqual(status, 200, payload)
        return payload

    def setUp(self):
        self.database.employees.delete_many({})
        self.database.attendance_logs.delete_many({})
        self.database.employees.insert_many([
            employee("EMP0001"), employee("EMP0002", joined="2026-07-13"),
            employee("EMP0003"), employee("EMP0004", joined="2026-08-01"),
            employee("EMP0005", "Sales"), employee("EMP0006"),
            employee("EMP0007", "Support", night=True),
            employee("EMP0008", "NewDept", joined="2026-07-15"),
        ])
        legacy = log("EMP0006", "2026-07-07", hours=None, late=10)
        del legacy["half_day"], legacy["history"]
        self.database.attendance_logs.insert_many([
            log("EMP0001", "2026-07-06", late=10),
            log("EMP0001", "2026-07-07", "WFH", 3.5, half=True),
            log("EMP0001", "2026-07-08", "LEAVE"),
            log("EMP0001", "2026-07-09", "ABSENT"),
            log("EMP0001", "2026-07-11", hours=9.0, late=20, overtime=30),
            log("EMP0002", "2026-07-14", "ON_DUTY", 5.0, late=10),
            log("EMP0005", "2026-07-06", late=20),
            log("EMP0006", "2026-07-06", hours=4.0, late=10), legacy,
        ])

    def test_filtered_pagination_and_legacy_defaults(self):
        page = self.get("/employees", department="Engineering", page=2, page_size=2)
        self.assertEqual(page["total"], 5)
        self.assertEqual([row["emp_code"] for row in page["items"]], ["EMP0003", "EMP0004"])
        self.assertEqual(self.get("/employees", department="engineering")["total"], 0)
        page = self.get("/attendance", page_size=2)
        self.assertEqual(page["total"], 9)
        self.assertEqual([row["date"] for row in page["items"]], ["2026-07-14", "2026-07-11"])
        record = self.get("/attendance", emp_code="EMP0006", date_from="2026-07-07")["items"][0]
        self.assertEqual(record["history"], [])
        self.assertFalse(record["half_day"])
        self.assertNotIn("_id", record)
        self.assertIsInstance(record["punch_in"], int)

    def test_monthly_stored_metrics_weekends_and_joiners(self):
        summary = self.get("/analytics/employees/EMP0001/monthly", month="2026-07")
        self.assertEqual(summary, {
            "emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
            "present_days": 1.5, "leave_days": 1, "late_count": 2,
            "total_late_minutes": 30, "total_overtime_minutes": 30, "attendance_pct": 6.52,
        })
        self.assertEqual(self.get("/analytics/employees/EMP0002/monthly", month="2026-07")["working_days"], 15)
        self.assertEqual(self.get("/analytics/employees/EMP0003/monthly", month="2026-07")["present_days"], 0)
        future = self.get("/analytics/employees/EMP0004/monthly", month="2026-07")
        self.assertEqual(future["working_days"], 0)
        self.assertIsNone(future["attendance_pct"])

    def test_department_record_average_and_no_log_headcount(self):
        items = self.get("/analytics/departments/summary", month="2026-07", department="Engineering")["items"]
        self.assertEqual(items, [{
            "department": "Engineering", "headcount": 4, "present_days": 4.5,
            "avg_work_hours": 5.9, "late_count": 5, "total_late_minutes": 60,
            "leave_count": 1, "on_duty_count": 1,
        }])
        self.assertEqual(self.get("/analytics/departments/summary", month="2026-06", department="Engineering")["items"], [])

    def test_competition_ranking_and_ties_at_cutoff(self):
        items = self.get("/analytics/leaderboard/late", month="2026-07", limit=2)["items"]
        self.assertEqual([(row["emp_code"], row["rank"]) for row in items],
                         [("EMP0001", 1), ("EMP0005", 2), ("EMP0006", 2)])
        items = self.get("/analytics/leaderboard/late", month="2026-07", limit=4)["items"]
        self.assertEqual([row["rank"] for row in items], [1, 2, 2, 4])
        items = self.get("/analytics/leaderboard/late", month="2026-07", limit=2, department="Engineering")["items"]
        self.assertEqual([row["emp_code"] for row in items], ["EMP0001", "EMP0006"])

    def test_trend_gaps_weekends_headcount_and_window(self):
        items = self.get("/analytics/departments/Engineering/trend", **{
            "from": "2026-07-05", "to": "2026-07-14",
        })["items"]
        self.assertEqual(len(items), 10)
        by_date = {row["date"]: row for row in items}
        self.assertEqual(list(by_date), [f"2026-07-{day:02}" for day in range(5, 15)])
        self.assertIsNone(items[0]["attendance_rate"])
        self.assertIsNone(items[0]["moving_avg_7d"])
        self.assertEqual(by_date["2026-07-06"]["headcount"], 3)
        self.assertEqual(by_date["2026-07-06"]["attendance_rate"], 0.6667)
        self.assertEqual(by_date["2026-07-07"]["present_count"], 1.5)
        self.assertEqual(by_date["2026-07-10"]["attendance_rate"], 0)
        self.assertEqual(by_date["2026-07-11"]["present_count"], 1)
        self.assertIsNone(by_date["2026-07-11"]["attendance_rate"])
        self.assertEqual(by_date["2026-07-11"]["moving_avg_7d"], 0.2333)
        self.assertEqual(by_date["2026-07-13"]["headcount"], 4)
        self.assertEqual(by_date["2026-07-13"]["moving_avg_7d"], 0.1)
        self.assertEqual(by_date["2026-07-14"]["moving_avg_7d"], 0.05)
        empty = self.get("/analytics/departments/NewDept/trend", **{
            "from": "2026-07-12", "to": "2026-07-15",
        })["items"]
        self.assertEqual([row["headcount"] for row in empty], [0, 0, 0, 1])
        self.assertTrue(all(row["attendance_rate"] is None for row in empty[:3]))
        self.assertEqual(empty[-1]["attendance_rate"], 0)

    def test_supported_calendar_upper_boundary(self):
        self.get("/analytics/employees/EMP0001/monthly", month="9999-12")
        items = self.get("/analytics/departments/NewDept/trend", **{
            "from": "9999-12-31", "to": "9999-12-31",
        })["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "9999-12-31")

    def race(self, method, path, bodies):
        barrier = Barrier(len(bodies))

        def send(body):
            barrier.wait(timeout=10)
            return self.request(method, path, body)

        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            return list(pool.map(send, bodies))

    def test_concurrent_employee_creation_has_one_winner(self):
        body = employee("EMP0009")
        del body["created_at"]
        results = self.race("POST", "/employees", [body] * 6)
        self.assertEqual(sorted(code for code, _ in results), [201, 409, 409, 409, 409, 409])
        created = next(payload for code, payload in results if code == 201)
        self.assertNotIn("_id", created)
        self.assertIsInstance(created["created_at"], int)
        self.assertEqual(self.database.employees.count_documents({"emp_code": "EMP0009"}), 1)

    def test_concurrent_overnight_punches(self):
        body = {"emp_code": "EMP0007", "punched_at": epoch("2026-07-21T00:30:00") + 999}
        results = self.race("POST", "/attendance/punch-in", [body] * 6)
        self.assertEqual(sorted(code for code, _ in results), [201, 409, 409, 409, 409, 409])
        opened = next(payload for code, payload in results if code == 201)
        self.assertEqual(opened["date"], "2026-07-20")
        self.assertEqual(opened["punch_in"] % 1000, 0)
        self.assertEqual(opened["late_minutes"], 150)
        self.assertEqual(opened["history"], [])
        out = {"emp_code": "EMP0007", "punched_at": epoch("2026-07-21T06:30:00")}
        results = self.race("POST", "/attendance/punch-out", [out] * 6)
        self.assertEqual(sorted(code for code, _ in results), [200, 409, 409, 409, 409, 409])
        closed = next(payload for code, payload in results if code == 200)
        self.assertEqual(closed["work_hours"], 6)
        self.assertEqual(closed["overtime_minutes"], 30)
        self.assertFalse(closed["half_day"])
        self.assertEqual(closed["history"], [])

    def test_punch_out_interval_validation(self):
        start = epoch("2026-07-20T09:30:00")
        status, payload = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0003", "punched_at": start,
        })
        self.assertEqual(status, 201, payload)
        for invalid in (start, start + 24 * 3600 * 1000 + 1000):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.request("POST", "/attendance/punch-out", {
                    "emp_code": "EMP0003", "punched_at": invalid,
                })[0], 422)
        status, payload = self.request("POST", "/attendance/punch-out", {
            "emp_code": "EMP0003", "punched_at": start + 24 * 3600 * 1000,
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["work_hours"], 24)

    def test_regularization_clears_times_and_appends_audit(self):
        path = "/attendance/EMP0001/2026-07-06"
        body = {"status": "LEAVE", "reason": "Approved leave correction", "regularized_by": "hr"}
        status, corrected = self.request("PATCH", path, body)
        self.assertEqual(status, 200, corrected)
        for field in ("punch_in", "punch_out", "work_hours"):
            self.assertIsNone(corrected[field])
        self.assertEqual(corrected["late_minutes"], 0)
        self.assertEqual(len(corrected["history"]), 1)
        entry = corrected["history"][0]
        self.assertEqual(entry["by"], "hr")
        self.assertIsInstance(entry["at"], int)
        self.assertIsInstance(entry["changes"]["punch_in"]["from"], int)
        self.assertNotIn("half_day", entry["changes"])
        self.assertEqual(self.request("PATCH", path, body)[0], 422)
        self.assertEqual(self.request("PATCH", path, body | {
            "punch_in": epoch("2026-07-06T09:30:00"),
        })[0], 422)
        status, corrected = self.request("PATCH", path, {
            "status": "WFH", "punch_in": epoch("2026-07-06T09:28:00"),
            "punch_out": epoch("2026-07-06T13:00:00"),
            "reason": "Restore corrected work times", "regularized_by": "hr",
        })
        self.assertEqual(status, 200, corrected)
        self.assertEqual(corrected["work_hours"], 3.53)
        self.assertTrue(corrected["half_day"])
        self.assertEqual(len(corrected["history"]), 2)
        stored = self.database.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
        self.assertIsInstance(stored["history"][1]["changes"]["punch_in"]["to"], datetime)

    def test_concurrent_corrections_never_lose_history(self):
        bodies = [{
            "punch_out": epoch(f"2026-07-06T18:{31 + number:02}:00"),
            "reason": "Correct a clock reading", "regularized_by": f"editor-{number}",
        } for number in range(6)]
        results = self.race("PATCH", "/attendance/EMP0001/2026-07-06", bodies)
        self.assertTrue(all(code in (200, 409) for code, _ in results), results)
        successes = [payload for code, payload in results if code == 200]
        self.assertGreaterEqual(len(successes), 1)
        record = self.get("/attendance", emp_code="EMP0001", date_from="2026-07-06",
                          date_to="2026-07-06")["items"][0]
        self.assertEqual(len(record["history"]), len(successes))
        self.assertEqual(len({entry["by"] for entry in record["history"]}), len(successes))
        for corrected in successes:
            last = corrected["history"][-1]
            self.assertEqual(corrected["punch_out"], last["changes"]["punch_out"]["to"])
        for before, after in zip(record["history"], record["history"][1:]):
            self.assertEqual(before["changes"]["punch_out"]["to"], after["changes"]["punch_out"]["from"])

    def test_invalid_queries_and_missing_resources(self):
        cases = [
            ("/attendance", {"page": 0}, 422), ("/employees", {"page_size": 101}, 422),
            ("/attendance", {"date_from": "2026-07-02", "date_to": "2026-07-01"}, 422),
            ("/attendance", {"date_from": "2026-02-30"}, 422),
            ("/analytics/departments/summary", {"month": "2026-13"}, 422),
            ("/analytics/employees/EMP9999/monthly", {"month": "2026-07"}, 404),
            ("/analytics/departments/Unknown/trend", {"from": "2026-07-01", "to": "2026-07-01"}, 404),
            ("/analytics/departments/Engineering/trend", {"from": "2026-07-02", "to": "2026-07-01"}, 422),
            ("/analytics/departments/Engineering/trend", {"from": "2026-01-01", "to": "2026-04-03"}, 422),
            ("/admin/explain/employee_monthly", {"month": "2026-07"}, 422),
        ]
        for path, query, expected in cases:
            with self.subTest(path=path, query=query):
                status, payload = self.request("GET", path, **query)
                self.assertEqual(status, expected, payload)
                self.assertIn("detail", payload)

    def test_real_explain_plans_use_indexes(self):
        cases = [
            ("attendance_list", {"date_from": "2026-07-01", "status": "PRESENT", "page": 2, "page_size": 2}),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 2}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-05", "to": "2026-07-14"}),
        ]

        def stages(value):
            if isinstance(value, dict):
                if "stage" in value:
                    yield value["stage"]
                for child in value.values():
                    yield from stages(child)
            elif isinstance(value, list):
                for child in value:
                    yield from stages(child)

        for endpoint, query in cases:
            with self.subTest(endpoint=endpoint):
                plan = self.get("/admin/explain/" + endpoint, **query)
                self.assertEqual(plan["endpoint"], endpoint)
                seen = set(stages(plan["explain"]))
                self.assertIn("IXSCAN", seen)
                self.assertNotIn("COLLSCAN", seen)


if __name__ == "__main__":
    unittest.main()
