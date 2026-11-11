from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from backend.db.database import connect
from backend.library import translations as tr

router = APIRouter(prefix="/api/library/translations", tags=["translations"])
_worker: tr.TranslationWorker | None = None


def set_worker(worker: tr.TranslationWorker | None):
    global _worker
    _worker = worker


class Scope(BaseModel):
    scope: Literal["selected", "recent", "all"]
    version_ids: list[str] = Field(default_factory=list, max_length=100000)
    limit: int = Field(default=100, ge=1, le=1000000)
    batch_size: int = Field(default=tr.DEFAULT_BATCH, ge=1, le=32)
    max_input_chars: int = Field(default=tr.DEFAULT_CHARS, ge=1000, le=64000)

    @model_validator(mode="after")
    def selected(self):
        if self.scope == "selected" and not self.version_ids:
            raise ValueError("Please select one or more paper versions")
        if any(not item or len(item) > 512 for item in self.version_ids):
            raise ValueError("Invalid version identifier")
        return self


class Automatic(BaseModel):
    enabled: bool
    batch_size: int | None = Field(default=None, ge=1, le=32)
    max_input_chars: int | None = Field(default=None, ge=1000, le=64000)


class CacheLookup(BaseModel):
    version_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(min_length=1, max_length=100)


def _call(fn: Callable, *args, **kwargs):
    try:
        with connect() as db:
            return fn(db, *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "所选论文版本或翻译任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(503, "宿主翻译模型暂不可用") from exc


def _notify(worker, enabled: bool | None = None):
    if worker:
        if enabled is not None and worker.on_auto_change:
            worker.on_auto_change(enabled)
        worker.notify()


@router.post("/lookup")
def lookup(body: CacheLookup):
    """Read saved fields for one displayed page, without scheduling work."""
    with connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='translation_fields'").fetchone():
            raise HTTPException(503, "翻译缓存尚未初始化")
        found = tr.read_cached_translations(db, ({"paper_version_id": key} for key in body.version_ids))
    return {"items": found}


@router.get("/provider")
def provider():
    return (
        _worker.provider_status()
        if _worker
        else {"available": False, "provider": None, "model": None, "reason": "MODEL_UNAVAILABLE"}
    )


@router.post("/estimate")
def estimate(body: Scope):
    return _call(tr.estimate, body.scope, body.version_ids, body.limit, body.batch_size, body.max_input_chars)


def _submit(body, priority=False):
    worker = _worker
    result = _call(
        tr.submit_job,
        body.scope,
        body.version_ids,
        body.limit,
        body.batch_size,
        body.max_input_chars,
        priority=priority,
        available=bool(worker and worker.available()),
    )
    _notify(worker)
    return result


@router.post("/jobs")
def submit(body: Scope):
    return _submit(body)


@router.get("/jobs")
def jobs(limit: int = Query(default=20, ge=1, le=100)):
    with connect() as db:
        return {
            "items": [
                tr.job_status(db, row[0])
                for row in db.execute(
                    "SELECT id FROM translation_jobs ORDER BY created_at DESC, id LIMIT ?", (limit,)
                )
            ]
        }


@router.get("/jobs/{job_id}")
def status(job_id: str):
    return _call(tr.job_status, job_id)


@router.post("/jobs/{job_id}/{action}")
def control(job_id: str, action: Literal["pause", "resume", "retry"]):
    worker = _worker
    result = _call(tr.control_job, job_id, action)
    _notify(worker, action != "pause" if job_id == tr.AUTO_JOB else None)
    return result


@router.get("/versions/{version_id:path}")
def version(version_id: str):
    return _call(tr.get_version, version_id)


@router.post("/versions/{version_id:path}")
def submit_version(version_id: str):
    return _submit(Scope(scope="selected", version_ids=[version_id], max_input_chars=64000), priority=True)


@router.get("/automatic")
def automatic():
    return _call(tr.automatic_status)


@router.put("/automatic")
def update_automatic(body: Automatic):
    worker = _worker
    result = _call(
        tr.set_automatic,
        body.enabled,
        body.batch_size,
        body.max_input_chars,
        available=bool(worker and worker.available()) if body.enabled else False,
    )
    _notify(worker, body.enabled)
    return result
