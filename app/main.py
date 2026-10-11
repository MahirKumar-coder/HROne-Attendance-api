import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

client: Optional[AsyncIOMotorClient] = None
db: Any = None


# -----------------------------------------------------------------------------
# Enums & Schemas
# -----------------------------------------------------------------------------
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# -----------------------------------------------------------------------------
# Business Logic Helpers (R1 - R8)
# -----------------------------------------------------------------------------
def round_half_up(value: Union[float, Decimal], decimals: int = 2) -> float:
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    dec = Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(dec)


def truncate_to_seconds_ms(epoch_ms: int) -> int:
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    truncated_ms = truncate_to_seconds_ms(epoch_ms)
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Date is IST calendar date of punch_in.

    Exception for overnight shift (shift_end <= shift_start): if punch_in
    IST time is strictly earlier than shift_end, it belongs to the previous
    day's shift.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start

    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_time = datetime(
        year=punch_in_ist.year,
        month=punch_in_ist.month,
        day=punch_in_ist.day,
        hour=end_h,
        minute=end_m,
        tzinfo=IST,
    )

    if is_overnight and punch_in_ist < shift_end_time:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, attendance_date_str: str, shift_start: str) -> int:
    """R2: Grace period 10 minutes.

    Late only if strictly more than 10:00 after shift_start.
    late_minutes = floor minutes since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    y, m, d = map(int, attendance_date_str.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    scheduled_start = datetime(y, m, d, sh, sm, 0, tzinfo=IST)

    elapsed_seconds = (punch_in_ist - scheduled_start).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    duration_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    val = Decimal(str(duration_seconds)) / Decimal("3600")
    return round_half_up(val, 2)


def compute_overtime_minutes(
    punch_out_utc: datetime,
    attendance_date_str: str,
    shift_start: str,
    shift_end: str,
) -> int:
    """R3: Floor minutes from shift_end to punch_out, only if >= 30, else 0.

    Handles overnight shifts where shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    y, m, d = map(int, attendance_date_str.split("-"))
    eh, em = map(int, shift_end.split(":"))

    scheduled_end = datetime(y, m, d, eh, em, 0, tzinfo=IST)
    if shift_end <= shift_start:
        scheduled_end += timedelta(days=1)

    elapsed_seconds = (punch_out_ist - scheduled_end).total_seconds()
    if elapsed_seconds >= 1800:
        return int(elapsed_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry_for_api(entry: Dict[str, Any]) -> HistoryEntry:
    raw_at = entry.get("at")
    at_ms = dt_to_epoch_ms(raw_at) if isinstance(raw_at, datetime) else int(raw_at)

    raw_changes = entry.get("changes", {})
    changes: Dict[str, Dict[str, Any]] = {}
    for k, v in raw_changes.items():
        frm = v.get("from")
        to = v.get("to")
        if k in ("punch_in", "punch_out"):
            frm = dt_to_epoch_ms(frm) if isinstance(frm, datetime) else frm
            to = dt_to_epoch_ms(to) if isinstance(to, datetime) else to
        changes[k] = {"from": frm, "to": to}

    return HistoryEntry(
        at=at_ms,
        by=entry.get("by", ""),
        reason=entry.get("reason", ""),
        changes=changes,
    )


def serialize_attendance_doc(doc: Dict[str, Any]) -> AttendanceRecord:
    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")

    hist = [
        serialize_history_entry_for_api(h) for h in doc.get("history", [])
    ]

    return AttendanceRecord(
        emp_code=doc["emp_code"],
        date=doc["date"],
        status=Status(doc["status"]),
        punch_in=dt_to_epoch_ms(p_in) if isinstance(p_in, datetime) else p_in,
        punch_out=dt_to_epoch_ms(p_out) if isinstance(p_out, datetime) else p_out,
        work_hours=doc.get("work_hours"),
        late_minutes=doc.get("late_minutes", 0),
        overtime_minutes=doc.get("overtime_minutes", 0),
        half_day=doc.get("half_day", False),
        history=hist,
    )


# -----------------------------------------------------------------------------
# Lifespan & Index Creation
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB]

    # Create indexes idempotently at startup
    # employees collection
    await db.employees.create_index("emp_code", unique=True)
    await db.employees.create_index([("department", 1), ("joined_on", 1)])
    await db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    await db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    await db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    await db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    await db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    await db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    await db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])

    yield

    if client:
        client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# -----------------------------------------------------------------------------
# 1. Health Probe
# -----------------------------------------------------------------------------
@app.get("/health", tags=["system"])
async def health():
    try:
        await db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )


# -----------------------------------------------------------------------------
# 2. Employees Endpoints
# -----------------------------------------------------------------------------
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
async def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shift_start must differ from shift_end",
        )

    now_utc = datetime.now(timezone.utc)
    now_utc_truncated = datetime.fromtimestamp(
        truncate_to_seconds_ms(int(now_utc.timestamp() * 1000)) / 1000.0,
        tz=timezone.utc,
    )

    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated

    try:
        await db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists",
        )

    return Employee(
        emp_code=doc["emp_code"],
        name=doc["name"],
        email=doc["email"],
        department=doc["department"],
        shift_start=doc["shift_start"],
        shift_end=doc["shift_end"],
        joined_on=doc["joined_on"],
        created_at=dt_to_epoch_ms(doc["created_at"]),
    )


@app.get("/employees", tags=["employees"])
async def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = await db.employees.count_documents(q)
    skip = (page - 1) * page_size

    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    async for d in cursor:
        items.append(
            Employee(
                emp_code=d["emp_code"],
                name=d["name"],
                email=d["email"],
                department=d["department"],
                shift_start=d["shift_start"],
                shift_end=d["shift_end"],
                joined_on=d["joined_on"],
                created_at=dt_to_epoch_ms(d["created_at"]),
            )
        )

    return EmployeePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# -----------------------------------------------------------------------------
# 3. Attendance Endpoints
# -----------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
async def punch_in(body: PunchInRequest):
    emp = await db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    now_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_in_dt = ms_to_utc_dt(now_ms)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        await db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already punched in for this date",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
async def punch_out(body: PunchOutRequest):
    emp = await db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    now_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_out_dt = ms_to_utc_dt(now_ms)

    # Find the most recent record whose punch_in is <= punched_at
    record = await db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found at or before punched_at",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    p_in_dt: datetime = record["punch_in"]
    if p_in_dt.tzinfo is None:
        p_in_dt = p_in_dt.replace(tzinfo=timezone.utc)

    duration_seconds = (punch_out_dt - p_in_dt).total_seconds()
    if duration_seconds <= 0 or duration_seconds > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(p_in_dt, punch_out_dt)
    overtime_min = compute_overtime_minutes(
        punch_out_dt,
        record["date"],
        emp["shift_start"],
        emp["shift_end"],
    )
    half_day = compute_half_day(work_hours)

    updated_doc = await db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated_doc is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record was already punched out",
        )

    return serialize_attendance_doc(updated_doc)


@app.get("/attendance", tags=["attendance"])
async def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Status] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status_filter is not None:
        q["status"] = status_filter.value

    total = await db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) async for doc in cursor]

    return AttendancePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
async def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = await db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    record = await db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record not found",
        )

    target_status = body.status.value if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )
        target_punch_in_dt = None
        target_punch_out_dt = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in_dt = ms_to_utc_dt(body.punch_in)
        else:
            target_punch_in_dt = record.get("punch_in")

        if target_punch_in_dt is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        # punch_in must stay on the record's attendance date per R1
        p_in_date = get_attendance_date(target_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if p_in_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            target_punch_out_dt = ms_to_utc_dt(body.punch_out)
        else:
            target_punch_out_dt = record.get("punch_out")

        target_late_minutes = compute_late_minutes(target_punch_in_dt, date, emp["shift_start"])

        if target_punch_out_dt is not None:
            dur = (target_punch_out_dt - target_punch_in_dt).total_seconds()
            if dur <= 0 or dur > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            target_work_hours = compute_work_hours(target_punch_in_dt, target_punch_out_dt)
            target_overtime_minutes = compute_overtime_minutes(
                target_punch_out_dt,
                date,
                emp["shift_start"],
                emp["shift_end"],
            )
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Check for actual changes
    changes_bson: Dict[str, Dict[str, Any]] = {}

    def check_diff(field: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes_bson[field] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), target_status)
    check_diff("punch_in", record.get("punch_in"), target_punch_in_dt)
    check_diff("punch_out", record.get("punch_out"), target_punch_out_dt)
    check_diff("work_hours", record.get("work_hours"), target_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), target_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), target_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), target_half_day)

    if not changes_bson:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request modifies no fields",
        )

    now_utc_truncated = ms_to_utc_dt(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": now_utc_truncated,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_bson,
    }

    current_history_length = len(record.get("history", []))

    updated_doc = await db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_length},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in_dt,
                "punch_out": target_punch_out_dt,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated_doc is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict",
        )

    return serialize_attendance_doc(updated_doc)


# -----------------------------------------------------------------------------
# 4. Analytics Pipelines
# -----------------------------------------------------------------------------
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
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
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
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
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
async def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = await db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    joined_date = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()

    working_days = 0
    for day in range(1, days_in_month + 1):
        cur_date = date(year, m, day)
        if cur_date >= joined_date and cur_date.weekday() < 5:
            working_days += 1

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = await db.attendance_logs.aggregate(pipeline).to_list(length=1)

    if res:
        data = res[0]
        present_days = float(data["present_days"])
        leave_days = int(data["leave_days"])
        late_count = int(data["late_count"])
        total_late_minutes = int(data["total_late_minutes"])
        total_overtime_minutes = int(data["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        pct = (
            Decimal(str(present_days))
            / Decimal(str(working_days))
            * Decimal("100")
        )
        attendance_pct = round_half_up(pct, 2)
    else:
        attendance_pct = None

    return EmployeeMonthly(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=present_days,
        leave_days=leave_days,
        late_count=late_count,
        total_late_minutes=total_late_minutes,
        total_overtime_minutes=total_overtime_minutes,
        attendance_pct=attendance_pct,
    )


def build_department_summary_pipeline(month: str, department_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department_filter is not None:
        match_filter["department"] = department_filter

    return [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "filtered_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$l.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$ifNull": ["$$this.late_minutes", 0]},
                            ]
                        },
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$filtered_work_hours"}, 0]},
                        {"$round": [{"$avg": "$filtered_work_hours.work_hours"}, 2]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"department": 1}},
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
async def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    pipeline = build_department_summary_pipeline(month, department)
    docs = await db.employees.aggregate(pipeline).to_list(length=1000)
    items = [
        DepartmentSummaryItem(
            department=d["department"],
            headcount=d["headcount"],
            present_days=float(d["present_days"]),
            avg_work_hours=d.get("avg_work_hours"),
            late_count=d["late_count"],
            total_late_minutes=d["total_late_minutes"],
            leave_count=d["leave_count"],
            on_duty_count=d["on_duty_count"],
        )
        for d in docs
    ]
    return DepartmentSummary(month=month, items=items)


def build_late_leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
async def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    docs = await db.attendance_logs.aggregate(pipeline).to_list(length=1000)
    items = [
        LeaderboardItem(
            rank=d["rank"],
            emp_code=d["emp_code"],
            name=d["name"],
            department=d["department"],
            total_late_minutes=d["total_late_minutes"],
            late_count=d["late_count"],
        )
        for d in docs
    ]
    return Leaderboard(month=month, items=items)


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    start_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    bounds_end = end_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "items": {
                    "$concatArrays": [
                        "$logs",
                        [
                            {"date": from_date, "is_dummy": True},
                            {"date": to_date, "is_dummy": True},
                        ],
                    ]
                }
            }
        },
        {"$unwind": "$items"},
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$items.date"}}
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$items.is_dummy", True]},
                                    {
                                        "$in": [
                                            "$items.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$items.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$items.is_dummy", True]},
                                    {"$gt": ["$items.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, bounds_end],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "dayOfWeek": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
async def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    emp_count = await db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown department",
        )

    f_d = datetime.strptime(from_date, "%Y-%m-%d").date()
    t_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    if t_d < f_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be on or after 'from' date",
        )

    delta_days = (t_d - f_d).days + 1
    if delta_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    docs = await db.employees.aggregate(pipeline).to_list(length=365)

    items = [
        TrendItem(
            date=d["date"],
            is_working_day=d["is_working_day"],
            headcount=d["headcount"],
            present_count=float(d["present_count"]),
            late_count=d["late_count"],
            attendance_rate=d.get("attendance_rate"),
            moving_avg_7d=d.get("moving_avg_7d"),
        )
        for d in docs
    ]
    return Trend(department=department, items=items)


# -----------------------------------------------------------------------------
# 5. Explain Endpoint
# -----------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}", tags=["admin"])
async def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Status] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown explain endpoint: {endpoint}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be after date_to",
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status_filter is not None:
            q["status"] = status_filter.value

        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        explain_res = await cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly explain",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = await db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary explain",
            )
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = await db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard explain",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = await db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend explain",
            )
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = await db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }