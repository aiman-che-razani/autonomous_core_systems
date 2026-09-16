from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from greyqueue import service
from greyqueue.models import Attempt, Job, Worker
from greyqueue.recovery import detect_workers, recover_jobs


def worker(db, name="w", capacity=1, capabilities=None):
    db.add(
        Worker(
            id=name,
            capacity=capacity,
            capabilities=capabilities or ["sleep", "calculate_pi", "flaky"],
        )
    )
    db.flush()


def test_priority_capabilities_capacity_and_fifo(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db, capacity=2, capabilities=["sleep"])
        first = service.submit(db, "sleep", {"seconds": 0.0}, priority=0)
        high = service.submit(db, "sleep", {"seconds": 0.0}, priority=10)
        service.submit(db, "calculate_pi", {"iterations": 10}, priority=99)
        assert service.claim(db, "w", 0, policy="priority")[0].id == high.id
        assert service.claim(db, "w", 1, policy="fifo")[0].id == first.id
        with pytest.raises(service.Conflict):
            service.claim(db, "w", 2)


def test_submission_dedup_and_conflict(database):
    sessions, _ = database

    def submit(_):
        with sessions.begin() as db:
            return service.submit(db, "sleep", {"seconds": 0.0}, idempotency_key="same").id

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(submit, range(8)))) == 1
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.submit(db, "sleep", {"seconds": 1.0}, idempotency_key="same")


def test_queue_and_rate_admission(database):
    sessions, _ = database
    with sessions.begin() as db:
        service.submit(db, "sleep", {"seconds": 0.0}, queue_limit=1)
    with pytest.raises(service.Saturated), sessions.begin() as db:
        service.submit(db, "sleep", {"seconds": 0.0}, queue_limit=1)
    with pytest.raises(service.Saturated), sessions.begin() as db:
        service.submit(db, "sleep", {"seconds": 0.0}, submissions_per_minute=1)


def test_expiration_recovery_and_fencing(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        worker(db, "other")
        job = service.submit(db, "sleep", {"seconds": 0.0}, max_retries=2, retry_delay=0)
        job_id = job.id
        _, old = service.claim(db, "w")
        old_id = old.id
        service.start(db, job_id, "w", old_id)
        old.expires_at = service.now() - timedelta(seconds=1)
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.finish(db, job_id, "w", old_id, {"late": True}, None)
    with sessions.begin() as db:
        assert recover_jobs(db) == 1
    with sessions.begin() as db:
        new_job, new = service.claim(db, "other")
        assert new.fence == 2
        assert new_job.id == job_id
        service.start(db, job_id, "other", new.id)
        service.finish(db, job_id, "other", new.id, {"ok": True}, None)
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.renew(db, job_id, "w", old_id, 10)
    with sessions() as db:
        assert db.get(Attempt, old_id).outcome == "LEASE_EXPIRED"
        assert service.get_job(db, job_id).status == "SUCCEEDED"


def test_renewal_blocks_recovery(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        job = service.submit(db, "sleep", {"seconds": 0.0})
        _, attempt = service.claim(db, "w")
        service.renew(db, job.id, "w", attempt.id, 30)
        assert recover_jobs(db) == 0


def test_retry_history_and_dead_letter(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        job_id = service.submit(db, "sleep", {"seconds": 0.0}, max_retries=1, retry_delay=0).id
    for expected in ["RETRY_WAIT", "DEAD_LETTER"]:
        with sessions.begin() as db:
            job, attempt = service.claim(db, "w")
            service.start(db, job.id, "w", attempt.id)
            service.finish(db, job.id, "w", attempt.id, None, "temporary", True)
        with sessions() as db:
            assert service.get_job(db, job_id).status == expected
    with sessions() as db:
        attempts = list(db.scalars(select(Attempt).where(Attempt.job_id == job_id)))
        assert len(attempts) == 2
        assert all(a.error == "temporary" for a in attempts)


def test_schedule_dependency_and_drain(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        future = service.submit(
            db, "sleep", {"seconds": 0.0}, scheduled_at=service.now() + timedelta(hours=1)
        )
        parent = service.submit(db, "sleep", {"seconds": 0.0})
        child = service.submit(db, "sleep", {"seconds": 0.0}, priority=99, depends_on=parent.id)
        job, attempt = service.claim(db, "w", policy="priority")
        assert job.id == parent.id
        service.start(db, job.id, "w", attempt.id)
        service.finish(db, job.id, "w", attempt.id, {}, None)
    with sessions.begin() as db:
        assert service.claim(db, "w")[0].id == child.id
        db.get(Worker, "w").state = "DRAINING"
        db.flush()
        assert service.claim(db, "w") is None
        assert db.get(Job, future.id).status == "QUEUED"


def test_failure_detection_and_illegal_transition(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        w = db.get(Worker, "w")
        w.last_seen = service.now() - timedelta(seconds=7)
        db.flush()
        detect_workers(db, 6, 12)
        assert w.state == "SUSPECT"
        w.last_seen = service.now() - timedelta(seconds=13)
        db.flush()
        detect_workers(db, 6, 12)
        assert w.state == "DEAD"
        job = service.submit(db, "sleep", {"seconds": 0.0})
        with pytest.raises(service.Conflict):
            service.transition(db, job, "SUCCEEDED")


def test_claim_replay_same_request(database):
    sessions, _ = database
    token = uuid4()
    with sessions.begin() as db:
        worker(db)
        service.submit(db, "sleep", {"seconds": 0.0})
        first = service.claim(db, "w", claim_id=token)
    with sessions.begin() as db:
        second = service.claim(db, "w", claim_id=token)
        assert first[1].id == second[1].id
        with pytest.raises(service.Conflict):
            service.claim(db, "w", claim_id=uuid4())


def test_cached_worker_cannot_claim_after_drain(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        service.submit(db, "sleep", {"seconds": 0.0})
    with sessions.begin() as request:
        cached = request.get(Worker, "w")
        assert cached.state == "HEALTHY"
        with sessions.begin() as operator:
            operator.get(Worker, "w").state = "DRAINING"
        assert service.claim(request, "w") is None


def test_expired_unstarted_claim_is_recovered(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        job = service.submit(db, "sleep", {"seconds": 0.0}, max_retries=1, retry_delay=0)
        job_id = job.id
        _, attempt = service.claim(db, "w")
        attempt.expires_at = service.now() - timedelta(seconds=1)
    with sessions.begin() as db:
        recover_jobs(db)
    with sessions() as db:
        assert db.get(Job, job_id).status == "RETRY_WAIT"


def test_dependency_failure_propagates(database):
    sessions, _ = database
    with sessions.begin() as db:
        parent = service.submit(db, "sleep", {"seconds": 0.0})
        child = service.submit(db, "sleep", {"seconds": 0.0}, depends_on=parent.id)
        child_id = child.id
        service.cancel(db, parent.id)
    with sessions.begin() as db:
        recover_jobs(db)
    with sessions() as db:
        assert db.get(Job, child_id).status == "CANCELLED"


def test_exponential_delay_and_permanent_failure(database):
    sessions, _ = database
    with sessions.begin() as db:
        worker(db)
        service.submit(
            db, "sleep", {"seconds": 0.0}, max_retries=3, retry_delay=2, retry_jitter=False
        )
        job, attempt = service.claim(db, "w")
        job_id = job.id
        service.start(db, job.id, "w", attempt.id)
        service.finish(db, job.id, "w", attempt.id, None, "transient", True)
        assert 1.9 <= (job.available_at - attempt.finished_at).total_seconds() <= 2.1
        assert service.claim(db, "w") is None
        job.available_at = service.now() - timedelta(seconds=1)
    with sessions.begin() as db:
        job, attempt = service.claim(db, "w")
        service.start(db, job.id, "w", attempt.id)
        service.finish(db, job.id, "w", attempt.id, None, "permanent", False)
    with sessions() as db:
        assert db.get(Job, job_id).status == "FAILED"
