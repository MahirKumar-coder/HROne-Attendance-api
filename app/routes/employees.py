from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, status
from app import database
from app.models import EmployeeIn

router = APIRouter(prefix="/employees", tags=["Employees"])

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_employee(body: EmployeeIn):
    existing = await database.db.employees.find_one({"emp_code": body.emp_code})
    if existing:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    res = await database.db.employees.insert_one(doc)
    created = await database.db.employees.find_one({"_id": res.inserted_id})
    return database.serialize_doc(created)

@router.get("")
async def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    
    total = await database.db.employees.count_documents(q)
    skip = (page - 1) * page_size
    
    cursor = database.db.employees.find(q).skip(skip).limit(page_size)
    items = [database.serialize_doc(doc) async for doc in cursor]
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }