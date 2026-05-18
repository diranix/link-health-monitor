from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import Base, engine, get_db
from app.models import Check, Monitor

# Prometheus metrics
http_requests_total = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"]
)
check_duration_seconds = Histogram(
    "check_duration_seconds", "URL check duration in seconds"
)
monitors_up = Counter("monitors_up_total", "Total successful checks")
monitors_down = Counter("monitors_down_total", "Total failed checks")

scheduler = AsyncIOScheduler()


async def check_url(url: str, monitor_id: int):
    async with httpx.AsyncClient(timeout=10) as client:
        start = datetime.utcnow()
        try:
            response = await client.get(url)
            response_time = (datetime.utcnow() - start).total_seconds()
            is_up = response.status_code < 500
            status_code = response.status_code
        except Exception:
            response_time = None
            is_up = False
            status_code = None

    with check_duration_seconds.time():
        pass

    if is_up:
        monitors_up.inc()
    else:
        monitors_down.inc()

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        check = Check(
            monitor_id=monitor_id,
            status_code=status_code,
            response_time=response_time,
            is_up=is_up,
        )
        db.add(check)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    result = None
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Monitor).where(Monitor.is_active.is_(True)))
        monitors = result.scalars().all()

    for monitor in monitors:
        scheduler.add_job(
            check_url,
            "interval",
            seconds=settings.check_interval,
            args=[monitor.url, monitor.id],
            id=f"monitor_{monitor.id}",
        )

    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Link Health Monitor", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/monitors")
async def get_monitors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Monitor))
    monitors = result.scalars().all()
    http_requests_total.labels("GET", "/monitors", "200").inc()
    return monitors


@app.post("/monitors")
async def create_monitor(url: str, name: str, db: AsyncSession = Depends(get_db)):
    monitor = Monitor(url=url, name=name)
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)

    scheduler.add_job(
        check_url,
        "interval",
        seconds=settings.check_interval,
        args=[monitor.url, monitor.id],
        id=f"monitor_{monitor.id}",
    )

    http_requests_total.labels("POST", "/monitors", "201").inc()
    return monitor


@app.delete("/monitors/{monitor_id}")
async def delete_monitor(monitor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Monitor).where(Monitor.id == monitor_id))
    monitor = result.scalar_one_or_none()

    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    job_id = f"monitor_{monitor_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await db.delete(monitor)
    await db.commit()

    http_requests_total.labels("DELETE", "/monitors", "200").inc()
    return {"message": "Monitor deleted"}
