"""Employee Attendance & Analytics API.

The API deliberately keeps the application code in this module, as required by
the assignment.  MongoDB stores instants as BSON UTC datetimes; the HTTP API
uses epoch milliseconds and calendar values remain strings.
"""

from __future__ import annotations

import json
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, NoReturn, Optional

from bson import Decimal128, ObjectId, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
EMP_CODE_PATTERN = r"^EMP[0-9]{4,6}$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5_000,
)
db = client[MONGO_DB]


# Stable names make the actual queries and the explain endpoint use the same
# indexes.  The unique indexes also provide the concurrency guarantees.
IDX_EMP_CODE = "uq_employees_emp_code"
IDX_EMP_DEPT_CODE = "ix_employees_department_emp_code"
IDX_EMP_DEPT_JOINED = "ix_employees_department_joined_on"
IDX_EMP_JOINED_DEPT = "ix_employees_joined_on_department"
IDX_LOG_CODE_DATE = "uq_attendance_emp_code_date"
IDX_LOG_DATE_CODE = "ix_attendance_date_emp_code"
IDX_LOG_STATUS_DATE_CODE = "ix_attendance_status_date_emp_code"
IDX_LOG_CODE_PUNCHIN = "ix_attendance_emp_code_punch_in"


def _validation_error(detail: str) -> NoReturn:
    raise HTTPException(
        status_code=422,
        detail=[{"loc": ["body"], "msg": detail, "type": "value_error"}],
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_second(value: datetime) -> datetime:
    return _as_utc(value).replace(microsecond=0)


def _epoch_ms_to_datetime(value: int) -> datetime:
    # Validation happens in the request model too; keeping the guard here
    # prevents a future internal caller from bypassing the API boundary.
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_EPOCH_MS
        or value > MAX_EPOCH_MS
    ):
        _validation_error("timestamp must be epoch milliseconds in the allowed range")
    return _truncate_second(datetime.fromtimestamp(value / 1000, tz=UTC))


def _datetime_to_epoch_ms(value: datetime) -> int:
    seconds = int((_as_utc(value) - EPOCH).total_seconds())
    return seconds * 1000


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_date(value: str, field_name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(DATE_PATTERN, value):
        _validation_error(f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _validation_error(f"{field_name} must be a real calendar date")


def _parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(MONTH_PATTERN, value):
        _validation_error("month must be YYYY-MM")
    try:
        first = date.fromisoformat(f"{value}-01")
    except ValueError:
        _validation_error("month must be YYYY-MM")
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def _parse_shift_time(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(TIME_PATTERN, value):
        _validation_error("shift times must use HH:MM")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _local_datetime(day: date, shift: str) -> datetime:
    hour, minute = _parse_shift_time(shift)
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = _as_utc(punch_in).astimezone(IST)
    start_minutes = _parse_shift_time(shift_start)[0] * 60 + _parse_shift_time(shift_start)[1]
    end_minutes = _parse_shift_time(shift_end)[0] * 60 + _parse_shift_time(shift_end)[1]
    result = local.date()
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        result -= timedelta(days=1)
    return result


def _shift_bounds(attendance_day: date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    start = _local_datetime(attendance_day, shift_start)
    end = _local_datetime(attendance_day, shift_end)
    start_minutes = _parse_shift_time(shift_start)[0] * 60 + _parse_shift_time(shift_start)[1]
    end_minutes = _parse_shift_time(shift_end)[0] * 60 + _parse_shift_time(shift_end)[1]
    if end_minutes <= start_minutes:
        end += timedelta(days=1)
    return start, end


def _compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_day: date,
) -> int:
    shift_start_dt, _ = _shift_bounds(attendance_day, shift_start, shift_start)
    elapsed_seconds = int((_as_utc(punch_in) - shift_start_dt).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 10 * 60 else 0


def _rounded_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_as_utc(punch_out) - _as_utc(punch_in)).total_seconds())
    rounded = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(rounded)


def _compute_overtime_minutes(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_day: date,
) -> int:
    _, shift_end_dt = _shift_bounds(attendance_day, shift_start, shift_end)
    elapsed_seconds = int((_as_utc(punch_out) - shift_end_dt).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds >= 30 * 60 else 0


def _compute_derived(
    *,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict[str, Any],
    attendance_day: date,
) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late_minutes = _compute_late_minutes(
        punch_in, employee["shift_start"], attendance_day
    )
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }

    work_hours = _rounded_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": _compute_overtime_minutes(
            punch_out,
            employee["shift_start"],
            employee["shift_end"],
            attendance_day,
        ),
        "half_day": Decimal(str(work_hours)) < Decimal("4.50"),
    }


def _api_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _datetime_to_epoch_ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: _api_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_api_value(item) for item in value]
    return value


def _api_employee(document: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in document.items() if key != "_id"}
    return _api_value(result)


def _api_attendance(document: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in document.items()
        if key not in {"_id", "id"}
    }
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return _api_value(result)


def _explain_value(value: Any) -> Any:
    # Explain contains BSON-specific values such as ObjectIds and dates.  The
    # relaxed Extended JSON representation keeps the complete explain tree
    # while making it JSON serializable for FastAPI.
    return json.loads(json_util.dumps(value))


def _ensure_indexes() -> None:
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name=IDX_EMP_CODE
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name=IDX_EMP_DEPT_CODE,
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name=IDX_EMP_DEPT_JOINED,
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name=IDX_EMP_JOINED_DEPT,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name=IDX_LOG_CODE_DATE,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name=IDX_LOG_DATE_CODE,
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name=IDX_LOG_STATUS_DATE_CODE,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name=IDX_LOG_CODE_PUNCHIN,
    )


indexes_ready = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global indexes_ready
    try:
        _ensure_indexes()
        indexes_ready = True
    except PyMongoError:
        indexes_ready = False
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def _validate_epoch(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_EPOCH_MS
        or value > MAX_EPOCH_MS
    ):
        raise ValueError(
            f"must be an integer epoch millisecond between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}"
        )
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120)
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = "09:30"
    shift_end: StrictStr = "18:30"
    joined_on: StrictStr

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(EMP_CODE_PATTERN, value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(EMAIL_PATTERN, value):
            raise ValueError("email has an invalid format")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, value: str) -> str:
        if not re.fullmatch(TIME_PATTERN, value):
            raise ValueError("shift times must use HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        if not re.fullmatch(DATE_PATTERN, value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a real calendar date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_pair(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: StrictInt | None = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return data

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value: int | None) -> int | None:
        return None if value is None else _validate_epoch(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: StrictInt | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return data

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value: int | None) -> int | None:
        return None if value is None else _validate_epoch(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Status | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_edit_values(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field_name in ("status", "punch_in", "punch_out"):
                if field_name in data and data[field_name] is None:
                    raise ValueError(f"{field_name} cannot be null")
        return data

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value: int | None) -> int | None:
        return None if value is None else _validate_epoch(value)


def _month_dates(month: str) -> tuple[date, date, datetime, int]:
    first, last = _parse_month(month)
    start_dt = datetime.combine(first, time.min, tzinfo=UTC)
    return first, last, start_dt, (last - first).days + 1


def _round_half_up_expression(value: Any, places: int) -> dict[str, Any]:
    scale = Decimal128(str(10**places))
    return {
        "$cond": [
            {"$eq": [value, None]},
            None,
            {
                "$divide": [
                    {
                        "$floor": {
                            "$add": [
                                {"$multiply": [{"$toDecimal": value}, scale]},
                                Decimal128("0.5"),
                            ]
                        }
                    },
                    scale,
                ]
            },
        ]
    }


def _presence_weight_expression() -> dict[str, Any]:
    return {
        "$cond": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {
                "$cond": [
                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    0.5,
                    1,
                ]
            },
            0,
        ]
    }


def _weekday_expression() -> dict[str, Any]:
    return {
        "$lte": [
            {
                "$isoDayOfWeek": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "timezone": "UTC",
                    }
                }
            },
            5,
        ]
    }


@app.get("/health")
def health() -> dict[str, str]:
    global indexes_ready
    try:
        db.command("ping")
        if not indexes_ready:
            _ensure_indexes()
            indexes_ready = True
    except PyMongoError as exc:
        indexes_ready = False
        raise HTTPException(status_code=503, detail="MongoDB not ready") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = _now_utc()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _api_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    hint = IDX_EMP_DEPT_CODE if department is not None else IDX_EMP_CODE
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query, hint=hint)
    cursor = (
        db.employees.find(query, {"_id": 0}, hint=hint)
        .sort([("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_api_employee(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        _epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_utc()
    )
    attendance_day = _attendance_date(
        punched_at, employee["shift_start"], employee["shift_end"]
    )
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _compute_late_minutes(
            punched_at, employee["shift_start"], attendance_day
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this attendance date",
        ) from exc
    return _api_attendance(document)


def _attendance_query(
    *,
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> tuple[dict[str, Any], str]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        _parse_date(date_from, "date_from")
        query.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        _parse_date(date_to, "date_to")
        query.setdefault("date", {})["$lte"] = date_to
    if date_from is not None and date_to is not None and date_from > date_to:
        _validation_error("date_from must not be after date_to")
    if status is not None:
        query["status"] = status

    if status is not None:
        hint = IDX_LOG_STATUS_DATE_CODE
    elif emp_code is not None:
        hint = IDX_LOG_CODE_DATE
    else:
        hint = IDX_LOG_DATE_CODE
    return query, hint


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(default=None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query, hint = _attendance_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(query, hint=hint)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0}, hint=hint)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [_api_attendance(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        _epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_utc()
    )
    punched_at = _truncate_second(punched_at)
    query = {
        "emp_code": body.emp_code,
        # Include every raw value whose truncated second can be at or before
        # the request time, preserving the API's whole-second rule.
        "punch_in": {"$lt": punched_at + timedelta(seconds=1)},
    }
    record = db.attendance_logs.find_one(
        query,
        sort=[("punch_in", DESCENDING)],
        hint=IDX_LOG_CODE_PUNCHIN,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance is already punched out")

    punch_in_value = _truncate_second(record["punch_in"])
    elapsed = punched_at - punch_in_value
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        _validation_error("punched_at must be after punch_in and within 24 hours")

    attendance_day = _parse_date(record["date"], "attendance date")
    derived = _compute_derived(
        status=record["status"],
        punch_in=punch_in_value,
        punch_out=punched_at,
        employee=employee,
        attendance_day=attendance_day,
    )
    update = {
        "$set": {
            "punch_in": punch_in_value,
            "punch_out": punched_at,
            **derived,
        }
    }
    compare_and_set = {
        "_id": record["_id"],
        "$and": [
            _cas_clause(record, field_name)
            for field_name in (
                "status",
                "punch_in",
                "punch_out",
                "work_hours",
                "late_minutes",
                "overtime_minutes",
                "half_day",
                "history",
            )
        ],
    }
    updated = db.attendance_logs.find_one_and_update(
        compare_and_set,
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance is already punched out")
    return _api_attendance(updated)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _truncate_second(left) == _truncate_second(right)
    if isinstance(left, Decimal128):
        left = left.to_decimal()
    if isinstance(right, Decimal128):
        right = right.to_decimal()
    if isinstance(left, (float, Decimal)) or isinstance(right, (float, Decimal)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (ValueError, TypeError, ArithmeticError):
            pass
    return left == right


def _change_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _truncate_second(value)
    return value


def _cas_clause(document: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Match one mutable field exactly, including whether it was missing."""
    if field_name not in document:
        return {field_name: {"$exists": False}}
    return {
        field_name: {
            "$exists": True,
            "$eq": document[field_name],
        }
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=DATE_PATTERN),
) -> dict[str, Any]:
    record_day = _parse_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date},
        hint=IDX_LOG_CODE_DATE,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    fields_set = body.model_fields_set
    current_status = record["status"]
    final_status = body.status if "status" in fields_set else current_status
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")
    current_punch_in = (
        _truncate_second(current_punch_in) if current_punch_in is not None else None
    )
    current_punch_out = (
        _truncate_second(current_punch_out) if current_punch_out is not None else None
    )

    if final_status not in ALL_STATUSES:
        _validation_error("invalid attendance status")
    if final_status not in PRESENCE_STATUSES and (
        "punch_in" in fields_set or "punch_out" in fields_set
    ):
        _validation_error("ABSENT and LEAVE records cannot include punch times")

    if final_status in PRESENCE_STATUSES:
        if "punch_in" in fields_set:
            assert body.punch_in is not None
        if "punch_out" in fields_set:
            assert body.punch_out is not None
        final_punch_in = (
            _epoch_ms_to_datetime(body.punch_in)
            if "punch_in" in fields_set
            else current_punch_in
        )
        final_punch_out = (
            _epoch_ms_to_datetime(body.punch_out)
            if "punch_out" in fields_set
            else current_punch_out
        )
        if final_punch_in is None:
            _validation_error("a presence status requires punch_in")
        if (
            _attendance_date(
                final_punch_in,
                employee["shift_start"],
                employee["shift_end"],
            )
            != record_day
        ):
            _validation_error("punch_in must belong to the record attendance date")
        if final_punch_out is not None:
            elapsed = final_punch_out - final_punch_in
            if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
                _validation_error("punch_out must be after punch_in and within 24 hours")
    else:
        final_punch_in = None
        final_punch_out = None

    derived = _compute_derived(
        status=final_status,
        punch_in=final_punch_in,
        punch_out=final_punch_out,
        employee=employee,
        attendance_day=record_day,
    )
    old_values = {
        "status": current_status,
        "punch_in": current_punch_in,
        "punch_out": current_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived,
    }
    changes: dict[str, dict[str, Any]] = {}
    for field_name in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if not _same_value(old_values[field_name], new_values[field_name]):
            changes[field_name] = {
                "from": _change_value(old_values[field_name]),
                "to": _change_value(new_values[field_name]),
            }
    if not changes:
        _validation_error("the correction does not change the attendance record")

    history_entry = {
        "at": _now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_filter: dict[str, Any] = {
        "_id": record["_id"],
        "$and": [
            _cas_clause(record, field_name)
            for field_name in (
                "status",
                "punch_in",
                "punch_out",
                "work_hours",
                "late_minutes",
                "overtime_minutes",
                "half_day",
                "history",
            )
        ],
    }
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                **derived,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    return _api_attendance(updated)


def _working_days_expression(start_dt: datetime, day_count: int) -> dict[str, Any]:
    day_value = {
        "$dateAdd": {
            "startDate": start_dt,
            "unit": "day",
            "amount": "$$offset",
        }
    }
    return {
        "$size": {
            "$filter": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "cond": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": day_value,
                                        "timezone": "UTC",
                                    }
                                },
                                "$joined_on",
                            ]
                        },
                        {"$lte": [{"$isoDayOfWeek": day_value}, 5]},
                    ]
                },
            }
        }
    }


def _monthly_log_stats(start: str, end: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$set": {
                "_weekday": _weekday_expression(),
                "_presence_weight": _presence_weight_expression(),
                "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": ["$_weekday", "$_presence_weight", 0]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "total_overtime_minutes": {"$sum": "$_overtime_minutes"},
            }
        },
    ]


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    first, last, month_start, day_count = _month_dates(month)
    start = first.isoformat()
    end = last.isoformat()
    zero_stats = {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }
    percentage = _round_half_up_expression(
        {
            "$multiply": [
                {
                    "$divide": [
                        {"$toDecimal": "$_stats.present_days"},
                        {"$toDecimal": "$working_days"},
                    ]
                },
                Decimal128("100"),
            ]
        },
        2,
    )
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": _monthly_log_stats(start, end),
                "as": "stats",
            }
        },
        {
            "$set": {
                "_stats": {
                    "$ifNull": [{"$arrayElemAt": ["$stats", 0]}, zero_stats]
                }
            }
        },
        {
            "$set": {
                "working_days": _working_days_expression(month_start, day_count)
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": month,
                "working_days": 1,
                "present_days": "$_stats.present_days",
                "leave_days": "$_stats.leave_days",
                "late_count": "$_stats.late_count",
                "total_late_minutes": "$_stats.total_late_minutes",
                "total_overtime_minutes": "$_stats.total_overtime_minutes",
                "attendance_pct": {
                    "$cond": [{"$gt": ["$working_days", 0]}, percentage, None]
                },
            }
        },
    ]


def _department_log_stats(start: str, end: str) -> list[dict[str, Any]]:
    presence = _presence_weight_expression()
    has_hours = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$set": {
                "_weekday": _weekday_expression(),
                "_presence_weight": presence,
                "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "_has_hours": has_hours,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": ["$_weekday", "$_presence_weight", 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            "$_has_hours",
                            {"$toDecimal": "$work_hours"},
                            Decimal128("0"),
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {"$cond": ["$_has_hours", 1, 0]}
                },
            }
        },
    ]


def _department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> list[dict[str, Any]]:
    first, last, _, _ = _month_dates(month)
    match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    zero_stats = {
        "present_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
        "hours_sum": Decimal128("0"),
        "hours_count": 0,
    }
    avg_hours = _round_half_up_expression(
        {
            "$divide": ["$hours_sum", {"$toDecimal": "$hours_count"}]
        },
        2,
    )
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": _department_log_stats(
                    first.isoformat(), last.isoformat()
                ),
                "as": "stats",
            }
        },
        {
            "$set": {
                "_stats": {
                    "$ifNull": [{"$arrayElemAt": ["$stats", 0]}, zero_stats]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_stats.present_days"},
                "late_count": {"$sum": "$_stats.late_count"},
                "total_late_minutes": {"$sum": "$_stats.total_late_minutes"},
                "leave_count": {"$sum": "$_stats.leave_count"},
                "on_duty_count": {"$sum": "$_stats.on_duty_count"},
                "hours_sum": {"$sum": "$_stats.hours_sum"},
                "hours_count": {"$sum": "$_stats.hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [{"$gt": ["$hours_count", 0]}, avg_hours, None]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str] = None
) -> list[dict[str, Any]]:
    first, last, _, _ = _month_dates(month)
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
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
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
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
        ]
    )
    return pipeline


def _department_trend_pipeline(
    department: str, from_date: date, to_date: date
) -> list[dict[str, Any]]:
    start_dt = datetime.combine(from_date, time.min, tzinfo=UTC)
    day_count = (to_date - from_date).days + 1
    presence_weight = _presence_weight_expression()
    rate = _round_half_up_expression(
        {
            "$divide": [
                {"$toDecimal": "$present_count"},
                {"$toDecimal": "$headcount"},
            ]
        },
        4,
    )
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "offset": {"$literal": 0}}},
        {
            "$densify": {
                "field": "offset",
                "range": {
                    "step": 1,
                    "bounds": [0, day_count],
                },
            }
        },
        {"$match": {"offset": {"$gte": 0, "$lt": day_count}}},
        {
            "$set": {
                "day": {
                    "$dateAdd": {
                        "startDate": start_dt,
                        "unit": "day",
                        "amount": "$offset",
                    }
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$match": {
                            "$expr": {"$lte": ["$joined_on", "$$trend_date"]}
                        }
                    },
                    {"$count": "value"},
                ],
                "as": "headcount_docs",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"department": department}},
                                {"$project": {"_id": 0, "emp_code": 1}},
                            ],
                            "as": "employee",
                        }
                    },
                    {"$match": {"employee.0": {"$exists": True}}},
                    {
                        "$set": {
                            "_presence_weight": presence_weight,
                            "_late": {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": "$_presence_weight"},
                            "late_count": {
                                "$sum": {"$cond": ["$_late", 1, 0]}
                            },
                        }
                    },
                ],
                "as": "day_stats",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_docs.value", 0]},
                        0,
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$day_stats.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$day_stats.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        rate,
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "_moving_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": _round_half_up_expression("$_moving_raw", 4),
            }
        },
    ]


def _aggregate(
    collection: Any, pipeline: list[dict[str, Any]], hint: str
) -> list[dict[str, Any]]:
    return list(collection.aggregate(pipeline, hint=hint, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=MONTH_PATTERN),
) -> dict[str, Any]:
    pipeline = _employee_monthly_pipeline(emp_code, month)
    rows = _aggregate(db.employees, pipeline, IDX_EMP_CODE)
    if not rows:
        raise HTTPException(status_code=404, detail="employee not found")
    return _api_value(rows[0])


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
) -> dict[str, Any]:
    pipeline = _department_summary_pipeline(month, department)
    hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED_DEPT
    rows = _aggregate(db.employees, pipeline, hint)
    return {"month": month, "items": [_api_value(row) for row in rows]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    pipeline = _late_leaderboard_pipeline(month, limit, department)
    rows = _aggregate(db.attendance_logs, pipeline, IDX_LOG_DATE_CODE)
    return {"month": month, "items": [_api_value(row) for row in rows]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
) -> dict[str, Any]:
    from_day = _parse_date(from_, "from")
    to_day = _parse_date(to, "to")
    if to_day < from_day:
        _validation_error("to must not be before from")
    if (to_day - from_day).days + 1 > 92:
        _validation_error("trend range cannot exceed 92 days")
    exists = db.employees.find_one(
        {"department": department}, {"_id": 1}, hint=IDX_EMP_DEPT_JOINED
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = _department_trend_pipeline(department, from_day, to_day)
    rows = _aggregate(db.employees, pipeline, IDX_EMP_DEPT_JOINED)
    return {"department": department, "items": [_api_value(row) for row in rows]}


EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


def _explain_aggregate(
    collection_name: str, pipeline: list[dict[str, Any]], hint: str
) -> dict[str, Any]:
    return db.command(
        {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
                "hint": hint,
                "allowDiskUse": True,
            },
            "verbosity": "executionStats",
        }
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(default=None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(default=None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(default=None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint not in EXPLAIN_ENDPOINTS:
        _validation_error("unknown explain endpoint")

    if endpoint == "attendance_list":
        query, hint = _attendance_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )
        skip = (page - 1) * page_size
        explain = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "projection": {"_id": 0},
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                    "hint": hint,
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _explain_value(explain),
        }

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            _validation_error("emp_code and month are required")
        pipeline = _employee_monthly_pipeline(emp_code, month)
        explain = _explain_aggregate("employees", pipeline, IDX_EMP_CODE)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_value(explain),
        }

    if endpoint == "department_summary":
        if month is None:
            _validation_error("month is required")
        pipeline = _department_summary_pipeline(month, department)
        hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED_DEPT
        explain = _explain_aggregate("employees", pipeline, hint)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_value(explain),
        }

    if endpoint == "late_leaderboard":
        if month is None:
            _validation_error("month is required")
        pipeline = _late_leaderboard_pipeline(month, limit, department)
        explain = _explain_aggregate("attendance_logs", pipeline, IDX_LOG_DATE_CODE)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _explain_value(explain),
        }

    if department is None or from_ is None or to is None:
        _validation_error("department, from and to are required")
    from_day = _parse_date(from_, "from")
    to_day = _parse_date(to, "to")
    if to_day < from_day:
        _validation_error("to must not be before from")
    if (to_day - from_day).days + 1 > 92:
        _validation_error("trend range cannot exceed 92 days")
    pipeline = _department_trend_pipeline(department, from_day, to_day)
    explain = _explain_aggregate("employees", pipeline, IDX_EMP_DEPT_JOINED)
    return {
        "endpoint": endpoint,
        "collection": "employees",
        "explain": _explain_value(explain),
    }
