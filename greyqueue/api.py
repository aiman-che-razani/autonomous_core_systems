import json
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from greyqueue import service
from greyqueue.config import Settings, settings
from greyqueue.db import make_sessions
from greyqueue.models import Job, Worker


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(max_length=64)
    args: dict[str, Any]


class Identity(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class Assignment(Identity):
    token: UUID


class Completion(Assignment):
    output: dict[str, Any] | None = None
    error: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def result_shape(self):
        if (self.output is None) == (self.error is None):
            raise ValueError("Provide exactly one of output or error")
        if len(json.dumps(self.output, allow_nan=False)) > 64000:
            raise ValueError("Output too large")
        return self


def create_app(config: Settings | None = None) -> FastAPI:
    config = config or settings()
    engine, sessions = make_sessions(config.database_url)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="GreyQueue", version="0.1.0", lifespan=lifespan)

    # Bound streamed bodies as well as Content-Length; untrusted JSON never reaches
    # validation after an unlimited allocation.
    class BodyLimit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            messages, total = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                total += len(message.get("body", b""))
                if total > 131072:
                    return await JSONResponse({"detail": "Request too large"}, 413)(
                        scope, receive, send
                    )
                messages.append(message)
                if not message.get("more_body", False):
                    break

            async def replay():
                if messages:
                    return messages.pop(0)
                return await receive()

            await self.app(scope, replay, send)

    app.add_middleware(BodyLimit)

    def session():
        with sessions.begin() as db:
            yield db

    DB = Annotated[Session, Depends(session, scope="function")]

    def authorize(expected: str, value: str | None):
        if value is None or not secrets.compare_digest(value, f"Bearer {expected}"):
            raise HTTPException(401, "Invalid bearer token")

    def client(authorization: Annotated[str | None, Header()] = None):
        authorize(config.client_token, authorization)

    def worker(authorization: Annotated[str | None, Header()] = None):
        authorize(config.worker_token, authorization)

    @app.exception_handler(service.Conflict)
    async def conflict(request: Request, exc: service.Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(service.Missing)
    async def missing(request: Request, exc: service.Missing):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/health")
    def health(db: DB):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/jobs", status_code=201, dependencies=[Depends(client)])
    def submit(body: Submit, db: DB):
        try:
            job = service.submit(db, body.task, body.args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return service.serialize(db, job)

    @app.get("/jobs", dependencies=[Depends(client)])
    def jobs(db: DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return [
            service.serialize(db, job)
            for job in db.scalars(
                select(Job).order_by(Job.created_at, Job.id).offset(offset).limit(limit)
            )
        ]

    @app.get("/jobs/{job_id}", dependencies=[Depends(client)])
    def job(job_id: UUID, db: DB):
        return service.serialize(db, service.get_job(db, job_id))

    @app.delete("/jobs/{job_id}", dependencies=[Depends(client)])
    def cancel(job_id: UUID, db: DB):
        service.cancel(db, job_id)
        return {"status": "CANCELLED"}

    @app.get("/workers", dependencies=[Depends(client)])
    def workers(db: DB, limit: int = Query(100, ge=1, le=200)):
        return [
            {
                "id": w.id,
                "state": "HEALTHY",
                "last_seen": w.last_seen.isoformat(),
                "note": "Observed heartbeat only; failure detection is deferred",
            }
            for w in db.scalars(select(Worker).order_by(Worker.id).limit(limit))
        ]

    @app.post("/internal/workers/register", dependencies=[Depends(worker)])
    def register(body: Identity, db: DB):
        db.execute(
            insert(Worker)
            .values(id=body.worker_id)
            .on_conflict_do_update(index_elements=[Worker.id], set_={"last_seen": service.now()})
        )
        return {"worker_id": body.worker_id}

    @app.post("/internal/workers/heartbeat", dependencies=[Depends(worker)])
    def heartbeat(body: Identity, db: DB):
        record = db.get(Worker, body.worker_id)
        if record is None:
            raise service.Missing("Worker not found")
        record.last_seen = service.now()
        return {"status": "observed"}

    @app.post("/internal/claim", dependencies=[Depends(worker)])
    def claim(body: Identity, db: DB):
        item = service.claim(db, body.worker_id)
        if item is None:
            return None
        job, attempt = item
        return {"job": service.serialize(db, job), "token": str(attempt.id)}

    @app.post("/internal/jobs/{job_id}/start", dependencies=[Depends(worker)])
    def start(job_id: UUID, body: Assignment, db: DB):
        service.start(db, job_id, body.worker_id, body.token)
        return {"status": "RUNNING"}

    @app.post("/internal/jobs/{job_id}/finish", dependencies=[Depends(worker)])
    def finish(job_id: UUID, body: Completion, db: DB):
        service.finish(db, job_id, body.worker_id, body.token, body.output, body.error)
        return {"status": "recorded"}

    return app
