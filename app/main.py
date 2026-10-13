"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import json
from bson import json_util
import calendar
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def epoch_ms_from_dt(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def dt_from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)


def round_half_up(value: Any, digits: int = 2) -> float:
    if value is None:
        return None
    quant = Decimal("0.01") if digits == 2 else Decimal("0.0001")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def record_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    if shift_end <= shift_start and local.time() < parse_hhmm(shift_end):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_dt_for_date(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt_for_date(date_str: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(date_str)
    if shift_end <= shift_start:
        day += timedelta(days=1)
    return datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, date_str: Optional[str] = None) -> int:
    local = punch_in.astimezone(IST)
    date_value = date.fromisoformat(date_str) if date_str else local.date()
    start_dt = shift_start_dt_for_date(date_value.isoformat(), shift_start)
    diff_seconds = (local - start_dt).total_seconds()
    if diff_seconds <= 600:
        return 0
    return int(diff_seconds / 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    total_seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(total_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end_dt = shift_end_dt_for_date(date_str, shift_start, shift_end)
    diff_seconds = (punch_out.astimezone(IST) - end_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = int(diff_seconds / 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    rounded = round_half_up(work_hours, 2)
    return rounded < 4.5


def compute_record_derived(emp: Dict[str, Any], status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str) -> Dict[str, Any]:
    if status in {"ABSENT", "LEAVE"} or punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"], date_str)
    if punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    work_hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": compute_half_day(work_hours),
    }


def normalize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return entry
    out = {
        "at": epoch_ms_from_dt(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {},
    }
    for key, value in (entry.get("changes") or {}).items():
        out["changes"][key] = {
            "from": epoch_ms_from_dt(value.get("from")) if isinstance(value.get("from"), datetime) else value.get("from"),
            "to": epoch_ms_from_dt(value.get("to")) if isinstance(value.get("to"), datetime) else value.get("to"),
        }
    return out


def serialize_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = epoch_ms_from_dt(out.get("punch_in"))
    out["punch_out"] = epoch_ms_from_dt(out.get("punch_out"))
    out["history"] = [normalize_history_entry(item) for item in (out.get("history") or [])]
    return out


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = epoch_ms_from_dt(out.get("created_at"))
    return out


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    next_month = (datetime(year, mon, 1) + timedelta(days=32)).replace(day=1)
    return month + "-01", next_month.strftime("%Y-%m") + "-01"


def employee_exists(emp_code: str) -> bool:
    return db.employees.find_one({"emp_code": emp_code}) is not None


def first_day_of_month(month: str) -> str:
    return f"{month}-01"


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]],
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return query



def month_days(month: str) -> List[str]:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    return [f"{year:04d}-{month_num:02d}-{day:02d}" for day in range(1, last_day + 1)]


def build_late_leaderboard_pipeline(start: str, stop: str, department: Optional[str], limit: int) -> List[Dict[str, Any]]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start, "$lt": stop},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    presence = list(PRESENT_STATUSES)
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
                "start": {"$first": {"$dateFromString": {"dateString": from_date}}},
                "end": {"$first": {"$dateFromString": {"dateString": to_date}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$start",
                                                "endDate": "$end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$start",
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "date": "$days",
                "day_date": {"$dateFromString": {"dateString": "$days"}},
                "employee_codes": {
                    "$map": {"input": "$employees", "as": "employee", "in": "$$employee.emp_code"}
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {"$lte": ["$$employee.joined_on", "$days"]},
                        }
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"target_date": "$date", "employee_codes": "$employee_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$target_date"]}}},
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$employee_codes"]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", presence]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_stats",
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily_stats.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_stats.late_count", 0]}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"day_date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"day_date": 1},
                "output": {"_moving_rate": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_rate", None]},
                        None,
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$_moving_rate", 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def build_employee_monthly_pipeline(emp_code: str, start: str, end: str) -> List[Dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(
    start: str, stop: str, month_end: str, department: Optional[str]
) -> List[Dict[str, Any]]:
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", stop]},
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance",
            }
        },
        {"$unwind": {"path": "$attendance", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", list(PRESENT_STATUSES)]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$attendance.date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$attendance.half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$attendance.status", list(PRESENT_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$attendance.work_hours", None]}, None]},
                                ]
                            },
                            "$attendance.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$attendance.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$attendance.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$attendance.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def get_aggregation_explain(collection, pipeline):
    command = {
        "aggregate": collection.name,
        "pipeline": pipeline,
        "cursor": {},
    }
    result = collection.database.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    return json.loads(json_util.dumps(result))


def get_find_explain(collection, query, sort, skip, limit):
    command = {
        "find": collection.name,
        "filter": query,
        "sort": dict(sort),
        "skip": skip,
        "limit": limit,
    }
    result = collection.database.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    return json.loads(json_util.dumps(result))


# --------------------------------------------------------------------------- #
# Validation models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_times(cls, value: str) -> str:
        if value == "00:00":
            return value
        return value

    @field_validator("shift_end")
    @classmethod
    def ensure_not_same_shift(cls, value: str, info):
        start = info.data.get("shift_start")
        if start and value == start:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            raise ValueError("punched_at must be an epoch millisecond value when supplied")
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            raise ValueError("punched_at must be an epoch millisecond value when supplied")
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value not in ALL_STATUSES:
            raise ValueError("status must be one of the allowed attendance statuses")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_field(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            raise ValueError("epoch values must not be null when supplied")
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("epoch values must be valid epoch milliseconds")
        return value


@app.on_event("startup")
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code")
    db.employees.create_index([("department", ASCENDING)], name="idx_employees_department")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_employees_department_emp_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_attendance_emp_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)], name="idx_attendance_emp_punch_in")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="idx_attendance_emp_date_desc")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING)], name="idx_attendance_status")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    items = [serialize_employee(item) for item in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = dt_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punched_at = punched_at.replace(microsecond=0)

    date_str = record_date_for_punch(punched_at, emp["shift_start"], emp["shift_end"])
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": date_str}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"], date_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    doc["_id"] = res.inserted_id
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = dt_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punched_at = punched_at.replace(microsecond=0)

    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if existing is None:
        future_punch = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punched_at}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_punch is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")

    if existing.get("punch_out") is not None:
        if existing["punch_in"] <= punched_at:
            raise HTTPException(409, "that record is already punched out")

    if existing["punch_in"] >= punched_at:
        raise HTTPException(422, "punched_at must be after punch_in")

    if (punched_at - existing["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    updated = compute_record_derived(
        emp,
        existing.get("status", "PRESENT"),
        existing["punch_in"],
        punched_at,
        existing["date"],
    )
    res = db.attendance_logs.find_one_and_update(
        {"_id": existing["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": updated["work_hours"],
                "overtime_minutes": updated["overtime_minutes"],
                "half_day": updated["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if res is None:
        raise HTTPException(409, "that record is already punched out")
    return serialize_record(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = build_attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_record(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeRequest):
    date_str = date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    old_record = {k: v for k, v in record.items()}
    new_status = body.status if body.status is not None else record.get("status")
    new_punch_in = dt_from_epoch_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_punch_out = dt_from_epoch_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot include punch times")
        new_punch_in = None
        new_punch_out = None
        new_status = str(new_status)
    else:
        if new_punch_in is None:
            if record.get("punch_in") is None:
                raise HTTPException(422, "presence status requires a punch_in")
            new_punch_in = record["punch_in"]

        if new_punch_in is not None:
            attendance_date = record_date_for_punch(
                new_punch_in, emp["shift_start"], emp["shift_end"]
            )
            if attendance_date != date_str:
                raise HTTPException(422, "punch_in must be on the attendance date")

        if new_punch_out is not None and new_punch_in is not None and new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")

        if new_punch_out is not None and new_punch_in is not None:
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = compute_record_derived(emp, new_status, new_punch_in, new_punch_out, date_str)
    updated_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    changes = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(key)
        new_value = updated_values.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "no changes detected")

    hist_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": v["from"], "to": v["to"]} for k, v in changes.items()},
    }

    update_filter = {"_id": record["_id"]}
    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    res = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": derived["work_hours"],
            "late_minutes": derived["late_minutes"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }, "$push": {"history": hist_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if res is None:
        raise HTTPException(409, "concurrent edit detected")
    return serialize_record(res)




@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    start, end = month_bounds(month)
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    joined_date = date.fromisoformat(emp["joined_on"])

    working_days = sum(
        1
        for day in range(1, last_day + 1)
        if date(year, month_num, day).weekday() < 5
        and date(year, month_num, day) >= joined_date
    )

    pipeline = build_employee_monthly_pipeline(emp_code, start, end)

    results = list(db.attendance_logs.aggregate(pipeline))
    totals = results[0] if results else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = totals["present_days"]
    attendance_pct = (
        None
        if working_days == 0
        else round_half_up(present_days / working_days * 100, 2)
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(totals["leave_days"]),
        "late_count": int(totals["late_count"]),
        "total_late_minutes": int(totals["total_late_minutes"]),
        "total_overtime_minutes": int(totals["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start, stop = month_bounds(month)

    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(start, stop, month_end, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for result in results:
        avg_hours = result.get("avg_work_hours")
        items.append({
            "department": result["department"],
            "headcount": int(result["headcount"]),
            "present_days": round_half_up(result.get("present_days", 0), 2),
            "avg_work_hours": None if avg_hours is None else round_half_up(avg_hours, 2),
            "late_count": int(result.get("late_count", 0)),
            "total_late_minutes": int(result.get("total_late_minutes", 0)),
            "leave_count": int(result.get("leave_count", 0)),
            "on_duty_count": int(result.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, stop = month_bounds(month)

    pipeline = build_late_leaderboard_pipeline(start, stop, department, limit)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range exceeds 92 days")

    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    pipeline = build_department_trend_pipeline(
        department, from_date.isoformat(), to_date.isoformat()
    )
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = build_attendance_query(emp_code, date_from, date_to, status)
        sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
        explain = get_find_explain(
            db.attendance_logs,
            q,
            sort,
            (page - 1) * page_size,
            page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        start, stop = month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start, stop)
        explain = get_aggregation_explain(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    
    
    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")

        start, stop = month_bounds(month)
        year, month_num = map(int, month.split("-"))
        last_day = calendar.monthrange(year, month_num)[1]
        month_end = f"{month}-{last_day:02d}"

        employee_match = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$gte": ["$date", start]},
                                        {"$lt": ["$date", stop]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "attendance",
                }
            },
            {
                "$unwind": {
                    "path": "$attendance",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$attendance.status", list(PRESENT_STATUSES)]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$attendance.date"
                                                        }
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$attendance.half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "avg_work_hours": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$attendance.status", list(PRESENT_STATUSES)]},
                                        {
                                            "$ne": [
                                                {"$ifNull": ["$attendance.work_hours", None]},
                                                None,
                                            ]
                                        },
                                    ]
                                },
                                "$attendance.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$attendance.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$attendance.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$attendance.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$headcount_codes"},
                    "present_days": 1,
                    "avg_work_hours": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explain = get_aggregation_explain(db.employees, pipeline)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        start, stop = month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(
            start, stop, department, limit if limit is not None else 10
        )
        explain = get_aggregation_explain(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    
    
    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(
                422, "department, from and to are required"
            )

        if to_date < from_date:
            raise HTTPException(422, "to must be >= from")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range exceeds 92 days")

        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(404, "department not found")

        pipeline = build_department_trend_pipeline(
            department, from_date.isoformat(), to_date.isoformat()
        )
        explain = get_aggregation_explain(db.employees, pipeline)

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain,
        }


    raise HTTPException(422, "unsupported endpoint")


# --------------------------------------------------------------------------- #
# Legacy compatibility
# --------------------------------------------------------------------------- #
"""This file intentionally contains the complete assignment implementation."""
