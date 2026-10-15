"""
Background job queue.

OCR is CPU-bound and slow (about 4.5 s per document here, and much more on a
multi-page scan), so the API must never do it inside a request. Jobs are queued,
picked up by a small pool of worker threads, and their progress is written to the
store so the UI can poll it.

This is the single-node form of the architecture in the brief: a broker
(RabbitMQ / Redis) plus Celery workers is the same code with `submit()` pushing
to a remote queue instead of a local one. The interface deliberately mirrors it.
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from typing import Any, Callable, Optional

from .store import Store

logger = logging.getLogger("workbench.jobs")


class JobQueue:
    """Thread-pool worker with persistent job records."""

    def __init__(self, store: Store, service: Any, workers: int = 2):
        self.store = store
        self.service = service
        self.workers = max(1, workers)
        self._queue: "queue.Queue[Optional[tuple[str, str, dict]]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for index in range(self.workers):
                thread = threading.Thread(target=self._worker, name=f"worker-{index}",
                                          daemon=True)
                thread.start()
                self._threads.append(thread)
            self._started = True
            logger.info("Job queue started with %d workers", self.workers)

    def stop(self) -> None:
        for _ in self._threads:
            self._queue.put(None)

    # --------------------------------------------------------------------- submit
    def submit(self, kind: str, params: Optional[dict[str, Any]] = None,
               detail: str = "", total: int = 0) -> str:
        self.start()
        job_id = self.store.create_job(kind, total=total, detail=detail)
        self._queue.put((job_id, kind, params or {}))
        return job_id

    # ---------------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, kind, params = item
            self.store.update_job(job_id, status="RUNNING", detail="running")
            try:
                handler = self._handler(kind)
                result = handler(job_id, params)
                self.store.update_job(job_id, status="DONE", result=result,
                                      detail=str(result.get("detail", "complete")),
                                      done=result.get("processed", 0),
                                      total=result.get("documents", 0) or 0)
            except Exception as exc:  # noqa: BLE001 — a failed job must not kill the worker
                logger.exception("Job %s (%s) failed", job_id, kind)
                self.store.update_job(job_id, status="FAILED", error=str(exc),
                                      detail=traceback.format_exc()[-400:])
            finally:
                self._queue.task_done()

    def _handler(self, kind: str) -> Callable[[str, dict], dict]:
        handlers = {
            "corpus_run": self._run_corpus,
            "process_uploads": self._process_uploads,
        }
        if kind not in handlers:
            raise ValueError(f"unknown job kind: {kind}")
        return handlers[kind]

    # -------------------------------------------------------------------- handlers
    def _run_corpus(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        def progress(done: int, total: int, detail: str) -> None:
            self.store.update_job(job_id, done=done, total=total,
                                  detail=f"{detail} ({done}/{total})")

        result = self.service.run_corpus(limit=params.get("limit"),
                                         progress=progress,
                                         reset=bool(params.get("reset")))
        processed = [
            {
                "invoice_id": record["invoice_id"],
                "invoice_number": record.get("invoice_number"),
                "vendor_name": record.get("vendor_name"),
                "decision": record.get("decision"),
                "risk_score": record.get("risk_score"),
                "risk_band": record.get("risk_band"),
                "amount": record.get("total_amount"),
                "correct": record.get("correct"),
            }
            for record in self.service.store.list_invoices(limit=1000, order="created_at DESC")
        ]
        return {
            "detail": f"{result['processed']}/{result['documents']} documents processed",
            "processed": result["processed"],
            "documents": result["documents"],
            "failed": result["failed"],
            "wall_seconds": result["wall_seconds"],
            "outcomes": result["summary"],
            "invoices": processed,
        }

    def _process_uploads(self, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
        from ap_intelligence.models import DocumentSource

        paths = params.get("paths") or []
        results = []
        for index, path in enumerate(paths, start=1):
            self.store.update_job(job_id, done=index - 1, total=len(paths),
                                  detail=f"{path} ({index}/{len(paths)})")
            record = self.service.process_path(path, source=DocumentSource.API_UPLOAD)
            results.append({"invoice_id": record["invoice_id"], "path": path,
                            "decision": record["decision"], "risk_score": record["risk_score"]})
        return {"detail": f"{len(results)} documents processed", "processed": len(results),
                "documents": len(paths), "invoices": results}
