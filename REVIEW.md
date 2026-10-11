# Code Review: Starter Attendance Module

## Critical Bugs & Flaws Identified

1. **In-Memory Sorting & Pagination Flaw in `list_attendance`**:
   - *Flaw*: `list_attendance` fetched all records into Python memory (`list(db.attendance_logs.find(q))`), sorted them in memory, and paginated using array slicing.
   - *Impact*: High RAM consumption and slow response times as dataset grows.
   - *Fix*: Replaced with MongoDB native `$sort`, `$skip`, and `$limit` at database engine level.

2. **Synchronous Driver (PyMongo) blocking FastAPI Event Loop**:
   - *Flaw*: Using synchronous `pymongo.MongoClient` inside FastAPI endpoints blocked the single-threaded event loop during IO operations.
   - *Fix*: Replaced `pymongo` with `motor.motor_asyncio.AsyncIOMotorClient`.

3. **Missing Indexes**:
   - *Flaw*: No compound unique index on `(emp_code, date)` or index on `department`.
   - *Fix*: Configured index initialization inside database connection startup handler.

4. **Timezone Handling Issues**:
   - *Flaw*: Mixed usage of naive datetimes and IST offsets resulted in incorrect duration and overtime calculations.
   - *Fix*: Enforced `timezone(timedelta(hours=5, minutes=30))` across all datetime conversions.