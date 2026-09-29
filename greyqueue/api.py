import asyncio
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from greyqueue import service
from greyqueue.config import Settings, settings
from greyqueue.db import make_sessions
from greyqueue.middleware import REQUEST_LIMIT, BoundRequests
from greyqueue.models import Attempt, Job, Result, SystemEvent, Worker
from greyqueue.observability import prometheus, snapshot, worker_rows
from greyqueue.protocol import (
    STATUSES,
    WORKER_ID,
    Assignment,
    AssignmentOut,
    AttemptOut,
    Claim,
    Completion,
    ErrorOut,
    HealthOut,
    Identity,
    JobOut,
    OperationsOut,
    RegisteredOut,
    Registration,
    RenewedOut,
    StateOut,
    StatusOut,
    Submit,
    WorkerOut,
)
from greyqueue.recovery import maintain

# Only database availability/validation warnings are logged here; the channel name is
# kept stable for anyone filtering on it.
log = logging.getLogger("greyqueue.database")
STATE_FILTER = "^(" + "|".join(STATUSES) + ")?$"
PROMETHEUS_TEXT = "text/plain; version=0.0.4; charset=utf-8"
# Raised by the middleware or the database on any documented route.
COMMON = {
    400: {
        "description": "Host header not in ALLOWED_HOSTS",
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
    408: {"model": ErrorOut, "description": "Request body not received within 10 seconds"},
    413: {"model": ErrorOut, "description": f"Request body over {REQUEST_LIMIT // 1024} KiB"},
    426: {"model": ErrorOut, "description": "HTTPS required (REQUIRE_TLS)"},
    503: {"model": ErrorOut, "description": "Database temporarily unavailable"},
}
ERRORS = {
    401: {"model": ErrorOut, "description": "Missing or invalid credentials"},
    404: {"model": ErrorOut, "description": "Unknown job or worker"},
    409: {"model": ErrorOut, "description": "Conflicting state (fenced, owned, not waiting)"},
    429: {
        "model": ErrorOut,
        "description": "Admission limit reached",
        "headers": {"Retry-After": {"schema": {"type": "integer"}, "description": "Seconds"}},
    },
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """The route-specific error responses a route can actually return."""
    return {code: ERRORS[code] for code in codes}


def unprocessable(exc: ValueError) -> list[dict[str, Any]]:
    """Every 422 uses FastAPI's request-validation shape: a list of {type, loc, msg}."""
    if isinstance(exc, ValidationError):  # task arguments rejected by the task's model
        return [
            {"type": e["type"], "loc": ["body", "args", *e["loc"]], "msg": e["msg"]}
            for e in exc.errors(include_url=False)
        ]
    field = getattr(exc, "field", None)
    loc = ["body", field] if field else ["body"]
    return [{"type": "value_error", "loc": loc, "msg": str(exc)}]


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

    app = FastAPI(title="GreyQueue", version="1.0.0", lifespan=lifespan, responses=COMMON)
    # Added first so BoundRequests stays outermost and also logs/labels host rejections.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    app.add_middleware(BoundRequests, require_tls=config.require_tls)

    def session():
        with sessions.begin() as db:
            yield db

    DB = Annotated[Session, Depends(session, scope="function")]
    # Credentials are documented as security schemes (see openapi() below), not params.
    SessionHeader = Annotated[str | None, Header(alias="X-Worker-Session", include_in_schema=False)]
    Authorization = Annotated[str | None, Header(include_in_schema=False)]
    WorkerPath = Annotated[str, PathParam(min_length=1, max_length=100, pattern=WORKER_ID)]

    def authorize(expected: str, value: str | None):
        # Bytes: compare_digest raises TypeError on non-ASCII str, which would be a 500.
        supplied = (value or "").encode("utf-8", "surrogateescape")
        if value is None or not secrets.compare_digest(supplied, f"Bearer {expected}".encode()):
            raise HTTPException(401, "Invalid bearer token")

    def client(authorization: Authorization = None):
        authorize(config.client_token, authorization)

    def worker(authorization: Authorization = None):
        authorize(config.worker_token, authorization)

    def identity(db, worker_id, credential):
        # Lock first, then compare: a check on an unlocked read could pass for an old
        # session while a concurrent re-registration of a DEAD ID replaces it.
        record = db.scalar(
            select(Worker)
            .where(Worker.id == worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        hashed = hashlib.sha256((credential or "").encode()).hexdigest()
        if (
            record is None
            or not record.session_hash
            or not secrets.compare_digest(record.session_hash, hashed)
        ):
            raise HTTPException(401, "Invalid worker session")
        return record

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc):
        # type/loc/msg only. The default also echoes `input`, which cannot be encoded when it
        # holds a NaN literal (json.loads accepts one), turning the 422 into a 500.
        detail = [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": detail}, status_code=422)

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
        log.warning(
            '{"event":"database_unavailable","sqlstate":"%s"}',
            getattr(getattr(exc, "orig", None), "sqlstate", None),
        )
        return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)

    @app.exception_handler(DataError)
    async def invalid_data(request: Request, exc):
        # Values PostgreSQL cannot store (e.g. out-of-range numbers) are client errors.
        log.warning(
            '{"event":"database_rejected_value","sqlstate":"%s"}',
            getattr(getattr(exc, "orig", None), "sqlstate", None),
        )
        detail = [{"type": "value_error", "loc": ["request"], "msg": "Value cannot be stored"}]
        return JSONResponse({"detail": detail}, status_code=422)

    @app.get("/health", response_model=HealthOut)
    def health(db: DB):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "version": "1.0.0"}

    @app.post(
        "/jobs",
        status_code=201,
        response_model=JobOut,
        dependencies=[Depends(client)],
        responses={
            200: {
                "model": JobOut,
                "description": "Idempotent replay: the job already admitted for this key",
            },
            **errors(401, 409, 429),
        },
    )
    def submit(body: Submit, db: DB, response: Response):
        try:
            job, created = service.admit(
                db,
                **body.model_dump(),
                queue_limit=config.queue_limit,
                submissions_per_minute=config.submissions_per_minute,
            )
        except ValueError as exc:
            raise HTTPException(422, unprocessable(exc)) from exc
        if not created:
            response.status_code = 200
        return service.serialize(db, job)

    @app.get(
        "/jobs", response_model=list[JobOut], dependencies=[Depends(client)], responses=errors(401)
    )
    def jobs(
        db: DB,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=1_000_000),
        state: str | None = Query(None, max_length=24, pattern=STATE_FILTER),
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

    @app.get(
        "/jobs/{job_id}",
        response_model=JobOut,
        dependencies=[Depends(client)],
        responses=errors(401, 404),
    )
    def job(job_id: UUID, db: DB):
        return service.serialize(db, service.get_job(db, job_id))

    @app.get(
        "/jobs/{job_id}/attempts",
        response_model=list[AttemptOut],
        dependencies=[Depends(client)],
        responses=errors(401, 404),
    )
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

    @app.delete(
        "/jobs/{job_id}",
        response_model=StatusOut,
        dependencies=[Depends(client)],
        responses=errors(401, 404, 409),
    )
    def cancel(job_id: UUID, db: DB):
        service.cancel(db, job_id)
        return {"status": "CANCELLED"}

    @app.get(
        "/workers",
        response_model=list[WorkerOut],
        dependencies=[Depends(client)],
        responses=errors(401),
    )
    def workers(db: DB, limit: int = Query(200, ge=1, le=200)):
        return worker_rows(db, limit)

    @app.post(
        "/workers/{worker_id}/drain",
        response_model=StateOut,
        dependencies=[Depends(client)],
        responses=errors(401, 404),
    )
    def drain(worker_id: WorkerPath, db: DB):
        record = db.scalar(
            select(Worker)
            .where(Worker.id == worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise service.Missing("Worker not found")
        # Kept apart from state: SUSPECT/DEAD overwrite state, but a worker that comes back
        # with the same session must still be told to drain (ADR 009). A DEAD worker keeps
        # its state and only records the intent; a new process taking over the ID clears it.
        if record.state != "DEAD":
            record.state = "DRAINING"
        if not record.drain_requested:
            record.drain_requested = True
            db.add(SystemEvent(kind="worker_draining", worker_id=worker_id))
        return {"state": record.state}

    @app.post(
        "/internal/workers/register",
        response_model=RegisteredOut,
        dependencies=[Depends(worker)],
        responses=errors(401, 409),
    )
    def register(body: Registration, db: DB):
        digest = hashlib.sha256(body.session_token.encode()).hexdigest()
        inserted = db.scalar(
            insert(Worker)
            .values(
                id=body.worker_id,
                capacity=body.capacity,
                capabilities=body.capabilities,
                session_hash=digest,
                # Statement time; the column default now() is transaction start.
                registered_at=func.clock_timestamp(),
                last_seen=func.clock_timestamp(),
            )
            .on_conflict_do_nothing()
            .returning(Worker.id)
        )
        record = db.scalar(
            select(Worker)
            .where(Worker.id == body.worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if inserted:
            db.add(SystemEvent(kind="worker_registered", worker_id=body.worker_id))
        elif secrets.compare_digest(record.session_hash or "", digest):
            # Replay by the same process (lost response). If it went DEAD meanwhile, revive
            # it now rather than leaving it unable to claim until its first heartbeat.
            if record.state == "DEAD":
                record.last_seen = service.database_time(db)
                record.state = "DRAINING" if record.drain_requested else "HEALTHY"
                db.add(SystemEvent(kind="worker_recovered", worker_id=body.worker_id))
        else:
            # A live ID stays owned; a DEAD one may be taken over by a restarted process.
            # Its unfinished attempts keep their old tokens and are fenced by lease expiry.
            if record.state != "DEAD":
                raise service.Conflict("Worker ID is already owned; use a new ID")
            record.session_hash = digest
            record.capacity, record.capabilities = body.capacity, body.capabilities
            record.state, record.last_seen = "HEALTHY", service.database_time(db)
            record.drain_requested = False  # a new process starts with a clean slate
            db.add(SystemEvent(kind="worker_reregistered", worker_id=body.worker_id))
        return {
            "worker_id": body.worker_id,
            "lease_seconds": config.lease_seconds,
            "heartbeat_interval": config.heartbeat_interval,
        }

    @app.post(
        "/internal/workers/heartbeat",
        response_model=StateOut,
        dependencies=[Depends(worker)],
        responses=errors(401),
    )
    def heartbeat(body: Identity, db: DB, credential: SessionHeader = None):
        record = identity(db, body.worker_id, credential)  # row is locked
        record.last_seen = service.database_time(db)
        if record.state in {"SUSPECT", "DEAD"}:
            db.add(SystemEvent(kind="worker_recovered", worker_id=body.worker_id))
        record.state = "DRAINING" if record.drain_requested else "HEALTHY"
        return {"state": record.state}

    @app.post(
        "/internal/claim",
        response_model=AssignmentOut | None,
        dependencies=[Depends(worker)],
        responses=errors(401, 404, 409),
    )
    def claim(body: Claim, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        try:
            item = service.claim(
                db, body.worker_id, body.slot, body.claim_id, config.scheduler, config.lease_seconds
            )
        except service.Invalid as exc:  # permanent (e.g. slot beyond capacity): not a 409
            raise HTTPException(422, unprocessable(exc)) from exc
        if item is None:
            return None
        job, attempt = item
        return {
            "job": service.serialize(db, job),
            "token": str(attempt.id),
            "fence": attempt.fence,
            "expires_at": attempt.expires_at,
        }

    @app.post(
        "/internal/jobs/{job_id}/start",
        response_model=StatusOut,
        dependencies=[Depends(worker)],
        responses=errors(401, 404, 409),
    )
    def start(job_id: UUID, body: Assignment, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        service.start(db, job_id, body.worker_id, body.token)
        return {"status": "RUNNING"}

    @app.post(
        "/internal/jobs/{job_id}/renew",
        response_model=RenewedOut,
        dependencies=[Depends(worker)],
        responses=errors(401, 404, 409),
    )
    def renew(job_id: UUID, body: Assignment, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        return {
            "expires_at": service.renew(
                db, job_id, body.worker_id, body.token, config.lease_seconds
            )
        }

    @app.post(
        "/internal/jobs/{job_id}/finish",
        response_model=StatusOut,
        dependencies=[Depends(worker)],
        responses=errors(401, 404, 409),
    )
    def finish(job_id: UUID, body: Completion, db: DB, credential: SessionHeader = None):
        identity(db, body.worker_id, credential)
        service.finish(
            db, job_id, body.worker_id, body.token, body.output, body.error, body.retryable
        )
        return {"status": "RECORDED"}

    @app.get(
        "/operations",
        response_model=OperationsOut,
        dependencies=[Depends(client)],
        responses=errors(401),
    )
    def operations(db: DB):
        return snapshot(db, config.queue_limit)

    @app.get(
        "/metrics",
        response_class=PlainTextResponse,
        dependencies=[Depends(client)],
        responses=errors(401),
    )
    def metrics(db: DB):
        text_format = prometheus(snapshot(db, config.queue_limit))
        return PlainTextResponse(text_format, media_type=PROMETHEUS_TEXT)

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(Path(__file__).parent / "dashboard" / "index.html")

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "dashboard" / name)

    def openapi():
        # The auth headers are hidden params (include_in_schema=False); declaring them as
        # security schemes lets client generators still send them.
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "clientToken": {"type": "http", "scheme": "bearer"},
            "workerToken": {"type": "http", "scheme": "bearer"},
            "workerSession": {"type": "apiKey", "in": "header", "name": "X-Worker-Session"},
        }
        for path, operations in schema["paths"].items():
            if path == "/health":
                continue
            if path == "/internal/workers/register":
                security = [{"workerToken": []}]
            elif path.startswith("/internal/"):
                security = [{"workerToken": [], "workerSession": []}]
            else:
                security = [{"clientToken": []}]
            for operation in operations.values():
                operation["security"] = security
                # Hidden headers are still validated, so FastAPI documents a 422 that no
                # request to a route without parameters or a body can trigger.
                if not operation.get("parameters") and "requestBody" not in operation:
                    operation["responses"].pop("422", None)
        # response_class=PlainTextResponse labels every /metrics response text/plain, but its
        # errors are JSON like everywhere else.
        for code, response in schema["paths"]["/metrics"]["get"]["responses"].items():
            if code not in {"200", "400"} and "text/plain" in response.get("content", {}):
                response["content"] = {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorOut"}}
                }
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
    return app
