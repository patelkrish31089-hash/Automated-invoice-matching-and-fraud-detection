"""
Application service layer.

Everything the HTTP API needs, expressed as plain methods: process a document,
list and inspect invoices, run the fraud analytics, apply a clerk's correction.
The FastAPI module is a thin shell over this class, so the whole application can
be exercised (and tested) without a web server.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ap_intelligence.audit import AuditBlockchain
from ap_intelligence.config import PROJECT_ROOT, Settings
from ap_intelligence.extraction.table_parser import reconcile_line_items
from ap_intelligence.feedback.active_learning import (
    ActiveLearningScheduler,
    FeedbackStore,
    FieldCorrection,
    FieldCorrectionExtractor,
)
from ap_intelligence.fraud.benford import BENFORD_D1, BENFORD_D2, BenfordAnalyzer, benford_chart_data
from ap_intelligence.ingestion.email_parser import EmailInvoiceIngestor
from ap_intelligence.models import Decision, DocumentSource, Invoice
from ap_intelligence.payments import PaymentEngine
from ap_intelligence.pipeline.orchestrator import InvoicePipeline, PipelineContext

from . import serialization as ser
from .store import Store, utcnow

logger = logging.getLogger("workbench.service")

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _counts(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    """Tally a column across rows — used by the payment and rail summaries."""
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def extract_address(value: str) -> str:
    """Pull `user@host` out of `Name <user@host>` (or return the value unchanged)."""
    text = (value or "").strip()
    if "<" in text and ">" in text:
        return text[text.index("<") + 1:text.rindex(">")].strip()
    return text


def _as_date(value: Any):
    """Best-effort ISO date parse for payload strings."""
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()[:19]).date()
        except ValueError:
            return None
    return None


class AppService:
    """Owns the pipeline, the ERP sandbox, the store and the fraud baselines."""

    def __init__(self, root: Path | str = PROJECT_ROOT, db_path: Optional[Path | str] = None,
                 settings: Optional[Settings] = None, outputs_dir: Optional[Path | str] = None):
        self.root = Path(root)
        self.settings = settings or Settings()
        if outputs_dir is not None:
            # The audit artefacts (trail, blocks, anchors) have exactly one writer per
            # outputs directory. A second service instance — a test, a tenant, a
            # replay — must be given its own directory, or it overwrites the first
            # instance's chain with a different one.
            self.settings.outputs_dir = str(outputs_dir)
            Path(outputs_dir).mkdir(parents=True, exist_ok=True)
            # ...and its own anchor log, or a test instance would append rehearsal
            # receipts to the production anchor file
            if getattr(self.settings, "anchor_url", ""):
                self.settings.anchor_url = f"file://{Path(outputs_dir) / 'anchors.jsonl'}"
        self.store = Store(db_path or (self.root / "data" / "workbench.db"))
        self.ctx = PipelineContext(settings=self.settings)
        self.pipeline = InvoicePipeline(self.ctx)
        self.erp = self.ctx.erp
        self.feedback = FeedbackStore(self.root / "outputs" / "feedback" / "corrections.jsonl")
        self.scheduler = ActiveLearningScheduler(self.feedback)
        self.email_ingestor = EmailInvoiceIngestor(
            trusted_domains={"northwind-supply.com", "meridian-office.com", "silicon-systems.com"},
            output_dir=self.root / "data" / "generated" / "attachments",
        )
        self.upload_dir = self.root / "data" / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.root / "data" / "samples" / "invoice_images"
        self.labels = self._load_labels()
        self.seeded_erp = False
        self.seeded_history = False
        self.seed_error: Optional[str] = None
        self.payment_engine = PaymentEngine(outputs_dir=self.root / "outputs")
        self._last_payment_run: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ seeding
    def _load_labels(self) -> dict[str, dict[str, Any]]:
        path = self.root / "data" / "samples" / "labels.json"
        if not path.exists():
            return {}
        try:
            records = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for record in records:
            out[Path(record["image"]).stem if "image" in record else record["doc_id"]] = {
                "doc_id": record.get("doc_id"),
                "expected_decision": (record.get("label") or {}).get("expected_decision"),
                "category": (record.get("label") or {}).get("category"),
                "notes": (record.get("label") or {}).get("notes"),
            }
        return out

    def seed_erp(self) -> dict[str, Any]:
        seed_path = self.root / "data" / "erp_seed" / "erp_seed.json"
        if not seed_path.exists():
            self.seed_error = f"ERP seed not found at {seed_path}"
            return {"error": self.seed_error}
        payload = json.loads(seed_path.read_text())
        counts = self.erp.seed_from_json(payload) if hasattr(self.erp, "seed_from_json") else {}
        self.seeded_erp = True
        return counts

    def seed_baselines(self) -> dict[str, Any]:
        """
        Load the historical invoice corpus so the fraud engines have a baseline.

        The history ships inside the ERP seed file (`history` key), which is how
        the CLI demo consumes it too — one artifact, one source of truth.
        """
        path = self.root / "data" / "erp_seed" / "erp_seed.json"
        if not path.exists():
            return {"error": f"ERP seed not found at {path}"}
        payload = json.loads(path.read_text())
        invoices = self._history_to_invoices({"invoices": payload.get("history", [])})
        po_dates = {r["po_number"]: date.fromisoformat(r["issued_on"])
                    for r in payload.get("purchase_orders", [])
                    if r.get("po_number") and r.get("issued_on")}
        approvals = {r["po_number"]: r.get("approved_by", "")
                     for r in payload.get("purchase_orders", []) if r.get("po_number")}
        self.pipeline.seed_history(invoices, po_dates, approvals)
        self.seeded_history = True
        summary = {
            "historical_invoices": len(invoices),
            "vendors": len({(i.vendor_name or "").lower() for i in invoices}),
            "isolation_backend": getattr(self.ctx.isolation, "_backend", "unknown"),
        }
        self.store.set_meta("baseline_summary", summary)
        return summary

    @staticmethod
    def _history_to_invoices(payload: dict[str, Any]) -> list[Invoice]:
        from ap_intelligence.models import LineItem
        out = []
        for record in payload.get("invoices", []):
            inv = Invoice(invoice_id=record.get("invoice_id", ""),
                          invoice_number=record.get("invoice_number", ""))
            inv.vendor_name = record.get("vendor_name")
            inv.vendor_tax_id = record.get("vendor_tax_id")
            inv.po_number = record.get("po_number")
            inv.currency = record.get("currency", "USD")
            if record.get("invoice_date"):
                inv.invoice_date = date.fromisoformat(record["invoice_date"])
            inv.subtotal = record.get("subtotal")
            inv.tax_amount = record.get("tax_amount")
            inv.total_amount = record.get("total_amount")
            inv.line_items = [
                LineItem(line_no=index,
                         description=r.get("description", ""),
                         quantity=r.get("quantity", r.get("qty", 0.0)),
                         unit_price=r.get("unit_price", 0.0),
                         amount=r.get("amount", 0.0),
                         unit=r.get("unit", "EA"),
                         part_number=r.get("part_number") or r.get("part"))
                for index, r in enumerate(record.get("line_items", []), start=1)
            ]
            out.append(inv)
        return out

    def bootstrap(self) -> dict[str, Any]:
        """Seed ERP + baselines. Safe to call repeatedly (both are idempotent)."""
        status: dict[str, Any] = {"erp": {}, "baselines": {}}
        try:
            status["erp"] = self.seed_erp()
        except Exception as exc:  # noqa: BLE001
            logger.exception("ERP seeding failed")
            status["erp"] = {"error": str(exc)}
        try:
            status["baselines"] = self.seed_baselines()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Baseline seeding failed")
            status["baselines"] = {"error": str(exc)}
        return status

    # --------------------------------------------------------------- processing
    def process_path(self, path: str | Path, source: DocumentSource = DocumentSource.API_UPLOAD,
                     expected_label: Optional[str] = None,
                     label_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run one document through the pipeline and persist the result."""
        result = self.pipeline.process_file(path, source=source)
        return self._persist(result, path, expected_label, label_meta)

    def _persist(self, result, path: str | Path, expected_label: Optional[str],
                 label_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        inv = result.invoice
        image_url = f"/api/invoices/{inv.invoice_id}/image"
        po, grns = self._erp_context(inv.po_number)
        detail = ser.result_to_detail(result, image_url=image_url,
                                      expected_label=expected_label, po=po, grns=grns)
        detail["document_path"] = self._relative(Path(path))
        detail["label"] = label_meta or {}
        # attach the PO/GRN context used for matching, for the audit trail
        detail["erp_context"] = {
            "po_resolved": po is not None,
            "grns_found": len(grns),
            "vendor_mastered": bool(
                getattr(self.erp, "get_vendor_by_name", lambda *_: None)(inv.vendor_name or "")
            ),
        }
        summary = ser.summary_from_detail(detail)
        correct = None
        if expected_label:
            correct = int(self._decision_agrees(expected_label, detail["decision"]))
        record = {
            "invoice_id": inv.invoice_id,
            "invoice_number": inv.invoice_number,
            "vendor_name": inv.vendor_name,
            "po_number": inv.po_number,
            "currency": inv.currency,
            "source": detail.get("source"),
            "document_path": self._relative(Path(path)),
            "expected_label": expected_label,
            "correct": correct,
            "review_status": "OPEN",
            "created_at": utcnow(),
            "processing_ms": detail.get("processing_ms"),
            "cost_usd": detail.get("cost_usd"),
            "payload": detail,
            **summary,
        }
        self.store.upsert_invoice(record)
        # Full-width SHA-256 over a canonical serialisation. An external verifier
        # recomputes exactly this, so the serialisation is part of the contract.
        record_digest = hashlib.sha256(
            json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()
        self.store.append_ledger(
            inv.invoice_id, detail["decision"], detail,
            previous_hash=self.store.get_meta("last_ledger_hash", "GENESIS"),
            record_hash=record_digest,
        )
        self.store.set_meta("last_ledger_hash", record_digest)
        return self.store.get_invoice(inv.invoice_id) or record

    def _erp_context(self, po_number: Optional[str]):
        if not po_number:
            return None, []
        po = self.erp.get_purchase_order(po_number)
        grns = self.erp.get_grns_for_po(po_number) if po else []
        return po, grns

    @staticmethod
    def _decision_agrees(expected: str, actual: str) -> bool:
        """Labels collapse PENDING_REVIEW/QUARANTINED/REJECTED into 'stop the invoice'."""
        stopped = {"PENDING_REVIEW", "QUARANTINED", "REJECTED"}
        return expected == actual or (expected in stopped and actual in stopped)

    # ------------------------------------------------------------------ corpus
    def corpus_documents(self) -> list[Path]:
        return sorted(self.images_dir.glob("*.png"))

    blockchain: Optional["AuditBlockchain"] = None

    def reset_state(self, keep_baselines: bool = True) -> dict[str, Any]:
        """
        Clear everything the workbench has learned from processed documents.

        The duplicate store has to be cleared too, not just the database: it
        keeps a vector of every invoice seen, so re-running the corpus against a
        stale store would flag the *original* of a duplicate pair as a copy of
        its own twin. In production that behaviour is correct (a resubmitted
        invoice should be caught); for a controlled re-run it must start clean.
        """
        self.store.reset()
        if self.blockchain is not None:
            self.blockchain.reset()
        store = self.ctx.duplicate_store
        store.records.clear()
        if hasattr(store, "_reindex"):
            store._reindex()
        if hasattr(store, "save"):
            store.save()
        self.pipeline = InvoicePipeline(self.ctx)          # fresh rolling history
        return self.bootstrap() if keep_baselines else {}

    def run_corpus(self, limit: Optional[int] = None,
                   progress: Optional[Callable[[int, int, str], None]] = None,
                   reset: bool = False) -> dict[str, Any]:
        """Process the bundled invoice corpus (the demo dataset)."""
        if reset:
            self.reset_state()
        documents = self.corpus_documents()
        if limit:
            documents = documents[:limit]
        started = time.perf_counter()
        processed, failed = 0, []
        for index, path in enumerate(documents, start=1):
            if progress:
                progress(index - 1, len(documents), path.name)
            doc_id = path.stem
            meta = self.labels.get(doc_id, {})
            try:
                self.process_path(path, source=DocumentSource.PORTAL,
                                  expected_label=meta.get("expected_decision"),
                                  label_meta=meta)
                processed += 1
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the batch
                logger.exception("Failed to process %s", path.name)
                failed.append({"document": path.name, "error": str(exc)})
        if progress:
            progress(len(documents), len(documents), "done")
        # seal this run's records into an anchored block and export the files an
        # external auditor verifies offline
        try:
            self.seal_audit_blocks()
        except Exception as exc:  # noqa: BLE001 — never fail a payment run over auditing
            logger.warning("Block sealing failed: %s", exc)
        summary = self.store.summary()
        return {
            "processed": processed,
            "failed": failed,
            "documents": len(documents),
            "wall_seconds": round(time.perf_counter() - started, 1),
            "summary": summary,
        }

    # ------------------------------------------------------------------ ingest
    def ingest_upload(self, filename: str, data: bytes,
                      source: DocumentSource = DocumentSource.PORTAL) -> dict[str, Any]:
        safe = "".join(ch for ch in filename if ch.isalnum() or ch in "._-") or "upload.bin"
        digest = hashlib.sha256(data).hexdigest()[:12]
        path = self.upload_dir / f"{digest}_{safe}"
        path.write_bytes(data)
        record = self.process_path(path, source=source)
        return {"stored_at": str(path.relative_to(self.root)), "invoice_id": record["invoice_id"],
                "decision": record["decision"], "risk_score": record["risk_score"]}

    def ingest_email(self, raw: bytes) -> dict[str, Any]:
        """Parse a raw RFC-822 message, then process each accepted attachment."""
        envelope = self.email_ingestor.parse(raw)
        results = []
        for attachment in envelope.attachments:
            content = attachment.get("content")
            if not content:
                continue
            name = attachment["filename"]
            safe = "".join(ch for ch in name if ch.isalnum() or ch in "._-") or "attachment.bin"
            path = self.upload_dir / f"{attachment['sha256'][:12]}_{safe}"
            path.write_bytes(content)
            try:
                record = self.process_path(path, source=DocumentSource.EMAIL)
                results.append({"filename": name, "mime": attachment["mime"],
                                "invoice_id": record["invoice_id"],
                                "decision": record["decision"],
                                "risk_score": record["risk_score"],
                                "vendor_name": record["vendor_name"]})
            except Exception as exc:  # noqa: BLE001
                results.append({"filename": name, "error": str(exc)})
        envelope_dict = envelope.to_dict()
        # the UI reads from/to plus accepted/rejected counters
        envelope_dict.setdefault("from", envelope_dict.get("sender"))
        envelope_dict.setdefault("to", envelope_dict.get("recipient"))
        envelope_dict.setdefault("from_display", envelope_dict.get("sender_display")
                                 or envelope_dict.get("sender"))
        processed = [r for r in results if "invoice_id" in r]
        failed = [r for r in results if "invoice_id" not in r]
        return {
            "envelope": envelope_dict,
            "injection_flags": envelope.injection_flags,
            "attachments_accepted": len(envelope.attachments),
            "accepted": len(processed),
            "rejected": len(failed),
            "processed": results,
        }

    def review_history(self, invoice_id: str) -> list[dict[str, Any]]:
        """Merge the clerk decision trail with field corrections into one timeline."""
        events: list[dict[str, Any]] = []
        for row in self.store.list_reviews(invoice_id):
            events.append({
                "kind": "review",
                "at": row.get("created_at"),
                "actor": row.get("clerk"),
                "action": row.get("action"),
                "previous": row.get("previous"),
                "detail": (f"decision {row.get('previous') or 'OPEN'} -> {row.get('action')}"
                           if row.get("previous") else f"decision -> {row.get('action')}"),
            })
        for row in self.store.list_corrections(limit=1000):
            if row.get("invoice_id") != invoice_id:
                continue
            events.append({
                "kind": "correction",
                "at": row.get("created_at"),
                "actor": row.get("clerk"),
                "action": "CORRECTION",
                "field": row.get("field"),
                "detail": f"{row.get('field')}: {row.get('predicted')} -> {row.get('corrected')}",
                "confidence": row.get("confidence"),
            })
        events.sort(key=lambda e: e.get("at") or "")
        return events

    # ------------------------------------------------------------------ review
    def set_review(self, invoice_id: str, status: str, clerk: str = "ap_clerk") -> dict[str, Any]:
        if status not in ("APPROVED", "REJECTED", "OPEN", "ESCALATED"):
            raise ValueError(f"unsupported review status: {status}")
        if not self.store.set_review(invoice_id, status, clerk):
            raise KeyError(invoice_id)
        return self.store.get_invoice(invoice_id)

    def apply_corrections(self, invoice_id: str, corrections: dict[str, Any],
                          clerk: str = "ap_clerk") -> dict[str, Any]:
        """
        Apply a clerk's field corrections, then re-assess the invoice.

        OCR is not repeated — matching, fraud detection and routing are re-run on
        the corrected data, which is what makes the second opinion cheap enough to
        give to every reviewer.
        """
        record = self.store.get_invoice(invoice_id)
        if record is None:
            raise KeyError(invoice_id)
        detail = record["payload"]
        original = ser.invoice_from_payload(detail)      # mutated as corrections apply
        original_invoice = ser.invoice_from_payload(detail)  # model's original output

        applied = []
        for field, value in corrections.items():
            if field not in {name for name, _ in ser.DISPLAY_FIELDS} | {
                    "subtotal", "tax", "shipping", "total"}:
                continue
            predicted = self._field_value(detail, field)
            if str(predicted) == str(value):
                continue
            self._apply_field(original, field, value)
            self.store.add_correction(invoice_id, field, predicted, value,
                                      confidence=(detail.get("field_confidences") or {}).get(field),
                                      clerk=clerk)
            applied.append({"field": field, "predicted": predicted, "corrected": value})

        if not applied:
            return {"changed": False, "invoice": record}

        # corrections are training data for the next extraction model
        try:
            diff_input = dict(corrections)
            if "tax" in diff_input:
                diff_input.setdefault("tax_amount", diff_input["tax"])
            if "total" in diff_input:
                diff_input.setdefault("total_amount", diff_input["total"])
            corrects = FieldCorrectionExtractor.diff(original_invoice, diff_input, clerk)
            if corrects:
                self.feedback.record_bulk(corrects)
        except Exception as exc:  # noqa: BLE001 — feedback must never break the review flow
            logger.warning("Could not record feedback: %s", exc)

        reassessment = self.pipeline.reassess(original)
        po, grns = self._erp_context(original.po_number)
        updated = self._payload_from_assessment(original, detail, reassessment, po, grns)
        summary = ser.summary_from_detail(updated)
        self.store.set_payload(invoice_id, updated, summary)
        updated_record = self.store.get_invoice(invoice_id)
        return {
            "changed": True,
            "corrections": applied,
            "reassessed": {
                "decision_before": detail.get("decision"),
                "decision_after": updated.get("decision"),
                "risk_before": (detail.get("fraud") or {}).get("risk_score"),
                "risk_after": (updated.get("fraud") or {}).get("risk_score"),
            },
            "invoice": updated_record,
        }

    @staticmethod
    def _field_value(detail: dict[str, Any], field: str) -> Any:
        if field in ("subtotal", "tax", "shipping", "total"):
            return (detail.get("totals") or {}).get(field)
        return detail.get(field)

    @staticmethod
    def _apply_field(invoice: Invoice, field: str, value: Any) -> None:
        if field in ("subtotal", "tax", "shipping", "total"):
            mapping = {"subtotal": "subtotal", "tax": "tax_amount",
                       "shipping": "shipping", "total": "total_amount"}
            try:
                setattr(invoice, mapping[field], float(str(value).replace(",", "").strip()))
            except (TypeError, ValueError):
                pass
            return
        if field in ("invoice_date", "due_date"):
            try:
                setattr(invoice, field, date.fromisoformat(str(value)[:10]))
            except ValueError:
                return
            return
        if field == "po_number":
            invoice.po_number = str(value).strip() or None
            return
        setattr(invoice, field, str(value).strip() or None)

    def _payload_from_assessment(self, invoice: Invoice, previous: dict[str, Any],
                                 reassessment: dict[str, Any],
                                 po, grns) -> dict[str, Any]:
        """Rebuild the detail payload after a re-assessment, preserving provenance."""
        match = reassessment["match"]
        fraud = reassessment["fraud"]
        outcome = reassessment["outcome"]
        updated = dict(previous)
        updated.update({
            "vendor_name": invoice.vendor_name,
            "vendor_tax_id": invoice.vendor_tax_id,
            "vendor_bank_account": invoice.vendor_bank_account,
            "invoice_number": invoice.invoice_number,
            "po_number": invoice.po_number,
            "currency": invoice.currency,
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "header_fields": ser.invoice_header_fields(invoice),
            "totals": ser.invoice_totals(invoice),
            "line_items": ser.line_items_to_payload(invoice),
            "line_item_reconciles": (invoice.subtotal is None
                                     or abs(invoice.line_item_sum - invoice.subtotal) <= 0.05),
            "match": ser.match_to_payload(match, po, grns),
            "fraud": ser.fraud_to_payload(fraud),
            "decision": outcome.decision.value,
            "decision_reason": outcome.reason,
            "routing_target": outcome.routing_target,
            "priority": outcome.priority,
            "field_confidences": {k: v for k, v in invoice.field_confidences.items()
                                  if isinstance(v, (int, float)) and not isinstance(v, bool)},
            "field_boxes": invoice.field_boxes,
        })
        history = list(updated.get("review_history") or [])
        history.append({
            "at": utcnow(),
            "action": "REASSESSED_AFTER_CORRECTION",
            "decision_before": previous.get("decision"),
            "decision_after": outcome.decision.value,
            "risk_before": (previous.get("fraud") or {}).get("risk_score"),
            "risk_after": fraud.risk_score,
        })
        updated["review_history"] = history
        return updated

    # --------------------------------------------------------------- analytics
    def analytics_overview(self) -> dict[str, Any]:
        summary = self.store.summary()
        decisions = self.store.decision_counts()
        invoices = self.store.list_invoices(limit=1000)
        findings: dict[str, int] = {}
        severities: dict[str, int] = {}
        engines: dict[str, float] = {}
        for record in invoices:
            payload = record.get("payload") or {}
            for finding in (payload.get("fraud") or {}).get("findings", []):
                findings[finding["type"]] = findings.get(finding["type"], 0) + 1
                severities[finding["severity"]] = severities.get(finding["severity"], 0) + 1
            for engine, score in ((payload.get("fraud") or {}).get("engine_scores") or {}).items():
                engines[engine] = max(engines.get(engine, 0.0), float(score or 0.0))
        labelled = summary.get("labelled") or 0
        correct = summary.get("correct") or 0
        total = summary.get("total") or 0
        return {
            "summary": {
                "invoices": total,
                "value_processed": round(summary.get("value") or 0.0, 2),
                "recoverable": round(summary.get("recoverable") or 0.0, 2),
                "compute_cost": round(summary.get("cost") or 0.0, 4),
                "manual_equivalent": round(15.0 * total, 2),
                "cost_per_invoice": round((summary.get("cost") or 0.0) / total, 5) if total else 0.0,
                "avg_document_confidence": round(summary.get("conf") or 0.0, 4),
                "avg_risk_score": round(summary.get("risk") or 0.0, 4),
                "avg_processing_ms": round((summary.get("ms") or 0.0) / total, 1) if total else 0.0,
                "open_review": summary.get("open_review") or 0,
                "straight_through_rate": round(decisions.get("AUTO_APPROVED", 0) / total, 4) if total else 0.0,
                "decision_agreement": round(correct / labelled, 4) if labelled else None,
                "labelled_documents": labelled,
            },
            "decisions": decisions,
            "risk_bands": self._risk_band_counts(invoices),
            "findings_by_type": dict(sorted(findings.items(), key=lambda kv: -kv[1])),
            "findings_by_severity": severities,
            "engine_peak_scores": {k: round(v, 4) for k, v in engines.items()},
        }

    @staticmethod
    def _risk_band_counts(invoices: Iterable[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in invoices:
            band = record.get("risk_band") or "LOW"
            out[band] = out.get(band, 0) + 1
        return out

    def dashboard(self) -> dict[str, Any]:
        """
        One payload with everything the dashboard renders.

        The UI could stitch this together from a dozen endpoints, but a single
        coherent snapshot means the charts always agree with each other and the
        browser makes one request instead of eleven.
        """
        overview = self.analytics_overview()
        invoices = self.store.list_invoices(limit=1000)
        summary = overview["summary"]

        # value-vs-risk scatter: shows at a glance whether the risk model separates
        # genuinely risky spend from ordinary spend
        scatter = []
        for record in invoices:
            amount = record.get("total_amount")
            if amount is None:
                continue
            scatter.append({
                "invoice_id": record["invoice_id"],
                "label": record.get("invoice_number") or record["invoice_id"],
                "vendor": record.get("vendor_name"),
                "amount": round(float(amount), 2),
                "risk": round(float(record.get("risk_score") or 0.0), 4),
                "decision": record.get("decision"),
                "band": record.get("risk_band") or "LOW",
                "recoverable": round(float(record.get("recoverable") or 0.0), 2),
            })

        # where the time goes, averaged over every processed document
        stage_totals: dict[str, list[float]] = {}
        for record in invoices:
            for step in ((record.get("payload") or {}).get("trace") or []):
                stage = step.get("stage")
                if stage:
                    stage_totals.setdefault(stage, []).append(float(step.get("ms") or 0.0))
        stage_timings = [
            {"stage": stage,
             "mean_ms": round(sum(values) / len(values), 2),
             "total_ms": round(sum(values), 2),
             "samples": len(values)}
            for stage, values in stage_totals.items()
        ]
        stage_timings.sort(key=lambda row: -row["mean_ms"])

        # every finding across the book, strongest first
        top_findings = []
        for record in invoices:
            for finding in ((record.get("payload") or {}).get("fraud") or {}).get("findings", []):
                top_findings.append({
                    "invoice_id": record["invoice_id"],
                    "vendor_name": record.get("vendor_name"),
                    "invoice_number": record.get("invoice_number"),
                    "type": finding["type"],
                    "severity": finding["severity"],
                    "engine": finding["engine"],
                    "message": finding["message"],
                    "recoverable": finding.get("recoverable_amount", 0.0),
                })
        severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        top_findings.sort(key=lambda f: (severity_rank.get(f["severity"], 9), -f["recoverable"]))

        throughput = [
            {"job": job["id"], "status": job["status"],
             "processed": job.get("done") or 0,
             "documents": job.get("total") or 0,
             "seconds": (job.get("result") or {}).get("wall_seconds"),
             "at": job.get("finished_at") or job.get("submitted_at")}
            for job in self.store.list_jobs(limit=8)
            if job["kind"] == "corpus_run" and job["status"] == "DONE"
        ]

        decisions = overview["decisions"]
        total = summary["invoices"] or 1
        auto = decisions.get("AUTO_APPROVED", 0)
        return {
            "generated_at": utcnow(),
            "summary": summary,
            "decisions": decisions,
            "risk_bands": overview["risk_bands"],
            "findings_by_type": overview["findings_by_type"],
            "findings_by_severity": overview["findings_by_severity"],
            "engine_scores": overview["engine_peak_scores"],
            "value_by_band": self.fraud_timeline(),
            "scatter": scatter,
            "stage_timings": stage_timings,
            "top_findings": top_findings[:40],
            "throughput": throughput,
            "recent": [
                {
                    "invoice_id": r["invoice_id"],
                    "invoice_number": r.get("invoice_number"),
                    "vendor_name": r.get("vendor_name"),
                    "total_amount": r.get("total_amount"),
                    "decision": r.get("decision"),
                    "risk_score": r.get("risk_score"),
                    "risk_band": r.get("risk_band"),
                    "document_confidence": r.get("document_confidence"),
                    "review_status": r.get("review_status"),
                    "created_at": r.get("created_at"),
                    "correct": r.get("correct"),
                    "source": r.get("source") or "corpus",
                }
                for r in invoices[:8]
            ],
            "targets": {
                "straight_through_rate": 0.60,
                "decision_agreement": 0.95,
                "cost_per_invoice": 0.50,
                "manual_cost_per_invoice": 15.0,
                "review_clearance": round(
                    1 - (summary.get("open_review") or 0) / total, 4) if total else 1.0,
            },
            "value_capture": self.value_capture(),
            "erp": {
                "posted_invoices": auto,
                "posted_value": round(sum(
                    float(r.get("total_amount") or 0) for r in invoices
                    if r.get("decision") == "AUTO_APPROVED"), 2),
            },
        }

    def fraud_timeline(self) -> list[dict[str, Any]]:
        """Value and count by risk band — what the CFO's slide shows."""
        invoices = self.store.list_invoices(limit=1000)
        buckets: dict[str, dict[str, float]] = {}
        for record in invoices:
            band = record.get("risk_band") or "LOW"
            bucket = buckets.setdefault(band, {"count": 0, "value": 0.0, "recoverable": 0.0})
            bucket["count"] += 1
            bucket["value"] += float(record.get("total_amount") or 0.0)
            bucket["recoverable"] += float(record.get("recoverable") or 0.0)
        return [{"band": band, **{k: round(v, 2) for k, v in data.items()}}
                for band, data in buckets.items()]

    def benford_analysis(self, digits: int = 1) -> dict[str, Any]:
        """First-digit (or first-two-digit) distribution over every amount we've seen."""
        analyzer = BenfordAnalyzer(self.settings)
        amounts: list[float] = []
        for record in self.store.list_invoices(limit=1000):
            payload = record.get("payload") or {}
            totals = payload.get("totals") or {}
            if totals.get("total"):
                amounts.append(float(totals["total"]))
            for item in payload.get("line_items") or []:
                if item.get("amount"):
                    amounts.append(float(item["amount"]))
        for invoice in self.pipeline.history:
            if invoice.total_amount:
                amounts.append(float(invoice.total_amount))
        if digits == 2:
            from collections import Counter
            from ap_intelligence.fraud.benford import mean_absolute_deviation
            observed = Counter()
            for amount in amounts:
                text = f"{abs(amount):.2f}".replace(".", "").lstrip("0")
                if len(text) >= 2:
                    observed[int(text[:2])] += 1
            n = sum(observed.values())
            expected = {int(k): v for k, v in BENFORD_D2.items()}
            rows = [{"digit": d, "observed": observed.get(d, 0) / n if n else 0.0,
                     "expected": expected.get(d, 0.0), "count": observed.get(d, 0)}
                    for d in sorted(expected)]
            return {"digits": 2, "samples": n, "rows": rows,
                    "mad": round(mean_absolute_deviation(observed, expected, n), 5) if n else None,
                    "conformity": "n/a"}
        result = analyzer.analyse_amounts(amounts)
        payload = result.to_dict()
        payload["chart"] = benford_chart_data(amounts)
        payload["samples"] = len(amounts)
        payload["source"] = {
            "processed_invoices": self.store.summary().get("total", 0),
            "historical_invoices": len(self.pipeline.history),
        }
        return payload

    def duplicate_analysis(self) -> dict[str, Any]:
        """Cluster stored invoices that look like the same submission."""
        from ap_intelligence.fraud.duplicates import normalise_invoice_number
        invoices = self.store.list_invoices(limit=1000)
        clusters: dict[str, list[dict[str, Any]]] = {}
        for record in invoices:
            number = record.get("invoice_number") or "?"
            key = f"{normalise_invoice_number(number)}|{round(float(record.get('total_amount') or 0), 2)}"
            clusters.setdefault(key, []).append({
                "invoice_id": record["invoice_id"],
                "invoice_number": number,
                "vendor_name": record.get("vendor_name"),
                "amount": record.get("total_amount"),
                "decision": record.get("decision"),
                "risk_band": record.get("risk_band"),
                "created_at": record.get("created_at"),
            })
        duplicates = [
            {"key": key, "count": len(rows), "total_at_stake": round(
                sum(float(r["amount"] or 0) for r in rows[1:]), 2), "invoices": rows}
            for key, rows in clusters.items() if len(rows) > 1
        ]
        duplicates.sort(key=lambda c: -c["total_at_stake"])
        semantic = []
        for record in invoices:
            for finding in ((record.get("payload") or {}).get("fraud") or {}).get("findings", []):
                if "DUPLICATE" in finding["type"]:
                    semantic.append({
                        "invoice_id": record["invoice_id"],
                        "invoice_number": record.get("invoice_number"),
                        "vendor_name": record.get("vendor_name"),
                        "type": finding["type"],
                        "severity": finding["severity"],
                        "message": finding["message"],
                        "evidence": finding.get("evidence", {}),
                        "amount": record.get("total_amount"),
                    })
                    break
        return {"exact_or_number_clusters": duplicates,
                "flagged_by_detector": semantic,
                "store_size": len(getattr(self.ctx.duplicate_store, "records", {}) or {})}

    def behavioural_analysis(self) -> dict[str, Any]:
        """Isolation-forest view: which invoices sit far from their vendor's norm."""
        rows = []
        for record in self.store.list_invoices(limit=1000):
            payload = record.get("payload") or {}
            findings = [f for f in ((payload.get("fraud") or {}).get("findings") or [])
                        if f["engine"] in ("isolation_forest", "isolation")
                        or f["type"] in ("BEHAVIOURAL_ANOMALY",)]
            if findings:
                top = max(findings, key=lambda f: f.get("score") or 0)
                rows.append({
                    "invoice_id": record["invoice_id"],
                    "invoice_number": record.get("invoice_number"),
                    "vendor_name": record.get("vendor_name"),
                    "amount": record.get("total_amount"),
                    "severity": top["severity"],
                    "score": top["score"],
                    "message": top["message"],
                    "evidence": top.get("evidence", {}),
                })
        rows.sort(key=lambda r: -(r.get("score") or 0))
        profiles = []
        for vendor, profile in (self.ctx.isolation.vendor_profiles or {}).items():
            data = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
            profiles.append({"vendor_name": vendor, **data})
        profiles.sort(key=lambda p: -(p.get("invoice_count") or 0))
        backend = getattr(self.ctx.isolation, "_backend", "unknown")
        return {"anomalies": rows, "vendor_profiles": profiles[:50], "backend": backend,
                "vendors_profiled": len(profiles)}

    def graph_analysis(self) -> dict[str, Any]:
        """
        The collusion network as nodes and edges — vendors, employees, bank
        accounts and addresses, with the links that create conflicts of interest.
        """
        vendors = self.erp.list_vendors() if hasattr(self.erp, "list_vendors") else []
        employees = self.erp.list_employees() if hasattr(self.erp, "list_employees") else []
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []

        def add_node(node_id: str, label: str, kind: str, meta: dict[str, Any] | None = None):
            nodes.setdefault(node_id, {"id": node_id, "label": label, "kind": kind,
                                       "meta": meta or {}})

        bank_index: dict[str, list[tuple[str, str]]] = {}
        address_index: dict[str, list[tuple[str, str]]] = {}

        for vendor in vendors:
            # ERP rows follow the SAP-ish column names: name / master_status / address
            name = vendor.get("name") or vendor.get("vendor_name") or "?"
            vid = f"vendor:{vendor.get('vendor_id') or name}"
            add_node(vid, name, "vendor",
                     {"tax_id": vendor.get("tax_id"),
                      "status": vendor.get("master_status") or vendor.get("status"),
                      "payment_terms": vendor.get("payment_terms"),
                      "address": vendor.get("address")})
            account = (vendor.get("bank_account") or "").strip()
            if account:
                nid = f"bank:{account}"
                add_node(nid, f"Account ****{account[-4:]}", "bank", {"account": account})
                edges.append({"source": vid, "target": nid, "kind": "REMITS_TO"})
                bank_index.setdefault(account, []).append((vid, name))
            address = (vendor.get("address") or "").strip().lower()
            if address:
                nid = f"address:{address}"
                add_node(nid, vendor.get("address", ""), "address")
                edges.append({"source": vid, "target": nid, "kind": "REGISTERED_AT"})
                address_index.setdefault(address, []).append((vid, name))

        for employee in employees:
            eid = f"employee:{employee.get('employee_id') or employee.get('name')}"
            add_node(eid, employee.get("name", "?"), "employee",
                     {"department": employee.get("department"),
                      "home_address": employee.get("home_address")})
            account = (employee.get("bank_account") or "").strip()
            if account:
                nid = f"bank:{account}"
                add_node(nid, f"Account ****{account[-4:]}", "bank", {"account": account})
                edges.append({"source": eid, "target": nid, "kind": "PAID_INTO"})
                bank_index.setdefault(account, []).append((eid, employee.get("name", "?")))
            address = (employee.get("address") or "").strip().lower()
            if address:
                nid = f"address:{address}"
                add_node(nid, employee.get("address", ""), "address")
                edges.append({"source": eid, "target": nid, "kind": "RESIDES_AT"})
                address_index.setdefault(address, []).append((eid, employee.get("name", "?")))

        conflicts = []
        for account, holders in bank_index.items():
            kinds = {h[0].split(":")[0] for h in holders}
            if len(holders) > 1:
                conflicts.append({
                    "risk": "bank_shared",
                    "severity": "CRITICAL" if "employee" in kinds else "HIGH",
                    "detail": f"{len(holders)} parties share account ****{account[-4:]}",
                    "parties": [h[1] for h in holders],
                    "node": f"bank:{account}",
                })
        for address, residents in address_index.items():
            kinds = {r[0].split(":")[0] for r in residents}
            if len(residents) > 1 and "employee" in kinds and "vendor" in kinds:
                conflicts.append({
                    "risk": "address_shared",
                    "severity": "CRITICAL",
                    "detail": f"vendor and employee registered at {address}",
                    "parties": [r[1] for r in residents],
                    "node": f"address:{address}",
                })

        flagged = []
        for record in self.store.list_invoices(limit=1000):
            for finding in ((record.get("payload") or {}).get("fraud") or {}).get("findings", []):
                if finding["engine"] == "graph_network":
                    flagged.append({
                        "invoice_id": record["invoice_id"],
                        "invoice_number": record.get("invoice_number"),
                        "vendor_name": record.get("vendor_name"),
                        "type": finding["type"],
                        "severity": finding["severity"],
                        "message": finding["message"],
                        "amount": record.get("total_amount"),
                    })
        return {"nodes": list(nodes.values()), "edges": edges,
                "conflicts": conflicts, "flagged_invoices": flagged,
                "counts": {"vendors": len(vendors), "employees": len(employees),
                           "bank_accounts": len(bank_index), "addresses": len(address_index)}}

    # ------------------------------------------------------------------ webhook
    def message_from_webhook(self, fields: dict[str, Any],
                             files: list[tuple[str, bytes, str]]) -> bytes:
        """
        Rebuild an RFC-822 message from an inbound-mail webhook payload.

        Providers do not agree on anything except that they POST to a URL, so the
        receiver accepts the shapes actually seen in the wild:

            * a raw MIME message in `raw` / `mime` / `body-mime` (base64 or text)
            * SendGrid Inbound Parse: form fields + `attachment1..N` file parts
            * Mailgun routes: `sender`, `recipient`, `subject`, `body-plain`,
              `attachment-1..N`, `attachment-count`
            * Microsoft Graph: `from.address`, `toRecipients[].emailAddress.address`,
              `subject`, `body.content`, `attachments[].contentBytes`

        Normalising here means the rest of the pipeline sees one envelope type and
        the security controls cannot be bypassed by choosing a different provider.
        """
        import base64
        import email.message

        # (a) a raw message handed over verbatim
        for key in ("raw", "mime", "body-mime", "raw_message", "message"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if candidate.startswith("base64:"):
                    candidate = base64.b64decode(candidate.split(":", 1)[1]).decode(
                        "utf-8", "replace")
                elif "\n" not in candidate and len(candidate) > 200:
                    try:      # provider sent the whole message base64-encoded
                        decoded = base64.b64decode(candidate, validate=True)
                        if b"\n" in decoded:
                            candidate = decoded.decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001 — not base64, use as-is
                        pass
                if "\n" in candidate and (":" in candidate.split("\n", 1)[0]):
                    return candidate.encode("utf-8", "replace")

        # (b) structured fields
        def pick(*names: str) -> str:
            for name in names:
                value = fields.get(name)
                if isinstance(value, str) and value:
                    return value
            return ""

        sender = pick("from", "sender", "From", "envelope-from")
        if not sender:
            graph_from = fields.get("from")
            if isinstance(graph_from, dict):
                sender = (graph_from.get("emailAddress") or {}).get("address") or ""
        recipients = fields.get("toRecipients")
        if isinstance(recipients, list) and recipients:
            first = recipients[0]
            recipient = ((first.get("emailAddress") or {}).get("address")
                         if isinstance(first, dict) else "") or ""
        else:
            recipient = pick("to", "recipient", "To")

        subject = pick("subject", "Subject") or "Inbound invoice"
        body = pick("text", "body-plain", "plain", "body")
        if not body:
            graph_body = fields.get("body")
            if isinstance(graph_body, dict):
                body = graph_body.get("content") or ""

        message = email.message.EmailMessage()
        message["From"] = extract_address(sender) or "unknown@invalid"
        message["To"] = extract_address(recipient) or "ap@company.com"
        message["Subject"] = subject
        message["Message-ID"] = (pick("message_id", "message-id", "Message-Id", "id")
                                 or f"<webhook-{abs(hash((sender, subject, recipient))) % 10**10}@inbox>")
        auth = fields.get("Authentication-Results") or fields.get("auth_results")
        if isinstance(auth, dict):
            auth = " ".join(f"{k}={v}" for k, v in auth.items())
        message["Authentication-Results"] = auth or str(fields.get("auth") or "")
        message.set_content(body or "(no body)")

        added = 0
        for filename, content, mime in files:
            if not content:
                continue
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            message.add_attachment(content, maintype=maintype or "application",
                                   subtype=subtype or "octet-stream", filename=filename)
            added += 1

        for item in fields.get("attachments") or []:
            if not isinstance(item, dict) or added >= 10:
                continue
            name = item.get("filename") or item.get("name") or "attachment.bin"
            payload = item.get("contentBytes") or item.get("content_base64") or ""
            if not payload:
                continue
            try:
                content = base64.b64decode(payload)
            except Exception:  # noqa: BLE001
                continue
            mime = item.get("contentType") or item.get("content_type") or "image/png"
            maintype, _, subtype = mime.partition("/")
            message.add_attachment(content, maintype=maintype or "application",
                                   subtype=subtype or "octet-stream", filename=name)
            added += 1

        for key, value in fields.items():          # SendGrid / Mailgun file fields
            if added >= 10 or not isinstance(value, str) or len(value) < 64:
                continue
            if not key.lower().startswith(("attachment", "file")):
                continue
            try:
                content = base64.b64decode(value, validate=True)
            except Exception:  # noqa: BLE001
                continue
            name = fields.get(f"{key}-name") or fields.get("attachment-info") or f"{key}.png"
            if isinstance(name, str) and name.strip().startswith("{"):
                name = f"{key}.png"
            message.add_attachment(content, maintype="image", subtype="png", filename=str(name))
            added += 1

        return message.as_bytes()

    # ------------------------------------------------------- business value model
    def value_capture(self, discount_pct: float = 0.02, window_days: int = 10,
                      discount_terms_days: int = 20, limit: int = 1000) -> dict[str, Any]:
        """
        Early-payment discount the automation puts back on the table.

        Suppliers who offer 2/10 Net 30 pay the buyer 2% to settle inside ten
        days of the invoice date. The window is short and it starts when the
        invoice arrives, so the benefit depends entirely on approval latency:
        this platform decides in seconds, a manual desk averages a fortnight and
        misses the window. That is a modelled benefit read off the invoices the
        platform cleared without a human — it is labelled as modelled, not
        measured, because cash-discount capture depends on the payment run.
        """
        from datetime import timedelta

        rows = self.store.list_invoices(decision="AUTO_APPROVED", limit=limit)
        captured, forgone, eligible = 0.0, 0.0, []
        manual_cycle_days = 14
        for row in rows:
            payload = row.get("payload") or {}
            headers = {f.get("name"): f.get("value") for f in payload.get("header_fields") or []}
            amount = float(row.get("total_amount") or 0)
            issued = _as_date(headers.get("invoice_date") or payload.get("invoice_date"))
            due = _as_date(headers.get("due_date") or payload.get("due_date"))
            if issued is None or due is None or amount <= 0:
                continue
            terms_days = (due - issued).days
            if terms_days < discount_terms_days:
                continue                      # this agreement carries no settlement discount
            discount = round(amount * discount_pct, 2)
            # automation approves on the day the invoice lands; a manual desk does not
            capturable = window_days >= 0
            missed_by_manual = manual_cycle_days > window_days
            eligible.append({
                "invoice_id": row.get("invoice_id"),
                "vendor_name": row.get("vendor_name"),
                "amount": round(amount, 2),
                "discount": discount,
                "terms_days": terms_days,
                "capture_by": (issued + timedelta(days=window_days)).isoformat(),
            })
            captured += discount if capturable else 0.0
            forgone += discount if missed_by_manual else 0.0
        return {
            "policy": f"{discount_pct:.0%} if approved within {window_days} days "
                      f"(standard 2/10 Net 30 terms)",
            "window_days": window_days,
            "manual_cycle_days": manual_cycle_days,
            "invoices_eligible": len(eligible),
            "discount_available": round(captured, 2),
            "captured_with_automation": round(captured, 2),
            "forgone_on_manual_cycle": round(forgone, 2),
            "modelled": True,
            "note": ("Approval latency decides this: the platform clears an invoice in seconds "
                     f"inside the {window_days}-day window, a {manual_cycle_days}-day manual cycle "
                     "does not."),
            "invoices": eligible[:12],
        }

    def erp_summary(self) -> dict[str, Any]:
        vendors = self.erp.list_vendors() if hasattr(self.erp, "list_vendors") else []
        employees = self.erp.list_employees() if hasattr(self.erp, "list_employees") else []
        posted = self.store.list_invoices(decision="AUTO_APPROVED", limit=1000)
        return {
            "vendors": vendors,
            "employees": employees,
            "vendor_count": len(vendors),
            "employee_count": len(employees),
            "posted_invoices": len(posted),
            "posted_value": round(sum(float(r.get("total_amount") or 0) for r in posted), 2),
            "early_payment": self.value_capture(),
            "erp_backend": type(self.erp).__name__,
        }

    # ---------------------------------------------------------------- feedback
    def feedback_status(self) -> dict[str, Any]:
        stats = self.store.correction_stats()
        stored = self.feedback.statistics() if hasattr(self.feedback, "statistics") else {}
        retrain = {}
        try:
            retrain = self.scheduler.should_retrain()
        except Exception as exc:  # noqa: BLE001
            retrain = {"error": str(exc)}
        return {
            "corrections_recorded": stats.get("total", 0),
            "by_field": stats.get("by_field", {}),
            "feedback_store": stored,
            "retrain": retrain,
            "recent": self.store.list_corrections(limit=25),
            "queue_preview": self.scheduler_review_queue(limit=5),
        }

    def scheduler_review_queue(self, limit: int = 5) -> list[dict[str, Any]]:
        """Invoices the active-learning sampler wants a human to look at."""
        out = []
        for record in self.store.list_invoices(limit=100):
            try:
                score = self.scheduler.uncertainty_score(
                    ser.invoice_from_payload(record.get("payload") or {}))
            except Exception:  # noqa: BLE001
                score = 0.0
            out.append({
                "invoice_id": record["invoice_id"],
                "invoice_number": record.get("invoice_number"),
                "vendor_name": record.get("vendor_name"),
                "document_confidence": record.get("document_confidence"),
                "uncertainty": round(float(score), 4),
            })
        out.sort(key=lambda r: -r["uncertainty"])
        return out[:limit]

    # -------------------------------------------------------------------- misc
    def health(self) -> dict[str, Any]:
        import platform
        import sys
        try:
            import pytesseract
            tesseract_version = str(pytesseract.get_tesseract_version())
        except Exception:  # noqa: BLE001
            tesseract_version = "unavailable"
        return {
            "status": "ok",
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()}",
            "ocr_engine": getattr(self.ctx.ocr, "name", type(self.ctx.ocr).__name__),
            "tesseract": tesseract_version,
            "extractor": type(self.ctx.extractor).__name__,
            "isolation_backend": getattr(self.ctx.isolation, "_backend", "unknown"),
            "erp_backend": type(self.erp).__name__,
            "seeded": {"erp": self.seeded_erp, "baselines": self.seeded_history},
            "history_size": len(self.pipeline.history),
            "database": self._relative(self.store.path),
            "documents_available": len(self.corpus_documents()),
            "time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------- block chain
    def _load_chain(self) -> "AuditBlockchain":
        """Rebuild the in-memory chain from persisted blocks (idempotent, cheap)."""
        from ap_intelligence.audit import AuditBlockchain, Block, build_anchor

        chain = self.blockchain
        if chain is None:
            chain = AuditBlockchain(anchor=build_anchor(self.settings), block_size=25)
            self.blockchain = chain
        stored = self.store.list_blocks(limit=100_000)
        if not stored:
            chain.reset()
        else:
            # DB columns are snake-cased for SQL; Block expects its header names
            chain.blocks = [
                Block.from_dict({
                    "index": row["block_index"],
                    "previous_hash": row["previous_hash"],
                    "merkle_root": row["merkle_root"],
                    "block_hash": row["block_hash"],
                    "record_hashes": row["record_hashes"],
                    "record_count": row["record_count"],
                    "first_seq": row["first_seq"],
                    "last_seq": row["last_seq"],
                    "sealed_at": row["sealed_at"],
                    "anchor": row["anchor"],
                })
                for row in sorted(stored, key=lambda b: b["block_index"])
            ]
        return chain

    def seal_audit_blocks(self) -> dict[str, Any]:
        """
        Seal any un-sealed ledger records into a new anchored block.

        Called at the end of a run and exposed as an endpoint, so blocks can also
        be sealed on a schedule by an external job.
        """
        chain = self._load_chain()
        sealed = chain.seal(self.store.all_ledger_records())
        for block in sealed:
            self.store.save_block(block.to_dict())
        self._export_chain_files()
        return {
            "sealed_blocks": len(sealed),
            "blocks": [b.to_dict() for b in sealed],
            "chain": chain.verify(),
        }

    def audit_chain_status(self) -> dict[str, Any]:
        chain = self._load_chain()
        status = chain.verify()
        pending = [r for r in self.store.all_ledger_records()
                   if r["seq"] > (chain.blocks[-1].last_seq if chain.blocks else -1)]
        status["pending_records"] = len(pending)
        status["block_size"] = chain.block_size
        status["blocks"] = [b.to_dict() for b in reversed(chain.blocks[-25:])]
        status["anchor_sink"] = chain.anchor.name
        return status

    def audit_proof(self, invoice_id: str) -> dict[str, Any]:
        """Merkle inclusion proof for one invoice, verifiable without this app."""
        record = self.store.get_invoice(invoice_id)
        if record is None:
            raise KeyError(invoice_id)
        chain = self._load_chain()
        row = next((r for r in self.store.all_ledger_records()
                    if r["invoice_id"] == invoice_id), None)
        if row is None:
            raise KeyError(f"no ledger record for {invoice_id}")
        proof = chain.proof_for_record(row["record_hash"])
        return {
            "invoice_id": invoice_id,
            "ledger_seq": row["seq"],
            "record_hash": row["record_hash"],
            "decision": row["decision"],
            **(proof or {"verified": False,
                         "note": "not yet sealed — seal blocks to produce a proof"}),
        }

    # ------------------------------------------------------------------ payments
    def payment_rail_info(self) -> dict[str, Any]:
        """What the payment engine would do right now, and on which rail."""
        info = self.payment_engine.rail_info()
        info["paid_invoices"] = len(self.store.paid_invoices())
        info["last_run"] = self._last_payment_run
        return info

    def run_payments(self, max_payments: Optional[int] = None,
                     invoice_ids: Optional[list[str]] = None,
                     dry_run: bool = False) -> dict[str, Any]:
        """
        Execute a payment run over the invoices the pipeline approved.

        Only invoices that exist in the workbench and whose decision released them
        are considered, and invoices already carrying a payment record are passed to
        the engine as settled so its idempotency guard can see them. That guard is
        what makes it safe to call this repeatedly — from the UI, from a cron job, or
        by accident.
        """
        vendors = {row["name"]: row for row in
                   (self.erp.list_vendors() if hasattr(self.erp, "list_vendors") else [])}
        paid = self.store.list_payments(limit=1000)
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        already_paid_ids = self.store.paid_invoices()
        already_paid: list[dict[str, Any]] = []

        for record in self.store.list_invoices(limit=1000):
            if invoice_ids and record["invoice_id"] not in invoice_ids:
                continue
            if record["invoice_id"] in already_paid_ids:
                # reported rather than silently dropped: "nothing happened" and
                # "everything was already paid" are very different answers
                already_paid.append({"invoice_id": record["invoice_id"],
                                     "invoice_number": record.get("invoice_number"),
                                     "vendor_name": record.get("vendor_name"),
                                     "amount": record.get("total_amount"),
                                     "reason": "already released in an earlier run — "
                                               "idempotency key matches"})
                continue
            if (record.get("decision") or "") not in ("AUTO_APPROVED",) and \
                    (record.get("review_status") or "") != "APPROVED":
                continue
            payload = (self.store.get_invoice(record["invoice_id"]) or {}).get("payload") or {}
            candidates.append((record, payload))

        # highest value first: a capped run should release the money that matters
        candidates.sort(key=lambda item: float(item[0].get("total_amount") or 0.0), reverse=True)

        run = self.payment_engine.run(candidates, vendors, already_paid=paid,
                                      max_payments=max_payments, dry_run=dry_run)
        if not dry_run:
            for payment in run.paid:
                self.store.save_payment(payment)
            for withheld in run.withheld:
                if withheld.get("payment_id"):
                    self.store.save_payment({
                        **withheld, "status": "WITHHELD", "rail_reference": None,
                        "created_at": datetime.now(timezone.utc).isoformat()})
        payload = run.to_dict()
        # candidates filtered out here never reached the engine, so they are folded
        # into the same "skipped" list the UI reads
        payload["skipped"] = already_paid + payload["skipped"]
        payload["dry_run"] = bool(dry_run)
        if dry_run:
            # a dry run is a forecast, not history: it must not become "the last run"
            return payload
        self._last_payment_run = payload
        self.store.set_meta("last_payment_run", payload)
        return payload

    def payment_history(self, limit: int = 200) -> dict[str, Any]:
        payments = self.store.list_payments(limit=limit)
        settled = [p for p in payments if p.get("status") in ("SETTLED", "SUBMITTED", "FILE_READY")]
        withheld = [p for p in payments if p.get("status") == "WITHHELD"]
        by_method: dict[str, int] = {}
        for payment in payments:
            key = str(payment.get("settlement_method") or "unknown")
            by_method[key] = by_method.get(key, 0) + 1
        return {
            "payments": payments,
            "count": len(payments),
            "released": len(settled),
            "withheld": len(withheld),
            "value_paid": round(sum(float(p.get("amount") or 0) for p in settled), 2),
            "value_withheld": round(sum(float(p.get("amount") or 0) for p in withheld), 2),
            "discount_captured": round(sum(float(p.get("discount_value") or 0)
                                           for p in settled), 2),
            "by_method": by_method,
            "by_status": _counts(payments, "status"),
            "rail": self.payment_engine.rail.describe(),
            "last_run": self._last_payment_run or self.store.get_meta("last_payment_run"),
        }

    def remittance_advice(self, payment_id: str) -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if payment is None:
            raise KeyError(payment_id)
        detail = payment.get("detail") or {}
        vendors = {row["name"]: row for row in
                   (self.erp.list_vendors() if hasattr(self.erp, "list_vendors") else [])}
        vendor = vendors.get(payment.get("vendor_name") or "", {})
        text = self.payment_engine.remittance_advice({**detail, **payment}, vendor)
        return {"payment_id": payment_id, "remittance": text,
                "vendor_name": payment.get("vendor_name"),
                "amount": payment.get("amount"), "currency": payment.get("currency")}

    # ----------------------------------------------------------------- contracts
    def contract_summary(self) -> dict[str, Any]:
        """Loaded master agreements, their parsed terms, and coverage of the corpus."""
        matcher = self.ctx.contracts
        if matcher is None:
            return {"contracts": 0, "parsed": [], "coverage": None,
                    "note": "no contracts loaded — run scripts/build_contracts.py"}
        store = matcher.store
        summary = store.summary()
        coverage = matcher.coverage_report()
        assessment = {
            "invoices_assessed": coverage["invoices_assessed"],
            "invoices_with_contract": coverage["invoices_with_contract"],
            "lines_compared": coverage["lines_compared"],
            "overcharge_exposure": coverage["overcharge_exposure"],
            "engine": coverage["engine"],
        }
        # Coverage is read back from the documents themselves, not from the
        # matcher's in-memory state: a restarted process must still be able to show
        # which invoices were checked against which agreement, and what it found.
        per_contract: dict[str, dict[str, Any]] = {}
        for record in self.store.list_invoices(limit=1000):
            payload = (self.store.get_invoice(record["invoice_id"]) or {}).get("payload") or {}
            for finding in (payload.get("fraud") or {}).get("findings", []):
                if finding.get("engine") != "contract_compliance":
                    continue
                contract_id = (finding.get("evidence") or {}).get("contract_id") or "unmatched"
                bucket = per_contract.setdefault(contract_id, {
                    "invoices": set(), "findings": 0, "exposure": 0.0, "by_type": {}})
                bucket["invoices"].add(record["invoice_id"])
                bucket["findings"] += 1
                # capped per invoice, the same rule the risk engine applies, so the
                # panel cannot show more exposure on one invoice than its value
                bucket["exposure"] = min(
                    bucket["exposure"] + float(finding.get("recoverable_amount") or 0.0),
                    float(record.get("total_amount") or 0.0) or float("inf"))
                key = finding.get("type") or "UNKNOWN"
                bucket["by_type"][key] = bucket["by_type"].get(key, 0) + 1

        assessment["findings_total"] = sum(b["findings"] for b in per_contract.values())
        assessment["exposure_from_documents"] = round(
            sum(b["exposure"] for b in per_contract.values()), 2)

        ratings = []
        for contract in store.contracts:
            bucket = per_contract.get(contract.contract_id, {})
            ratings.append({
                **contract.to_dict(),
                "invoices": len(bucket.get("invoices") or []),
                "lines_checked": len(contract.rate_lines),
                "exposure": round(bucket.get("exposure", 0.0), 2),
                "findings": bucket.get("findings", 0),
                "findings_by_type": bucket.get("by_type", {}),
                "report": store.last_report.get(contract.contract_id, {}),
            })
        return {"contracts": summary["contracts"], "directory": summary["directory"],
                "llm": summary["llm"], "parsed": summary["parsed"],
                "coverage": assessment, "agreements": ratings}

    # ------------------------------------------------------------------ platform
    def platform_info(self) -> dict[str, Any]:
        """
        How this instance is deployed: topology, runtime configuration, and what the
        optional infrastructure is currently set to.
        """
        from services.registry import topology

        settings = self.settings
        return {
            "topology": topology(),
            "runtime": {
                "database": "sqlite (single node)",
                "broker": os.getenv("AP_BROKER_URL") or "in-process queue",
                "object_store": os.getenv("AP_OBJECT_STORE", "local filesystem"),
                "anchor": settings.anchor_url or "not configured (NullAnchor)",
                "payments": os.getenv("AP_PAYMENTS_MODE", "sandbox"),
                "llm": self.contract_summary().get("llm", {}).get("provider", "none"),
                "services_mode": os.getenv("AP_SERVICES_MODE", "single-process"),
            },
            "process": {
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "documents_indexed": self.store.summary()["total"],
            },
        }

    def _export_chain_files(self) -> None:
        """
        Write the three files an auditor verifies offline:
        records, sealed blocks, anchor receipts.
        """
        outputs = Path(getattr(self.settings, "outputs_dir", PROJECT_ROOT / "outputs"))
        outputs = Path(outputs)
        outputs.mkdir(parents=True, exist_ok=True)

        with (outputs / "audit_trail.jsonl").open("w", encoding="utf-8") as handle:
            for row in self.store.all_ledger_records():
                try:
                    record = json.loads(row.get("record") or "{}")
                except json.JSONDecodeError:
                    record = {}
                handle.write(json.dumps({
                    "seq": row["seq"], "invoice_id": row["invoice_id"],
                    "decision": row["decision"], "previous_hash": row["previous_hash"],
                    "record_hash": row["record_hash"], "created_at": row["created_at"],
                    "record": record,
                }, sort_keys=True, default=str) + "\n")

        blocks = self.store.list_blocks(limit=100_000)
        with (outputs / "audit_blocks.jsonl").open("w", encoding="utf-8") as handle:
            for row in sorted(blocks, key=lambda b: b["block_index"]):
                # export in header form so the verifier never has to know SQL naming
                handle.write(json.dumps({
                    "index": row["block_index"],
                    "previous_hash": row["previous_hash"],
                    "merkle_root": row["merkle_root"],
                    "block_hash": row["block_hash"],
                    "record_hashes": row["record_hashes"],
                    "record_count": row["record_count"],
                    "first_seq": row["first_seq"],
                    "last_seq": row["last_seq"],
                    "sealed_at": row["sealed_at"],
                    "version": 1,
                }, sort_keys=True, default=str) + "\n")

        with (outputs / "anchors.jsonl").open("w", encoding="utf-8") as handle:
            for block in sorted(blocks, key=lambda b: b["block_index"]):
                anchor = block.get("anchor") or {}
                if not anchor.get("anchored"):
                    continue
                handle.write(json.dumps({
                    "block_index": block["block_index"],
                    "block_hash": anchor.get("block_hash") or block["block_hash"],
                    "merkle_root": block["merkle_root"],
                    "record_count": block["record_count"],
                    "anchored_at": anchor.get("anchored_at"),
                    "anchor": anchor.get("anchor"),
                }, sort_keys=True, default=str) + "\n")

    def _relative(self, path: Path) -> str:
        """Display a path relative to the project when possible, absolute otherwise."""
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    def document_image_path(self, invoice_id: str) -> Optional[Path]:
        """
        Locate the stored scan. Paths are persisted relative to the project, so a
        database moved to another machine (or unzipped anywhere) still resolves.
        """
        record = self.store.get_invoice(invoice_id)
        if not record:
            return None
        candidates = [
            record.get("document_path"),
            (record.get("payload") or {}).get("document_path"),
        ]
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw)
            for candidate in (path, self.root / path):
                try:
                    if candidate.exists():
                        return candidate
                except OSError:
                    continue
        return None

    def close(self) -> None:
        self.store.close()
