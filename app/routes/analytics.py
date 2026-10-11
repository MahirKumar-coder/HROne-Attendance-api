from fastapi import APIRouter, HTTPException, Query, status
from app import database

router = APIRouter(prefix="/analytics", tags=["Analytics"])

@router.get("/employees/{emp_code}/monthly")
async def get_monthly_employee_analytics(
    emp_code: str,
    month: str = Query(..., description="Format: YYYY-MM e.g. 2026-10")
):
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}"}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_days_present": {"$sum": 1},
                "total_work_hours": {"$sum": "$work_hours"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}}
            }
        }
    ]
    res = await database.db.attendance_logs.aggregate(pipeline).to_list(length=1)
    if not res:
        return {
            "emp_code": emp_code,
            "month": month,
            "total_days_present": 0,
            "total_work_hours": 0.0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "half_days": 0
        }
    out = res[0]
    out["month"] = month
    out["emp_code"] = out.pop("_id")
    return out

@router.get("/departments/summary")
async def get_department_summary():
    pipeline = [
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs"
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$department",
                "total_records": {"$sum": 1},
                "avg_work_hours": {"$avg": "$logs.work_hours"},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "total_records": 1,
                "avg_work_hours": {"$round": ["$avg_work_hours", 2]},
                "total_late_minutes": 1,
                "total_overtime_minutes": 1
            }
        }
    ]
    return await database.db.employees.aggregate(pipeline).to_list(length=100)

@router.get("/leaderboard/late")
async def get_late_leaderboard(limit: int = Query(10, ge=1, le=50)):
    pipeline = [
        {"$match": {"late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_occurrences": {"$sum": 1}
            }
        },
        {"$sort": {"total_late_minutes": -1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_occurrences": 1
            }
        }
    ]
    return await database.db.attendance_logs.aggregate(pipeline).to_list(length=limit)

@router.get("/departments/{department}/trend")
async def get_department_trend(department: str):
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "log"
            }
        },
        {"$unwind": "$log"},
        {
            "$group": {
                "_id": "$log.date",
                "present_count": {"$sum": 1},
                "total_work_hours": {"$sum": "$log.work_hours"}
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "total_work_hours": {"$round": ["$total_work_hours", 2]}
            }
        }
    ]
    return await database.db.employees.aggregate(pipeline).to_list(length=365)