"""Pure rule/validation checks; no MongoDB server or extra test package needed."""

import unittest
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError

from app import main as api


def instant(local_time):
    return datetime.fromisoformat(local_time).replace(tzinfo=api.IST).astimezone(api.UTC)


class RuleTests(unittest.TestCase):
    def test_grace_is_strict_and_minutes_are_since_shift_start(self):
        for clock, expected in (("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45)):
            with self.subTest(clock=clock):
                self.assertEqual(api._compute_late_minutes(
                    instant(f"2026-07-06T{clock}"), "09:30", date(2026, 7, 6)
                ), expected)

    def test_overnight_date_boundary_is_exclusive(self):
        for clock, expected in (("05:59:59", "2026-07-06"), ("06:00:00", "2026-07-07")):
            with self.subTest(clock=clock):
                self.assertEqual(api._attendance_date(
                    instant(f"2026-07-07T{clock}"), "22:00", "06:00"
                ).isoformat(), expected)
        self.assertEqual(api._attendance_date(
            instant("2026-07-07T00:30:00"), "22:00", "06:00"
        ), date(2026, 7, 6))

    def test_overtime_minimum_and_overnight_end(self):
        for clock, expected in (("06:29:59", 0), ("06:30:00", 30), ("06:40:59", 40)):
            with self.subTest(clock=clock):
                self.assertEqual(api._compute_overtime_minutes(
                    instant(f"2026-07-07T{clock}"), "22:00", "06:00", date(2026, 7, 6)
                ), expected)

    def test_half_up_and_half_day_use_rounded_hours(self):
        start = instant("2026-07-06T09:30:00")
        self.assertEqual(api._rounded_work_hours(start, start + timedelta(seconds=3618)), 1.01)
        for seconds, hours, half_day in ((16181, 4.49, True), (16182, 4.50, False)):
            with self.subTest(seconds=seconds):
                derived = api._compute_derived(
                    status="WFH", punch_in=start, punch_out=start + timedelta(seconds=seconds),
                    employee={"shift_start": "09:30", "shift_end": "18:30"},
                    attendance_day=date(2026, 7, 6),
                )
                self.assertEqual(derived["work_hours"], hours)
                self.assertEqual(derived["half_day"], half_day)

    def test_milliseconds_truncate_and_naive_bson_is_utc(self):
        milliseconds = api._datetime_to_epoch_ms(instant("2026-07-06T09:40:00"))
        self.assertEqual(api._datetime_to_epoch_ms(api._epoch_ms_to_datetime(milliseconds + 999)),
                         milliseconds)
        self.assertEqual(api._datetime_to_epoch_ms(datetime(2026, 7, 6, 4, 10)), milliseconds)

    def test_timestamp_types_and_bounds(self):
        for value in (True, 1783311000, 1783311000000.0, "1783311000000", None,
                      api.MIN_EPOCH_MS - 1, api.MAX_EPOCH_MS + 1):
            for model in (api.PunchInIn, api.PunchOutIn):
                with self.subTest(model=model.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        model(emp_code="EMP0001", punched_at=value)
        for value in (api.MIN_EPOCH_MS, api.MAX_EPOCH_MS):
            self.assertEqual(api.PunchInIn(emp_code="EMP0001", punched_at=value).punched_at, value)
        self.assertIsNone(api.PunchOutIn(emp_code="EMP0001").punched_at)

    def test_employee_validation_and_read_only_fields(self):
        valid = dict(emp_code="EMP0001", name="Example", email="person@example.com",
                     department="Engineering", joined_on="2026-07-01")
        for changes in ({"emp_code": "0001"}, {"joined_on": "2026-02-29"},
                        {"shift_start": "24:00"}, {"shift_end": "09:30"},
                        {"email": "missing-at"}, {"name": ""}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    api.EmployeeIn(**(valid | changes))
        body = api.PunchInIn(emp_code="EMP0001", work_hours=99, late_minutes=999)
        self.assertNotIn("work_hours", body.model_dump())
        self.assertNotIn("late_minutes", body.model_dump())
        with self.assertRaises(ValidationError):
            api.PunchInIn(emp_code="EMP0001", status="ABSENT")

    def test_correction_null_is_not_an_omitted_field(self):
        for field in ("status", "punch_in", "punch_out"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    api.RegularizeIn(reason="Clock correction", regularized_by="hr", **{field: None})

    def test_calendar_validation_and_last_supported_month(self):
        self.assertEqual(api._parse_month("9999-12"), (date(9999, 12, 1), date(9999, 12, 31)))
        self.assertEqual(api._parse_month("2024-02")[1], date(2024, 2, 29))
        for value in ("2026-13", "0000-01", "2026-7"):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as error:
                    api._parse_month(value)
                self.assertEqual(error.exception.status_code, 422)
        with self.assertRaises(HTTPException):
            api._parse_date("2026-02-30")

    def test_legacy_defaults_and_nested_audit_instants(self):
        when = instant("2026-07-06T09:30:00")
        record = api._api_attendance({
            "_id": "internal", "emp_code": "EMP0001", "date": "2026-07-06",
            "status": "PRESENT", "punch_in": when,
        })
        self.assertNotIn("_id", record)
        self.assertEqual(record["history"], [])
        self.assertFalse(record["half_day"])
        self.assertIsNone(record["work_hours"])
        converted = api._api_value({"at": when, "changes": {
            "punch_in": {"from": None, "to": when}
        }})
        self.assertIsInstance(converted["at"], int)
        self.assertEqual(converted["changes"]["punch_in"]["to"], record["punch_in"])


if __name__ == "__main__":
    unittest.main()
