# Design Rationale & Decisions

## 1. Motor (Async MongoDB) over PyMongo
FastAPI operates on an asynchronous event loop. Synchronous database drivers like PyMongo block the thread during network calls to MongoDB. Switching to Motor ensures non-blocking I/O.

## 2. In-Database Pagination vs In-Memory Slicing
MongoDB's native `.skip()` and `.limit()` operations stream only the requested page of documents over the network, dramatically lowering memory footprint.

## 3. History Append Strategy for Auditing
Rather than storing audit records in a completely disconnected format, pushing mutation entries directly into an embedded `history` array on `attendance_logs` guarantees atomic updates (`$push`) alongside current field mutations (`$set`).

## 4. Indexing Strategy
- `db.employees.create_index("emp_code", unique=True)`
- `db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)`