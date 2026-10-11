# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing employee attendance tracking, business rules (grace period, overtime, half days, overnight shifts), concurrency-safe punch events, manual regularization with audit history, and MongoDB aggregation pipelines for organizational analytics.

## Requirements & Environment

- **Python**: 3.11+
- **MongoDB**: 6.0+ (Tested on MongoDB 7.0 / 8.2)
- Environment Variables:
  - `MONGO_URI` (defaults to `mongodb://localhost:27017`)
  - `MONGO_DB` (defaults to `attendance_db`)

Environment variables can be provided directly or through a local `.env` file (actual environment variables take precedence).

## Quick Start

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Seed sample data (optional):
   ```bash
   python sample_seed.py
   ```

3. Start the application:
   ```bash
   uvicorn app.main:app --port 8000
   ```

4. Verify health probe:
   ```bash
   curl http://localhost:8000/health
   ```

Interactive API documentation is available at `http://localhost:8000/docs`.

## Architecture & Implementation Notes

- **Single-File Application**: All application code lives inside `app/main.py`.
- **Database & Indexes**: Indexes for all unique constraints, query filters, sort paths, and aggregation pipelines are initialized idempotently during application startup via FastAPI's lifespan handler.
- **Explain Endpoint**: `GET /admin/explain/{endpoint}` executes `explain("executionStats")` on the production query or pipeline for any analytical or listing endpoint to verify that index scans (`IXSCAN`) are used and collection scans (`COLLSCAN`) are avoided.
- **Review & Design Rationale**:
  - `REVIEW.md`: Detailed audit of defects identified and resolved from the starter code.
  - `DECISIONS.md`: Explanations of indexing strategy, concurrency handling, tie-breaking, headcount calculation, and 100x scaling considerations.
