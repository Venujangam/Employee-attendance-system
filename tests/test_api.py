"""MongoDB-backed regression tests for the attendance API."""
from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import HTTPException
from pydantic import ValidationError
from pymongo import MongoClient

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
TEST_DB = "attest_" + uuid4().hex[:10]
probe = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500, tz_aware=True)
try:
    probe.admin.command("ping")
except Exception as exc:
    probe.close()
    raise unittest.SkipTest(f"MongoDB unavailable: {type(exc).__name__}") from exc
probe.close()

os.environ["MONGO_DB"] = TEST_DB
from app import main  # noqa: E402


IST = timezone(timedelta(hours=5, minutes=30))


def epoch_ms(hour: int, minute: int, second: int = 0, millisecond: int = 0, day: int = 6) -> int:
    value = datetime(2026, 7, day, hour, minute, second, millisecond * 1000, tzinfo=IST)
    return int(value.timestamp() * 1000)


def employee(code: str, department: str = "Engineering", joined_on: str = "2026-01-01", shift_start: str = "09:30", shift_end: str = "18:30") -> dict:
    return {
        "emp_code": code,
        "name": code,
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    }


def invoke_status(callable_, *args, **kwargs):
    try:
        callable_(*args, **kwargs)
    except HTTPException as exc:
        return exc.status_code
    return 200


class AttendanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = main.db
        main.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        main.client.drop_database(TEST_DB)
        main.client.close()

    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    def test_startup_indexes_include_unique_keys_and_query_indexes(self):
        employees = {index["name"]: index for index in self.db.employees.list_indexes()}
        attendance = {index["name"]: index for index in self.db.attendance_logs.list_indexes()}
        self.assertTrue(employees["idx_employees_emp_code"]["unique"])
        self.assertIn("idx_employees_department_emp_code", employees)
        self.assertTrue(attendance["idx_attendance_emp_date"]["unique"])
        self.assertIn("idx_attendance_date_emp", attendance)
        self.assertIn("idx_attendance_emp_punch_in", attendance)

    def add_employee(self, code: str, **kwargs):
        self.db.employees.insert_one(employee(code, **kwargs))

    def test_employee_validation_creation_listing_and_pagination(self):
        with self.assertRaises(ValidationError):
            main.EmployeeIn(
                emp_code="EMP1001", name="A", email="a@example.com", department="Sales", joined_on="20260701"
            )
        with self.assertRaises(ValidationError):
            main.EmployeeIn(
                emp_code="EMP1001", name="A", email="a@example.com", department="Sales", joined_on="2026-02-30"
            )

        for code, dept in (("EMP1001", "Sales"), ("EMP1002", "Sales"), ("EMP1003", "Support")):
            result = main.create_employee(main.EmployeeIn(
                emp_code=code,
                name=code,
                email=f"{code.lower()}@example.com",
                department=dept,
                joined_on="2026-01-01",
            ))
            self.assertIsInstance(result["created_at"], int)
            self.assertNotIn("_id", result)

        duplicate = invoke_status(
            main.create_employee,
            main.EmployeeIn(emp_code="EMP1001", name="Duplicate", email="d@example.com", department="Sales", joined_on="2026-01-01"),
        )
        self.assertEqual(duplicate, 409)
        first = main.list_employees(department="Sales", page=1, page_size=1)
        second = main.list_employees(department="Sales", page=2, page_size=1)
        self.assertEqual((first["total"], len(first["items"])), (2, 1))
        self.assertEqual((second["total"], second["items"][0]["emp_code"]), (2, "EMP1002"))

    def test_punch_status_errors_truncation_and_derived_boundaries(self):
        self.add_employee("EMP2001")
        missing = invoke_status(main.punch_in, main.PunchInIn(emp_code="EMP2999", punched_at=epoch_ms(9, 30)))
        self.assertEqual(missing, 404)
        for bad in (1, 4102444800001, 1783312500000.5, "1783312500000"):
            with self.assertRaises(ValidationError):
                main.PunchInIn(emp_code="EMP2001", punched_at=bad)
        with self.assertRaises(ValidationError):
            main.PunchInIn(emp_code="EMP2001", punched_at=None)
        with self.assertRaises(ValidationError):
            main.PunchOutIn(emp_code="EMP2001", punched_at=None)
        with self.assertRaises(ValidationError):
            main.RegularizeRequest(status=None, reason="valid reason", regularized_by="test")

        punched = main.punch_in(main.PunchInIn(emp_code="EMP2001", punched_at=epoch_ms(9, 40, 1, 900)))
        self.assertEqual(punched["punch_in"], epoch_ms(9, 40, 1))
        self.assertEqual(punched["late_minutes"], 10)
        self.assertEqual(punched["history"], [])
        duplicate = invoke_status(main.punch_in, main.PunchInIn(emp_code="EMP2001", punched_at=epoch_ms(10, 0)))
        self.assertEqual(duplicate, 409)
        before_punch_out = invoke_status(main.punch_out, main.PunchOutIn(emp_code="EMP2001", punched_at=epoch_ms(9, 40, 0)))
        self.assertEqual(before_punch_out, 422)
        equal_punch_out = invoke_status(main.punch_out, main.PunchOutIn(emp_code="EMP2001", punched_at=epoch_ms(9, 40, 1)))
        self.assertEqual(equal_punch_out, 422)
        too_late = invoke_status(main.punch_out, main.PunchOutIn(emp_code="EMP2001", punched_at=epoch_ms(10, 0, day=7)))
        self.assertEqual(too_late, 422)

        closed = main.punch_out(main.PunchOutIn(emp_code="EMP2001", punched_at=epoch_ms(19, 0)))
        self.assertEqual(closed["work_hours"], 9.33)
        self.assertEqual(closed["overtime_minutes"], 30)
        self.assertFalse(closed["half_day"])
        self.assertEqual(invoke_status(main.punch_out, main.PunchOutIn(emp_code="EMP2001", punched_at=epoch_ms(19, 1))), 409)

    def test_time_zone_late_overtime_rounding_and_half_day_edges(self):
        shift_start = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
        self.assertEqual(main.compute_late_minutes(shift_start + timedelta(minutes=10), "09:30", "18:30", "2026-07-06"), 0)
        self.assertEqual(main.compute_late_minutes(shift_start + timedelta(minutes=10, seconds=1), "09:30", "18:30", "2026-07-06"), 10)
        shift_end = datetime(2026, 7, 6, 18, 30, tzinfo=IST)
        self.assertEqual(main.compute_overtime(shift_end + timedelta(minutes=29, seconds=59), "09:30", "18:30", "2026-07-06"), 0)
        self.assertEqual(main.compute_overtime(shift_end + timedelta(minutes=30), "09:30", "18:30", "2026-07-06"), 30)
        overnight = datetime(2026, 7, 7, 5, 59, tzinfo=IST)
        self.assertEqual(main.record_date_for_punch(overnight, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(main.record_date_for_punch(datetime(2026, 7, 7, 6, 0, tzinfo=IST), "22:00", "06:00"), "2026-07-07")
        self.assertEqual(main.compute_work_hours(shift_start, shift_start + timedelta(seconds=3618)), 1.01)
        self.assertTrue(main.compute_half_day(4.49))
        self.assertFalse(main.compute_half_day(4.50))

    def test_overnight_punch_out_crosses_midnight(self):
        self.add_employee("EMP2501", department="Night", shift_start="22:00", shift_end="06:00")
        started = main.punch_in(main.PunchInIn(emp_code="EMP2501", punched_at=epoch_ms(22, 0)))
        self.assertEqual(started["date"], "2026-07-06")
        closed = main.punch_out(main.PunchOutIn(emp_code="EMP2501", punched_at=epoch_ms(6, 30, day=7)))
        self.assertEqual(closed["date"], "2026-07-06")
        self.assertEqual(closed["work_hours"], 8.5)
        self.assertEqual(closed["overtime_minutes"], 30)

    def test_overnight_regularization_history_and_validation(self):
        self.add_employee("EMP3001", department="Support", shift_start="22:00", shift_end="06:00")
        punched = main.punch_in(main.PunchInIn(emp_code="EMP3001", punched_at=epoch_ms(2, 0, day=7)))
        self.assertEqual(punched["date"], "2026-07-06")
        corrected_ms = epoch_ms(2, 5, day=7)
        body = main.RegularizeRequest(punch_in=corrected_ms, reason="correct overnight punch", regularized_by="test")
        corrected = main.regularize_attendance("EMP3001", date(2026, 7, 6), body)
        self.assertEqual(corrected["punch_in"], corrected_ms)
        self.assertEqual(len(corrected["history"]), 1)
        self.assertEqual(corrected["history"][0]["by"], "test")
        no_change = invoke_status(main.regularize_attendance, "EMP3001", date(2026, 7, 6), body)
        self.assertEqual(no_change, 422)
        conflict_body = main.RegularizeRequest(status="ABSENT", punch_in=corrected_ms, reason="invalid absence", regularized_by="test")
        self.assertEqual(invoke_status(main.regularize_attendance, "EMP3001", date(2026, 7, 6), conflict_body), 422)
        absent = main.regularize_attendance(
            "EMP3001", date(2026, 7, 6), main.RegularizeRequest(status="ABSENT", reason="mark absent", regularized_by="test")
        )
        self.assertIsNone(absent["punch_in"])
        self.assertIsNone(absent["punch_out"])
        self.assertEqual(len(absent["history"]), 2)
        self.assertEqual(invoke_status(main.regularize_attendance, "EMP3999", date(2026, 7, 6), body), 404)

        self.add_employee("EMP3002")
        self.db.attendance_logs.insert_one({
            "emp_code": "EMP3002", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc), "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        })
        seeded = main.regularize_attendance(
            "EMP3002", date(2026, 7, 6), main.RegularizeRequest(status="WFH", reason="seed correction", regularized_by="test")
        )
        self.assertEqual(len(seeded["history"]), 1)

    def test_attendance_filters_and_accurate_total(self):
        self.add_employee("EMP4001")
        for code, day, status in (("EMP4001", "2026-07-06", "PRESENT"), ("EMP4001", "2026-07-07", "LEAVE")):
            self.db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None, "history": []})
        result = main.list_attendance(emp_code="EMP4001", date_from=date(2026, 7, 6), date_to=date(2026, 7, 7), status="LEAVE", page=1, page_size=10)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["date"], "2026-07-07")
        self.assertEqual(result["items"][0]["history"], [])
        with self.assertRaises(HTTPException) as error:
            main.list_attendance(date_from=date(2026, 7, 8), date_to=date(2026, 7, 7), page=1, page_size=20)
        self.assertEqual(error.exception.status_code, 422)

    def test_analytics_empty_ranges_department_ties_and_trend(self):
        self.add_employee("EMP5001", department="DeptA", joined_on="2026-07-06")
        self.add_employee("EMP5002", department="DeptA", joined_on="2026-07-08")
        self.add_employee("EMP5003", department="DeptA", joined_on="2026-08-01")
        self.add_employee("EMP5004", department="DeptB", joined_on="2026-01-01")
        rows = [
            ("EMP5001", "2026-07-06", "PRESENT", 8.0, 90, 0, False),
            ("EMP5001", "2026-07-07", "WFH", 2.0, 0, 0, True),
            ("EMP5001", "2026-07-08", "LEAVE", None, 0, 0, False),
            ("EMP5001", "2026-07-11", "ON_DUTY", 6.0, 5, 0, False),
            ("EMP5004", "2026-07-06", "PRESENT", 9.0, 95, 0, False),
        ]
        self.db.attendance_logs.insert_many([
            {"emp_code": code, "date": day, "status": status, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
            for code, day, status, hours, late, overtime, half in rows
        ])
        monthly = main.employee_monthly("EMP5001", "2026-07")
        self.assertEqual(monthly["present_days"], 1.5)
        self.assertEqual(monthly["leave_days"], 1)
        self.assertEqual(monthly["total_late_minutes"], 95)
        summary = main.department_summary("2026-07", "DeptA")["items"][0]
        self.assertEqual(summary["headcount"], 2)
        self.assertEqual(summary["present_days"], 1.5)
        self.assertEqual(summary["avg_work_hours"], 5.33)
        self.assertEqual(summary["leave_count"], 1)
        leaderboard = main.late_leaderboard("2026-07", 1, None)["items"]
        self.assertEqual([(row["emp_code"], row["rank"]) for row in leaderboard], [("EMP5001", 1), ("EMP5004", 1)])
        scoped = main.late_leaderboard("2026-07", 1, "DeptA")["items"]
        self.assertEqual([(row["emp_code"], row["rank"]) for row in scoped], [("EMP5001", 1)])
        trend = main.department_trend("DeptA", date(2026, 7, 6), date(2026, 7, 12))["items"]
        self.assertEqual(len(trend), 7)
        self.assertEqual((trend[2]["headcount"], trend[2]["attendance_rate"]), (2, 0.0))
        self.assertFalse(trend[5]["is_working_day"])
        self.assertIsNone(trend[5]["attendance_rate"])
        self.assertEqual(trend[6]["moving_avg_7d"], 0.3)
        before_joining = main.department_trend("DeptA", date(2026, 7, 4), date(2026, 7, 5))["items"]
        self.assertTrue(all(row["headcount"] == 0 and row["attendance_rate"] is None for row in before_joining))
        rounding_employees = [employee(f"EMP51{i:02d}", department="RateRound") for i in range(32)]
        self.db.employees.insert_many(rounding_employees)
        self.db.attendance_logs.insert_one({
            "emp_code": "EMP5100", "date": "2026-07-06", "status": "PRESENT", "half_day": False,
            "late_minutes": 0, "overtime_minutes": 0,
        })
        rounded_rate = main.department_trend("RateRound", date(2026, 7, 6), date(2026, 7, 6))["items"][0]
        self.assertEqual(rounded_rate["attendance_rate"], 0.0313)
        self.assertEqual(rounded_rate["moving_avg_7d"], 0.0313)
        empty = main.employee_monthly("EMP5002", "2026-06")
        self.assertEqual(empty["present_days"], 0)
        self.assertIsNone(empty["attendance_pct"] if empty["working_days"] == 0 else "unexpected")
        self.assertEqual(invoke_status(main.employee_monthly, "EMP5999", "2026-07"), 404)
        self.assertEqual(invoke_status(main.department_trend, "Unknown", date(2026, 7, 1), date(2026, 7, 1)), 404)
        self.assertEqual(invoke_status(main.department_trend, "DeptA", date(2026, 1, 1), date(2026, 4, 3)), 422)

    def test_explain_uses_endpoint_pipelines_and_execution_stats(self):
        self.add_employee("EMP6001", department="Explain")
        for endpoint, kwargs in (
            ("employee_monthly", {"emp_code": "EMP6001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07", "department": "Explain"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 2, "department": "Explain"}),
            ("department_trend", {"department": "Explain", "from_date": date(2026, 7, 6), "to_date": date(2026, 7, 7)}),
            ("attendance_list", {"status": "PRESENT", "page": 1, "page_size": 5}),
        ):
            result = main.explain_endpoint(endpoint, **kwargs)
            self.assertEqual(result["endpoint"], endpoint)
            self.assertIn("executionStats", json.dumps(result["explain"]))
        self.assertEqual(
            main.build_department_summary_pipeline("2026-07-01", "2026-08-01", "2026-07-31", "Explain"),
            main.explain_endpoint("department_summary", month="2026-07", department="Explain")["explain"]["command"]["pipeline"],
        )

    def test_concurrent_duplicate_punches_and_regularizations(self):
        self.add_employee("EMP7001")
        barrier = Barrier(8)
        body = main.PunchInIn(emp_code="EMP7001", punched_at=epoch_ms(9, 30))

        def punch_worker():
            barrier.wait()
            return invoke_status(main.punch_in, body)

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: punch_worker(), range(8)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 7)

        self.add_employee("EMP7002")
        self.db.attendance_logs.insert_one({
            "emp_code": "EMP7002", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc), "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        })
        out_barrier = Barrier(8)
        out_body = main.PunchOutIn(emp_code="EMP7002", punched_at=epoch_ms(18, 30))

        def out_worker():
            out_barrier.wait()
            return invoke_status(main.punch_out, out_body)

        with ThreadPoolExecutor(max_workers=8) as pool:
            out_statuses = list(pool.map(lambda _: out_worker(), range(8)))
        self.assertEqual(out_statuses.count(200), 1)
        self.assertEqual(out_statuses.count(409), 7)

        self.add_employee("EMP7003")
        self.db.attendance_logs.insert_one({
            "emp_code": "EMP7003", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc), "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        })
        edit_barrier = Barrier(2)
        edits = [main.RegularizeRequest(status=value, reason="parallel correction", regularized_by=value) for value in ("WFH", "ON_DUTY")]

        def edit_worker(edit):
            edit_barrier.wait()
            return invoke_status(main.regularize_attendance, "EMP7003", date(2026, 7, 6), edit)

        with ThreadPoolExecutor(max_workers=2) as pool:
            edit_statuses = list(pool.map(edit_worker, edits))
        self.assertEqual(edit_statuses.count(200), 1)
        self.assertEqual(edit_statuses.count(409), 1)
        self.assertEqual(len(self.db.attendance_logs.find_one({"emp_code": "EMP7003"})["history"]), 1)


if __name__ == "__main__":
    unittest.main()
