from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, status
from app import database
from app.models import PunchInIn, PunchOutIn, AttendanceCorrectionIn

IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(prefix="/attendance", tags=["Attendance"])

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))

@router.post("/punch-in", status_code=status.HTTP_201_CREATED)
async def punch_in(body: PunchInIn):
    emp = await database.db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(IST)
    d = ts.date().isoformat()

    existing = await database.db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if existing:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp.get("shift_start", "09:30")),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    res = await database.db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc

@router.post("/punch-out", status_code=status.HTTP_200_OK)
async def punch_out(body: PunchOutIn):
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(IST)
    d = ts.date().isoformat()

    record = await database.db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found for today")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=400, detail="Employee already punched out today")

    emp = await database.db.employees.find_one({"emp_code": body.emp_code})
    p_in = record["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=IST)

    work_hours = compute_work_hours(p_in, ts)
    overtime = compute_overtime(ts, emp.get("shift_end", "18:30"), d) if emp else 0
    half_day = work_hours < 4.0

    update_doc = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "updated_at": datetime.now(IST)
    }

    await database.db.attendance_logs.update_one({"_id": record["_id"]}, {"$set": update_doc})
    updated = await database.db.attendance_logs.find_one({"_id": record["_id"]})
    return database.serialize_doc(updated)

@router.patch("/{emp_code}/{date}", status_code=status.HTTP_200_OK)
async def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    record = await database.db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    emp = await database.db.employees.find_one({"emp_code": emp_code})
    
    # Preserve previous values in audit trail
    history_entry = {
        "previous_status": record.get("status"),
        "previous_punch_in": record.get("punch_in"),
        "previous_punch_out": record.get("punch_out"),
        "reason": body.reason,
        "corrected_by": body.corrected_by,
        "timestamp": datetime.now(IST)
    }

    new_p_in = datetime.fromisoformat(body.punch_in) if body.punch_in else record.get("punch_in")
    new_p_out = datetime.fromisoformat(body.punch_out) if body.punch_out else record.get("punch_out")
    new_status = body.status if body.status else record.get("status")

    work_hours = record.get("work_hours")
    overtime = record.get("overtime_minutes", 0)
    late_min = record.get("late_minutes", 0)

    if new_p_in and new_p_out:
        if new_p_out < new_p_in:
            raise HTTPException(status_code=400, detail="punch_out cannot be before punch_in")
        work_hours = compute_work_hours(new_p_in, new_p_out)
        if emp:
            overtime = compute_overtime(new_p_out, emp.get("shift_end", "18:30"), date)
            late_min = compute_late_minutes(new_p_in, emp.get("shift_start", "09:30"))

    update_data = {
        "status": new_status,
        "punch_in": new_p_in,
        "punch_out": new_p_out,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "late_minutes": late_min,
        "half_day": (work_hours < 4.0) if work_hours else False,
        "updated_at": datetime.now(IST)
    }

    await database.db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": update_data, "$push": {"history": history_entry}}
    )

    updated = await database.db.attendance_logs.find_one({"_id": record["_id"]})
    return database.serialize_doc(updated)

@router.get("", status_code=status.HTTP_200_OK)
async def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_val: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_val:
        q["status"] = status_val

    total = await database.db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = database.db.attendance_logs.find(q).sort("date", -1).skip(skip).limit(page_size)
    items = [database.serialize_doc(doc) async for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}