from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None  # Epoch timestamp in milliseconds
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendanceCorrectionIn(BaseModel):
    punch_in: Optional[str] = None   # ISO format timestamp string
    punch_out: Optional[str] = None  # ISO format timestamp string
    status: Optional[str] = None
    reason: str = Field(..., description="Reason for manual correction")
    corrected_by: str = Field(..., description="Admin or HR username/ID")