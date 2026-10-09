"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from typing import Any, List, Literal, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db_indexes()
    except Exception:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Serializers
# --------------------------------------------------------------------------- #
def to_epoch_ms(val: Any) -> Optional[int]:
    """Convert a UTC BSON datetime or timestamp to integer epoch milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return int(val.timestamp() * 1000)
    return None


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        if isinstance(v, dict):
            from_val = v.get("from")
            to_val = v.get("to")
            if isinstance(from_val, datetime):
                from_val = to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_ms(to_val)
            changes[k] = {"from": from_val, "to": to_val}
        else:
            changes[k] = v
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(created_at),
    }


def serialize_attendance_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600s) after shift_start.
    late_minutes = floor(minutes elapsed since shift_start), not since grace ended.
    """
    h, m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date)
    start_ist = datetime.combine(att_date, time(h, m), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_in_ist - start_ist).total_seconds()
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(val: Optional[Union[float, int, Decimal]], places: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers (2 or 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-places)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, only if >= 30, else 0."""
    h, m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    end_ist = datetime.combine(end_date, time(h, m), tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - end_ist).total_seconds()
    if diff_sec <= 0:
        return 0
    minutes = int(math.floor(diff_sec / 60))
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_and_joined(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return self


# Backward-compatibility alias
EmployeeIn = EmployeeCreate


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any):
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch millisecond")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("punched_at out of range")
        return v


# Backward-compatibility alias
PunchInIn = PunchInRequest


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any):
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch millisecond")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("punched_at out of range")
        return v


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_instants(cls, v: Any):
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("Must be an integer epoch millisecond")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("Epoch millisecond out of range")
        return v




# --------------------------------------------------------------------------- #
# Endpoints (Part A)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: returns 200 only if MongoDB responds to a ping, otherwise 503."""
    try:
        db.command("ping")
        init_db_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces uniqueness via unique index; sets created_at on server."""
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees: sorted by emp_code ascending, paginated, filtered total."""
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in: calculates attendance date per R1 and late_minutes per R2."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = (body.punched_at // 1000) * 1000
        punch_in_utc = datetime.fromtimestamp(punched_ms / 1000, tz=timezone.utc)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Convert to IST to determine attendance date (R1)
    punch_in_ist = punch_in_utc.astimezone(IST)
    cal_date = punch_in_ist.date()
    cal_time = punch_in_ist.strftime("%H:%M")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # Overnight shifts: shift_end <= shift_start
    if shift_end <= shift_start and cal_time < shift_end:
        attendance_date = (cal_date - timedelta(days=1)).isoformat()
    else:
        attendance_date = cal_date.isoformat()

    late_minutes = compute_late_minutes(punch_in_utc, shift_start, attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out:
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = (body.punched_at // 1000) * 1000
        punch_out_utc = datetime.fromtimestamp(punched_ms / 1000, tz=timezone.utc)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Locate most recent record with punch_in <= punch_out_utc
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
        },
        sort=[("punch_in", -1)],
    )

    if not candidate:
        future_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": punch_out_utc},
            },
            sort=[("punch_in", 1)],
        )
        if future_record is not None:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
        raise HTTPException(status_code=404, detail="No punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out for this record")

    cand_punch_in = candidate["punch_in"]
    if cand_punch_in.tzinfo is None:
        cand_punch_in = cand_punch_in.replace(tzinfo=timezone.utc)

    if punch_out_utc <= cand_punch_in:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if punch_out_utc > cand_punch_in + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch-out cannot be more than 24 hours after punch-in")

    work_hours = compute_work_hours(cand_punch_in, punch_out_utc)
    half_day = bool(work_hours < 4.50)
    overtime_minutes = compute_overtime(
        punch_out_utc,
        emp.get("shift_end", "18:30"),
        emp.get("shift_start", "09:30"),
        candidate["date"],
    )

    # Atomic conditional update for concurrency safety
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Already punched out for this record")

    return serialize_attendance_record(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes late_minutes, work_hours, overtime_minutes, half_day.
    Appends one entry to history with only the fields that actually changed.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be a valid calendar date YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    current_status = record["status"]
    current_punch_in = record.get("punch_in")
    if current_punch_in is not None and current_punch_in.tzinfo is None:
        current_punch_in = current_punch_in.replace(tzinfo=timezone.utc)

    current_punch_out = record.get("punch_out")
    if current_punch_out is not None and current_punch_out.tzinfo is None:
        current_punch_out = current_punch_out.replace(tzinfo=timezone.utc)

    current_work_hours = record.get("work_hours")
    current_late_minutes = record.get("late_minutes", 0)
    current_overtime_minutes = record.get("overtime_minutes", 0)
    current_half_day = record.get("half_day", False)

    # Setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )

    final_status = body.status if body.status is not None else current_status

    if final_status in ["ABSENT", "LEAVE"]:
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            in_ms = (body.punch_in // 1000) * 1000
            final_punch_in = datetime.fromtimestamp(in_ms / 1000, tz=timezone.utc)
        else:
            final_punch_in = current_punch_in

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        punch_in_ist = final_punch_in.astimezone(IST)
        cal_date = punch_in_ist.date()
        cal_time = punch_in_ist.strftime("%H:%M")
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")

        if shift_end <= shift_start and cal_time < shift_end:
            computed_date = (cal_date - timedelta(days=1)).isoformat()
        else:
            computed_date = cal_date.isoformat()

        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            out_ms = (body.punch_out // 1000) * 1000
            final_punch_out = datetime.fromtimestamp(out_ms / 1000, tz=timezone.utc)
        else:
            final_punch_out = current_punch_out

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if final_punch_out > final_punch_in + timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        final_late_minutes = compute_late_minutes(final_punch_in, shift_start, date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = bool(final_work_hours < 4.50)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_end, shift_start, date)
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

    # Diff fields that actually changed
    changes = {}
    if current_status != final_status:
        changes["status"] = {"from": current_status, "to": final_status}

    if to_epoch_ms(current_punch_in) != to_epoch_ms(final_punch_in):
        changes["punch_in"] = {"from": current_punch_in, "to": final_punch_in}

    if to_epoch_ms(current_punch_out) != to_epoch_ms(final_punch_out):
        changes["punch_out"] = {"from": current_punch_out, "to": final_punch_out}

    if current_work_hours != final_work_hours:
        changes["work_hours"] = {"from": current_work_hours, "to": final_work_hours}

    if current_late_minutes != final_late_minutes:
        changes["late_minutes"] = {"from": current_late_minutes, "to": final_late_minutes}

    if current_overtime_minutes != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": current_overtime_minutes, "to": final_overtime_minutes}

    if current_half_day != final_half_day:
        changes["half_day"] = {"from": current_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change any fields")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic Concurrency Control (OCC)
    occ_filter = {"_id": record["_id"]}
    rec_history = record.get("history")
    if rec_history is None or len(rec_history) == 0:
        occ_filter["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        occ_filter["history"] = {"$size": len(rec_history)}

    updated_doc = db.attendance_logs.find_one_and_update(
        occ_filter,
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records: sorted by date desc, emp_code asc. Fast on 100k docs."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics & Admin Endpoints (Part B & C)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Working days (R7): Mon-Fri from max(month_start, joined_on)
    working_days = 0
    joined_on_str = emp["joined_on"]
    for d in range(1, num_days + 1):
        day_str = f"{month}-{d:02d}"
        dt = date(year, month_num, d)
        if dt.weekday() < 5 and day_str >= joined_on_str:
            working_days += 1

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    year, month_num = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "log": "$logs",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$logs.date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                                ]
                            },
                            "$log.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$log.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$log.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$log.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": "$present_days",
                "avg_work_hours": {"$avg": "$work_hours_list"},
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        if doc.get("headcount", 0) > 0:
            items.append({
                "department": doc["department"],
                "headcount": doc["headcount"],
                "present_days": float(doc.get("present_days", 0.0)),
                "avg_work_hours": round_half_up(doc.get("avg_work_hours"), 2),
                "late_count": int(doc.get("late_count", 0)),
                "total_late_minutes": int(doc.get("total_late_minutes", 0)),
                "leave_count": int(doc.get("leave_count", 0)),
                "on_duty_count": int(doc.get("on_duty_count", 0)),
            })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month."""
    year, month_num = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
        {
            "$unwind": "$employee",
        },
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {"total_late_minutes": -1, "_id": 1}
        },
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
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """Daily attendance trend for a department."""
    dept_employees = list(
        db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    )
    if not dept_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must be on or before to")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    from_dt = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
    to_dt_plus_one = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)
    dept_emp_codes = [e["emp_code"] for e in dept_employees]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
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
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "date_dt": 1,
                "day_of_week": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_employees,
                            "as": "e",
                            "cond": {
                                "$lte": [
                                    "$$e.joined_on",
                                    {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "raw_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
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
                "moving_avg_7d": "$raw_moving_avg",
            }
        },
    ]

    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(row.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(row.get("moving_avg_7d"), 4),
        })

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """MongoDB explain with executionStats verbosity for an endpoint's main query."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(exp),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, month_num = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, month_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {
                "$project": {
                    "emp_code": 1,
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(exp),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, month_num = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, month_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        match_stage = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "log": "$logs",
                    "day_of_week": {
                        "$cond": [
                            {"$ifNull": ["$logs.date", False]},
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}},
                            None,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "work_hours_list": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                                    ]
                                },
                                "$log.work_hours",
                                "$$REMOVE",
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": "$present_days",
                    "avg_work_hours": {"$avg": "$work_hours_list"},
                    "late_count": "$late_count",
                    "total_late_minutes": "$total_late_minutes",
                    "leave_count": "$leave_count",
                    "on_duty_count": "$on_duty_count",
                }
            },
            {"$sort": {"department": 1}},
        ]
        exp = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": jsonable_encoder(exp),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, month_num = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, month_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
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
        if department:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
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
        ])
        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(exp),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if from_date > to_date:
            raise HTTPException(status_code=422, detail="from must be on or before to")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        dept_employees = list(
            db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1})
        )
        dept_emp_codes = [e["emp_code"] for e in dept_employees]

        from_dt = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
        to_dt_plus_one = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$_id",
                    "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_plus_one]},
                }
            },
            {
                "$project": {
                    "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                    "date_dt": 1,
                    "day_of_week": {"$dayOfWeek": "$date_dt"},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": dept_employees,
                                "as": "e",
                                "cond": {
                                    "$lte": [
                                        "$$e.joined_on",
                                        {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                                    ]
                                },
                            }
                        }
                    },
                }
            },
            {
                "$project": {
                    "date": 1,
                    "date_dt": 1,
                    "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "raw_moving_avg": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
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
                    "moving_avg_7d": "$raw_moving_avg",
                }
            },
        ]
        exp = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(exp),
        }

