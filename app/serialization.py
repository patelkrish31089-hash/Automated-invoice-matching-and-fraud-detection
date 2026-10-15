"""
Serialisation between the pipeline's dataclasses and the workbench's JSON.

The pipeline works with typed objects; the web layer works with JSON. Rather than
sprinkling `to_dict()` calls everywhere, conversion lives here — one place to
change when the domain model grows.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from ap_intelligence.models import (
    BoundingBox,
    Decision,
    DocumentSource,
    Finding,
    FraudAssessment,
    Invoice,
    LineItem,
    MatchResult,
    ProcessingResult,
    PurchaseOrder,
    PurchaseOrderLine,
    GoodsReceiptNote,
    GoodsReceiptLine,
)

# The header fields the reviewer sees, in the order they appear on a document.
DISPLAY_FIELDS: list[tuple[str, str]] = [
    ("vendor_name", "Vendor name"),
    ("vendor_tax_id", "Vendor tax ID"),
    ("vendor_bank_account", "Remit-to bank account"),
    ("invoice_number", "Invoice number"),
    ("invoice_date", "Invoice date"),
    ("due_date", "Due date"),
    ("po_number", "Purchase order"),
    ("currency", "Currency"),
]


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None


def _num(value: Any) -> Optional[float]:
    return None if value is None else round(float(value), 2)


def box_to_list(box: Optional[BoundingBox]) -> Optional[list[int]]:
    if box is None:
        return None
    return [int(box.x0), int(box.y0), int(box.x1), int(box.y1)]


# --------------------------------------------------------------------------- in
def invoice_from_payload(payload: dict[str, Any]) -> Invoice:
    """Rebuild an Invoice from a stored detail payload (used after corrections)."""
    inv = Invoice(
        invoice_id=payload["invoice_id"],
        document_path=payload.get("document_path", ""),
        page_width=payload.get("page_width", 0),
        page_height=payload.get("page_height", 0),
    )
    inv.vendor_name = payload.get("vendor_name")
    inv.vendor_tax_id = payload.get("vendor_tax_id")
    inv.vendor_bank_account = payload.get("vendor_bank_account")
    inv.invoice_number = payload.get("invoice_number")
    inv.po_number = payload.get("po_number")
    inv.currency = payload.get("currency") or "USD"
    for field in ("invoice_date", "due_date"):
        raw = payload.get(field)
        if raw:
            try:
                setattr(inv, field, date.fromisoformat(str(raw)[:10]))
            except ValueError:
                setattr(inv, field, None)
    totals = payload.get("totals", {})
    inv.subtotal = totals.get("subtotal")
    inv.tax_amount = totals.get("tax")
    inv.shipping = totals.get("shipping") or 0.0
    inv.total_amount = totals.get("total")
    inv.field_confidences = {
        k: v for k, v in (payload.get("field_confidences") or {}).items()
    }
    inv.field_boxes = {k: list(v) for k, v in (payload.get("field_boxes") or {}).items()}
    inv.document_confidence = payload.get("document_confidence", 0.0)
    inv.ocr_mean_confidence = payload.get("ocr_mean_confidence", 0.0)
    inv.extraction_model = payload.get("extraction_model", "spatial-heuristic-v1")
    inv.source = DocumentSource.API_UPLOAD
    inv.line_items = []
    for item in payload.get("line_items", []):
        box = item.get("source_bbox")
        inv.line_items.append(
            LineItem(
                line_no=item.get("line_no", 0),
                description=item.get("description", ""),
                quantity=item.get("quantity", 0.0),
                unit_price=item.get("unit_price", 0.0),
                amount=item.get("amount", 0.0),
                unit=item.get("unit", "EA"),
                part_number=item.get("part_number"),
                confidence=item.get("confidence", 1.0),
                source_bbox=BoundingBox(*box) if isinstance(box, list) and len(box) == 4 else None,
                matched_po_line=item.get("matched_po_line"),
                match_score=item.get("match_score", 0.0),
                notes=list(item.get("notes") or []),
            )
        )
    return inv


# -------------------------------------------------------------------------- out
def invoice_header_fields(inv: Invoice) -> list[dict[str, Any]]:
    """Header fields with confidence and the pixel box that produced each value."""
    fields = []
    for name, label in DISPLAY_FIELDS:
        raw = getattr(inv, name, None)
        value = _iso(raw) if isinstance(raw, (date, datetime)) else raw
        fields.append({
            "name": name,
            "label": label,
            "value": value,
            "display": _display_value(name, value),
            "confidence": round(float(inv.field_confidences.get(name, 0.0)), 4),
            "box": inv.field_boxes.get(name),
            "missing": value in (None, ""),
        })
    return fields


def _display_value(name: str, value: Any) -> str:
    if value in (None, ""):
        return "—"
    if name in ("invoice_date", "due_date"):
        return str(value)[:10]
    return str(value)


def invoice_totals(inv: Invoice) -> dict[str, Any]:
    """Amounts plus the arithmetic check that a clerk (or a fraudster) must satisfy."""
    expected = None
    if inv.subtotal is not None and inv.tax_amount is not None:
        expected = round(inv.subtotal + inv.tax_amount + (inv.shipping or 0.0), 2)
    delta = None if (expected is None or inv.total_amount is None) else round(
        inv.total_amount - expected, 2)
    return {
        "subtotal": _num(inv.subtotal),
        "tax": _num(inv.tax_amount),
        "shipping": _num(inv.shipping),
        "total": _num(inv.total_amount),
        "line_item_sum": inv.line_item_sum,
        "subtotal_delta": (None if (inv.subtotal is None)
                           else round(inv.line_item_sum - inv.subtotal, 2)),
        "expected_total": expected,
        "total_delta": delta,
        "arithmetic_ok": delta is not None and abs(delta) <= 0.05,
        "boxes": {name: inv.field_boxes.get(f"amount_{name}")
                  for name in ("subtotal", "tax", "shipping", "total")},
        "confidences": {name: round(float(inv.field_confidences.get(f"amount_{name}", 0.0)), 4)
                        for name in ("subtotal", "tax", "shipping", "total")},
    }


def line_items_to_payload(inv: Invoice) -> list[dict[str, Any]]:
    out = []
    for item in inv.line_items:
        out.append({
            "line_no": item.line_no,
            "description": item.description,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "amount": item.amount,
            "unit": item.unit,
            "part_number": item.part_number,
            "confidence": round(float(item.confidence), 4),
            "source_bbox": box_to_list(item.source_bbox),
            "matched_po_line": item.matched_po_line,
            "match_score": round(float(item.match_score), 4),
            "notes": list(item.notes or []),
        })
    return out


def match_to_payload(match: Optional[MatchResult],
                     po: Optional[PurchaseOrder] = None,
                     grns: Optional[list[GoodsReceiptNote]] = None) -> Optional[dict[str, Any]]:
    """Match outcome, enriched with the ERP records it was checked against."""
    if match is None:
        return None
    payload: dict[str, Any] = {
        "po_found": match.po_found,
        "grn_found": match.grn_found,
        "fully_matched": match.fully_matched,
        "within_tolerance": match.within_tolerance,
        "total_variance": round(float(match.total_variance or 0.0), 2),
        "line_matches": [],
        "purchase_order": None,
        "goods_receipts": [],
    }
    payload.update({
        "po_number": match.po_number,
        "po_total": round(float(match.po_total or 0.0), 2),
        "grn_total": round(float(match.grn_total or 0.0), 2),
        "invoice_total": round(float(match.invoice_total or 0.0), 2),
        "total_variance_pct": round(float(match.total_variance_pct or 0.0), 4),
        "notes": list(match.notes or []),
    })
    for lm in match.line_matches:
        payload["line_matches"].append({
            "invoice_line": lm.invoice_line_no,
            "po_line": lm.po_line_no,
            "description_score": round(float(lm.description_score or 0.0), 4),
            "quantity_delta": round(float(lm.quantity_delta or 0.0), 3),
            "price_delta": round(float(lm.price_delta or 0.0), 2),
            "amount_delta": round(float(lm.amount_delta or 0.0), 2),
            "po_quantity": lm.po_quantity,
            "grn_quantity": lm.grn_quantity,
            "status": lm.status,
        })
    if isinstance(po, PurchaseOrder):
        payload["purchase_order"] = {
            "po_number": po.po_number,
            "vendor_name": po.vendor_name,
            "issued_on": _iso(po.issued_on),
            "buyer": po.buyer,
            "approved_by": po.approved_by,
            "status": po.status,
            "total": round(sum(line.amount for line in po.lines), 2),
            "lines": [
                {"line_no": line.line_no, "description": line.description,
                 "quantity": line.quantity, "unit_price": line.unit_price,
                 "amount": line.amount, "part_number": line.part_number}
                for line in po.lines
            ],
        }
    for grn in grns or []:
        payload["goods_receipts"].append({
            "grn_number": grn.grn_number,
            "po_number": grn.po_number,
            "received_on": _iso(grn.received_on),
            "received_by": grn.received_by,
            "lines": [{"po_line_no": line.po_line_no,
                       "quantity_received": line.quantity_received,
                       "condition": line.condition}
                      for line in grn.lines],
        })
    return payload


def finding_to_payload(finding: Finding) -> dict[str, Any]:
    return {
        "type": finding.finding_type.value,
        "severity": finding.severity.value,
        "message": finding.message,
        "engine": finding.engine,
        "score": round(float(finding.score or 0.0), 4),
        "recoverable_amount": round(float(finding.recoverable_amount or 0.0), 2),
        "evidence": finding.evidence,
        "box": box_to_list(finding.bbox),
    }


def fraud_to_payload(fraud: FraudAssessment) -> dict[str, Any]:
    payload = fraud.to_dict()
    payload["findings"] = [finding_to_payload(f) for f in fraud.findings]
    return payload


def result_to_detail(result: ProcessingResult, image_url: str = "",
                     expected_label: Optional[str] = None,
                     quality: Optional[dict] = None,
                     po: Optional[PurchaseOrder] = None,
                     grns: Optional[list[GoodsReceiptNote]] = None) -> dict[str, Any]:
    """Everything the invoice detail screen renders, in one object."""
    inv = result.invoice
    return {
        "invoice_id": inv.invoice_id,
        "invoice_number": inv.invoice_number,
        "vendor_name": inv.vendor_name,
        "document_path": inv.document_path,
        "image_url": image_url,
        "page_width": inv.page_width,
        "page_height": inv.page_height,
        "source": inv.source.value if isinstance(inv.source, DocumentSource) else str(inv.source),
        "received_at": _iso(inv.received_at),
        "document_confidence": round(float(inv.document_confidence), 4),
        "ocr_mean_confidence": round(float(inv.ocr_mean_confidence), 4),
        "extraction_model": inv.extraction_model,
        "field_confidences": {k: v for k, v in inv.field_confidences.items()
                              if isinstance(v, (int, float)) and not isinstance(v, bool)},
        "field_boxes": inv.field_boxes,
        "header_fields": invoice_header_fields(inv),
        "totals": invoice_totals(inv),
        "line_items": line_items_to_payload(inv),
        "line_item_reconciles": (
            inv.subtotal is None or abs(inv.line_item_sum - inv.subtotal) <= 0.05
        ),
        "match": match_to_payload(result.match, po, grns),
        "fraud": fraud_to_payload(result.fraud),
        "decision": result.decision.value,
        "decision_reason": result.decision_reason,
        "routing_target": result.routing_target,
        "priority": result.priority,
        "processing_ms": round(result.processing_ms, 1),
        "cost_usd": round(result.cost_usd, 5),
        "trace": result.trace,
        "quality": quality or {},
        "expected_label": expected_label,
    }


def summary_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Columns for the list view / review queue."""
    fraud = detail.get("fraud", {})
    return {
        "decision": detail.get("decision"),
        "decision_reason": detail.get("decision_reason"),
        "routing_target": detail.get("routing_target"),
        "risk_score": fraud.get("risk_score"),
        "risk_band": fraud.get("risk_band"),
        "recoverable": fraud.get("recoverable_amount", 0.0),
        "document_confidence": detail.get("document_confidence"),
        "total_amount": (detail.get("totals") or {}).get("total"),
        "priority": detail.get("priority", "NORMAL"),
    }
