import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.core.config import settings
from app.api.routes import admin, razorpay, whatsapp
from app.services.razorpay_service import RazorpayService


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    # Payments on: pending flight bookings hold seats, so something must release the ones nobody pays for.
    if RazorpayService.configured():
        tasks.append(asyncio.create_task(razorpay.sweep_unpaid_bookings()))
    if settings.VISA_DEMO_AUTOPROGRESS:
        tasks.append(asyncio.create_task(admin.visa_demo_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

app.include_router(whatsapp.router, prefix="/webhook", tags=["whatsapp"])
app.include_router(razorpay.router, prefix="/webhook", tags=["razorpay"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])

@app.get("/")
def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}
