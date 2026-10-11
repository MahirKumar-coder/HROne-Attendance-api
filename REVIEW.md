# Code Review: Starter Attendance Module (Part A)

This document records the defects identified during the thorough code review of the starter implementation (`app/main.py`), including exact locations, nature of the flaws, how they manifest under testing/concurrency/scale, and how each was fixed.

---

## Defects Identified & Fixed

### 1. `GET /health` returned unconditional 200 without pinging MongoDB
- **Where**: `health` route handler.
- **What was wrong**: The endpoint immediately returned `{"status": "ok"}` without verifying database connectivity. If MongoDB went down, network partitioned, or failed to start within the 20-second startup window, the health check still returned 200 instead of 503 as required by the contract.
- **How noticed**: Stopped the MongoDB daemon or set an unreachable `MONGO_URI`. `GET /health` returned HTTP 200 instead of HTTP 503.
- **How fixed**: Wrapped an async MongoDB ping command `await db.command("ping")` in a `try...except` block, returning HTTP 200 only upon successful response and raising HTTP 503 (`Database unavailable`) upon failure.

---

### 2. Check-then-insert race condition in `POST /employees`
- **Where**: `create_employee` endpoint.
- **What was wrong**: The endpoint performed a pre-check query `await db.employees.find_one({"emp_code": body.emp_code})` before calling `insert_one()`. Under concurrent duplicate create requests with the same `emp_code`, both requests could pass the check simultaneously, resulting in unhandled driver exceptions (HTTP 500) rather than a controlled HTTP 409.
- **How noticed**: Sending 5 simultaneous `POST /employees` requests with identical payload `{"emp_code": "EMP0001", ...}`.
- **How fixed**: Enforced a database-level unique index on `emp_code` created at application startup (`create_index("emp_code", unique=True)`), removed the fragile check-then-insert pattern, and caught `pymongo.errors.DuplicateKeyError` to return HTTP 409 Conflict.

---

### 3. Missing validation for identical shift start and end times in `POST /employees`
- **Where**: `create_employee` validation logic.
- **What was wrong**: An employee could be created where `shift_start == shift_end` (e.g., `"09:30"` and `"09:30"`). The contract explicitly specifies that `shift_start` must differ from `shift_end`, and such requests must be rejected with HTTP 422.
- **How noticed**: Submitted payload with `{"shift_start": "10:00", "shift_end": "10:00"}`. The starter accepted the request and created the record with HTTP 201.
- **How fixed**: Added validation verifying `body.shift_start != body.shift_end`; raised HTTP 422 Unprocessable Content if equal.

---

### 4. Schema and serialization violations for employee creation and retrieval
- **Where**: `create_employee` and `list_employees`.
- **What was wrong**: 
  1. Internal MongoDB `_id` was transformed to `"id": str(_id)` and exposed in responses, violating the contract which prohibits internal IDs from appearing in the API.
  2. `created_at` was stored/returned as a naive datetime or ISO string rather than integer epoch milliseconds (`EpochMillis`, >= 100000000000) truncated to whole seconds.
- **How noticed**: Validating the response against `openapi.yaml` showed unexpected `"id"` field and invalid format for `created_at`.
- **How fixed**: Converted `created_at` to integer epoch milliseconds truncated to whole seconds (`truncate_to_seconds_ms`), and strictly serialized outputs according to the Pydantic `Employee` schema without `_id` or `id`.

---

### 5. `GET /employees` lacked sorting by `emp_code` ascending
- **Where**: `list_employees` query.
- **What was wrong**: The starter did not sort employee documents, returning them in natural MongoDB disk order, whereas the contract strictly mandates sorting by `emp_code` ascending.
- **How noticed**: Created employees out of alphabetical order (`EMP0005`, `EMP0001`). Listing employees returned them in insertion order.
- **How fixed**: Added `.sort("emp_code", 1)` directly onto the MongoDB cursor before `.skip()` and `.limit()`.

---

### 6. Filtered count discrepancy in `GET /employees` pagination
- **Where**: `list_employees` total count.
- **What was wrong**: Total count was either computed over the entire collection or mismatched when a `department` query filter was supplied.
- **How noticed**: Queried `GET /employees?department=Engineering` in a multi-department database; `total` returned total employee count rather than matching Engineering employees.
- **How fixed**: Executed `await db.employees.count_documents(q)` using the exact query filter dictionary `q` so `total` accurately reflects the filtered set per Rule R10.

---

### 7. Check-then-insert race condition in `POST /attendance/punch-in`
- **Where**: `punch_in` endpoint.
- **What was wrong**: Queried `find_one({"emp_code": ..., "date": ...})` before inserting. Two simultaneous punch-ins for the same employee and date could both pass the query and trigger unhandled duplicate key crashes or race inconsistencies.
- **How noticed**: Dispatched concurrent asynchronous punch-in requests for the same employee and instant; caused uncaught driver errors or duplicate records.
- **How fixed**: Leveraged a unique compound index on `("emp_code", 1), ("date", 1)` initialized at startup, caught `DuplicateKeyError`, and returned HTTP 409 Conflict immediately.

---

### 8. Failure to assign overnight shift punch-in to previous calendar day (Rule R1)
- **Where**: Attendance date calculation in `punch_in`.
- **What was wrong**: The starter used `ts.date().isoformat()` unconditionally. For overnight shifts (where `shift_end <= shift_start`, e.g., 22:00 to 06:00), an employee punching in at 05:30 IST belongs to the shift that started the previous calendar day. The starter incorrectly attributed it to the current day.
- **How noticed**: Employee with shift 22:00-06:00 punching in at 05:30 IST on 2026-07-07 was assigned `date: "2026-07-07"` instead of `"2026-07-06"`.
- **How fixed**: Implemented `get_attendance_date()`: if `shift_end <= shift_start` and the punch-in IST time of day is strictly earlier than `shift_end`, the attendance date is assigned to `punch_in_ist.date() - timedelta(days=1)`.

---

### 9. Lack of sub-second truncation (Rule R1)
- **Where**: Instant normalization in `punch_in` and `punch_out`.
- **What was wrong**: Millisecond / microsecond fractions were preserved directly in storage and math, leading to discrepancy in boundary comparisons (e.g., 09:40:00.900 should be evaluated as 09:40:00).
- **How noticed**: Punching in at 09:40:00.900 IST for a 09:30 shift was treated as late because fractional seconds exceeded 10:00 grace.
- **How fixed**: Applied `(epoch_ms // 1000) * 1000` truncating all instants to whole seconds prior to storage, derived field computation, and API responses.

---

### 10. Defective late calculation logic (Rule R2)
- **Where**: `compute_late_minutes` helper.
- **What was wrong**: 
  1. The starter evaluated `minutes > 10` after performing rough integer division on seconds, failing on edge cases like 09:40:01 (which is strictly > 10:00 grace and should be 10 minutes late).
  2. Failed to use exact scheduled start datetime anchored on the calculated attendance date in IST.
- **How noticed**: Shift 09:30: punch-in at 09:40:01 gave 0 late minutes instead of 10.
- **How fixed**: Anchored scheduled shift start in IST; measured `elapsed_seconds = (punch_in_ist - scheduled_start).total_seconds()`. Only if `elapsed_seconds > 600` (strictly more than 10 minutes), computed `int(elapsed_seconds // 60)` floor minutes since `shift_start`.

---

### 11. Overnight shift punch-out matching failure
- **Where**: `punch_out` endpoint record lookup.
- **What was wrong**: The starter looked up open records using `find_one({"emp_code": body.emp_code, "date": today})`. On an overnight shift, punch-out occurs the next morning, so looking up by `today` failed with 404 because the punch-in was recorded under yesterday's date.
- **How noticed**: Night-shift employee punched in on 2026-07-06 at 22:00, punched out on 2026-07-07 at 06:15; endpoint returned 404 "No punch-in record found for today".
- **How fixed**: Looked up the employee's most recent attendance record whose `punch_in <= punched_at` sorted by `punch_in` descending (`{"emp_code": emp_code, "punch_in": {"$lte": punch_out_dt}}, sort=[("punch_in", -1)]`).

---

### 12. Non-atomic punch-out race condition
- **Where**: `punch_out` update operation.
- **What was wrong**: Read record, performed calculations in Python, and executed `update_one({"_id": record["_id"]})`. Two parallel punch-out requests for the same open record both succeeded, returning two 200 responses instead of exactly one 200 and one 409.
- **How noticed**: Fired two concurrent `POST /attendance/punch-out` requests; both returned 200.
- **How fixed**: Executed atomic `find_one_and_update({"_id": record["_id"], "punch_out": None}, ...)` with `ReturnDocument.AFTER`. If the record was already punched out or claimed by a concurrent request, the operation returned `None`, triggering an immediate HTTP 409 Conflict.

---

### 13. Incorrect half-day threshold (Rule R5)
- **Where**: `half_day` calculation in `punch_out`.
- **What was wrong**: Starter set `half_day = work_hours < 4.0`. Rule R5 explicitly mandates that `half_day` is true when the rounded `work_hours < 4.50`.
- **How noticed**: Employee with 4.25 work hours was marked `half_day: False`.
- **How fixed**: Updated condition to `work_hours < 4.50`.

---

### 14. Missing validation for date ranges in `GET /attendance`
- **Where**: `list_attendance` query parameter handling.
- **What was wrong**: If `date_from > date_to`, the endpoint executed an impossible MongoDB range query and returned an empty list `[]` instead of HTTP 422.
- **How noticed**: Sent `GET /attendance?date_from=2026-07-10&date_to=2026-07-01`; returned HTTP 200 with 0 items instead of HTTP 422.
- **How fixed**: Added explicit check `if date_from and date_to and date_from > date_to: raise HTTPException(422, ...)`.

---

## What Was Examined and Kept

1. **Async Motor Driver**: Motor (`AsyncIOMotorClient`) was maintained and properly integrated with FastAPI's `lifespan` context manager for non-blocking database I/O.
2. **MongoDB Extended JSON Compatibility**: Kept compatibility with ISO dates stored in MongoDB as BSON datetimes, while strictly converting to and from integer epoch milliseconds at the API boundary.
3. **Database Indexing at Lifespan Startup**: Startup index creation was retained and enhanced with compound index definitions covering sorting, filtering, lookup, and aggregation execution paths.