from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import connect_to_db, close_db_connection
from app.routes import employees, attendance, analytics

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_db()
    yield
    await close_db_connection()

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)

app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(analytics.router)

@app.get("/health")
def health():
    return {"status": "ok"}