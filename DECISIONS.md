# Design Decisions & Architecture Rationale (Part C)

### 1. Which indexes you created and why
We created unique compound index `attendance_logs: (emp_code, date)` to enforce single-log integrity and race safety; `(date, -1), (emp_code, 1)` for cursor sorting and pagination without memory sort; `(date, 1), (late_minutes, 1)` to accelerate monthly leaderboard and analytics range queries; and `(emp_code, 1), (punch_in, -1)` for rapid punch-out resolution. On `employees`, unique `emp_code` prevents duplicates, while compound `(department, 1), (joined_on, 1)` eliminates collection scans during cross-collection trend and summary pipelines.

### 2. What exactly happens when two identical punch-ins arrive at the same instant
Both requests compute the attendance date and attempt an `insert_one()`. Because `attendance_logs` has a unique compound index on `(emp_code, date)`, MongoDB's WiredTiger engine permits only the first write to commit. The second concurrent write triggers a `DuplicateKeyError`, which our application catches and cleanly returns as an HTTP 409 Conflict, avoiding check-then-insert race conditions.

### 3. What your leaderboard returns for a tie at the cutoff
We use standard competition ranking (`$rank` in MongoDB `$setWindowFields`), assigning identical ranks to tied scores and skipping subsequent ranks (1, 2, 2, 4). The `limit` filter (`$match: {rank: {$lte: limit}}`) applies strictly *after* ranking, ordered by `total_late_minutes` descending and `emp_code` ascending. If multiple employees tie at or below the cutoff rank, all tied employees are returned, allowing the result set to exceed `limit` rows as specified.

### 4. How your department summary counts employees with no logs
The aggregation pipeline initiates from the `employees` collection rather than `attendance_logs`, filtering employees where `joined_on <= end_of_month`. It groups by `department` to establish true `headcount` before executing a `$lookup` into `attendance_logs`. Employees with zero attendance logs are naturally retained in the headcount, while their absent attendance metrics contribute zero to presence counts and are excluded from work-hour averages.

### 5. What you would change for 100 times the data
At 10,000,000+ documents, we would shard `attendance_logs` on a hashed or compound key `(emp_code, date)`, pre-aggregate daily and monthly rollups into an incremental summary collection (avoiding cross-collection lookups on on-demand queries), switch pagination from `.skip()` to keyset/cursor-based pagination (`date`, `_id`), and route analytical read workloads to secondary replicas.