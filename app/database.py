import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: AsyncIOMotorClient = None
db = None

async def connect_to_db():
    global client, db
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB]
    # Create required indexes for optimal query & aggregation performance
    await db.employees.create_index("emp_code", unique=True)
    await db.employees.create_index("department")
    await db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    await db.attendance_logs.create_index("date")
    await db.attendance_logs.create_index("status")

async def close_db_connection():
    global client
    if client:
        client.close()

def serialize_doc(doc: dict) -> dict:
    if not doc:
        return doc
    if "_id" in doc:
        doc["id"] = str(doc["_id"])
        del doc["_id"]
    return doc