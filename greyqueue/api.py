import asyncio
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.orm import Session

from greyqueue import service
from greyqueue.config import Settings, settings
from greyqueue.db import make_sessions
from greyqueue.middleware import BoundRequests
from greyqueue.models import Attempt, Job, Result, SystemEvent, Worker
from greyqueue.observability import prometheus, snapshot, worker_rows
from greyqueue.protocol import Assignment, Claim, Completion, Identity, Registration, Submit
from greyqueue.recovery import maintain


def create_app(config: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = config or settings()
    engine, sessions = make_sessions(config.database_url)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        task = asyncio.create_task(maintain(sessions, config, stop))
        try:
            yield
        finally:
            stop.set()
            await task
            engine.dispose()

    app = FastAPI(title="GreyQueue", version="1.0.0", lifespan=lifespan)
    app.add_middleware(BoundRequests, require_tls=config.require_tls)

    def session():
        with sessions.begin() as db:
            yield db

    DB = Annotated[Session, Depends(session, scope="function")]
    SessionHeader = Annotated[str | None, Header(alias="X-Worker-Session")]

    def authorize(expected: str, value: str | None):
        if value is None or not secrets.compare_digest(value, f"Bearer {expected}"):
            raise HTTPException(401, "Invalid bearer token")

    def client(authorization: Annotated[str | None, Header()] = None):
        authorize(config.client_token, authorization)

    def worker(authorization: Annotated[str | None, Header()] = None):
        authorize(config.worker_token, authorization)

    def identity(db, worker_id, credential):
        record = db.get(Worker, worker_id)
        hashed = hashlib.sha256((credential or "").encode()).hexdigest()
        if (
            record is None
            or not record.session_hash
            or not secrets.compare_digest(record.session_hash, hashed)
        ):
            raise HTTPException(401, "Invalid worker session")
        return record

    @app.exception_handler(service.Conflict)
    async def conflict(request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(service.Missing)
    async def missing(request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(service.Saturated)
    async def saturated(request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "2"})

    @app.exception_handler(SQLTimeoutError)
    @app.exception_handler(OperationalError)
    async def unavailable(request: Request, exc):
        logging.getLogger("greyqueue.database").warning(
            '{"event":"database_unavailable","sqlstate":"%s"}',
            getattr(getattr(exc, "orig", None), "sqlstate", None),
        )
        return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)

    @app.get("/health")
    def health(db: DB):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "version": "1.0.0"}

    @app.post("/jobs", status_code=201, dependencies=[Depends(client)])
    def submit(body: Submit, db: DB):
        try:
            job = service.submit(
                db,
                **body.model_dump(),
                queue_limit=config.queue_limit,
                submissions_per_minute=config.submissions_per_minute,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return service.serialize(db, job)

    @app.get("/jobs", dependencies=[Depends(client)])
    def jobs(
        db: DB,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        state: str | None = Query(None, max_length=24),
    ):
        query = select(Job).order_by(Job.created_at.desc(), Job.id).offset(offset).limit(limit)
        if state:
            query = query.where(Job.status == state)
        rows = list(db.scalars(query))
        results = {
            r.job_id: r
            for r in db.scalars(select(Result).where(Result.job_id.in_([j.id for j in rows])))
        }
        return [service.serialize(db, job, results) for job in rows]

    @app.get("/jobs/{job_id}", dependencies=[Depends(client)])
    def job(job_id: UUID, db: DB):
        return service.serialize(db, service.get_job(db, job_id))

    @app.get("/jobs/{job_id}/attempts", dependencies=[Depends(client)])
    def attempts(job_id: UUID, db: DB):
        service.get_job(db, job_id)
        return [
            {
                "id": str(a.id),
                "worker_id": a.worker_id,
                "fence": a.fence,
                "outcome": a.outcome,
                "error": a.error,
                "output": a.output,
                "created_at": a.created_at,
                "started_at": a.started_at,
                "finished_at": a.finished_at,
                "expires_at": a.expires_at,
            }
            for a in db.scalars(
                select(Attempt).where(Attempt.job_id == job_id).order_by(Attempt.fence)
            )
        ]

    @app.delete("/jobs/{job_id}", dependencies=[Depends(client)])
    def cancel(job_id: UUID, db: DB):
        service.cancel(db, job_id)
        return {"status": "CANCELLED"}

    @app.get("/workers", dependencies=[Depends(client)])
    def workers(db: DB, limit: int = Query(100, ge=1, le=200)):
        return worker_rows(db, limit)

    @app.post("/workers/{worker_id}/drain", dependencies=[Depends(client)])
    def drain(worker_id: str, db: DB):
        record = db.scalar(
            select(Worker)
            .where(Worker.id == worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise service.Missing("Worker not found")
        record.state = "DRAINING"
        db.add(SystemEvent(kind="worker_draining", worker_id=worker_id))
        return {"state": record.state}

    @app.post("/internal/workers/register", dependencies=[Depends(worker)])
    def register(body: Registration, db: DB):
        digest = hashlib.sha256(body.session_token.encode()).hexdigest()
        inserted = db.scalar(
            insert(Worker)
            .values(
                id=body.worker_id,
                capacity=body.capacity,
                capabilities=body.capabilities,
                session_hash=digest,
            )
            .on_conflict_do_nothing()
            .returning(Worker.id)
        )
        record = db.get(Worker, body.worker_id)
        if not secrets.compare_digest(record.session_hash or "", digest):
            raise service.Conflict("Worker ID is already owned; use a new ID")
        if inserted:
            db.add(SystemEvent(kind="worker_registered", worker_id=body.worker_id))
        return {
            "worker_id": body.worker_id,
            "lease_seconds": config.lease_seconds,
            "heartbeat_interval": config.heartbeat_interval,
        }

    @app.post("/internal/workers/heartbeat", dependencies=[Depends(worker)])
    def heartbeat(body: Identity, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        record = db.scalar(
            select(Worker)
            .where(Worker.id == body.worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        record.last_seen = service.database_time(db)
        if record.state != "DRAINING":
            record.state = "HEALTHY"
        return {"state": record.state}

    @app.post("/internal/claim", dependencies=[Depends(worker)])
    def claim(body: Claim, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        item = service.claim(
            db, body.worker_id, body.slot, body.claim_id, config.scheduler, config.lease_seconds
        )
        if item is None:
            return None
        job, attempt = item
        return {
            "job": service.serialize(db, job),
            "token": str(attempt.id),
            "fence": attempt.fence,
            "expires_at": attempt.expires_at,
        }

    @app.post("/internal/jobs/{job_id}/start", dependencies=[Depends(worker)])
    def start(job_id: UUID, body: Assignment, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        service.start(db, job_id, body.worker_id, body.token)
        return {"status": "RUNNING"}

    @app.post("/internal/jobs/{job_id}/renew", dependencies=[Depends(worker)])
    def renew(job_id: UUID, body: Assignment, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        return {
            "expires_at": service.renew(
                db, job_id, body.worker_id, body.token, config.lease_seconds
            )
        }

    @app.post("/internal/jobs/{job_id}/finish", dependencies=[Depends(worker)])
    def finish(job_id: UUID, body: Completion, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        service.finish(
            db, job_id, body.worker_id, body.token, body.output, body.error, body.retryable
        )
        return {"status": "recorded"}

    @app.get("/operations", dependencies=[Depends(client)])
    def operations(db: DB):
        return snapshot(db, config.queue_limit)

    @app.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(client)])
    def metrics(db: DB):
        return prometheus(snapshot(db, config.queue_limit))

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).parent / "dashboard" / "index.html")

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "dashboard" / name)

    return app
