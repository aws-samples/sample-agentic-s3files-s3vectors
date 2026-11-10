"""Option 3 prototype: intake recognition logic for the orchestrator.

Pure, dependency-free, and unit-testable. This is NOT wired into the shipped
orchestrator container yet — it exists so we can validate the decision logic and
the correctness guardrail locally before spending a rebuild + live-event cycle.

The real orchestrator would:
  * read the PDF text layer with a cheap, non-LLM library,
  * call `parse_intake` on that text,
  * pass a real S3 Vectors query function into `recognize`,
  * write `build_task(...)` to tasks/<id>.json.

Everything here is deterministic given its inputs; the only external dependency
(the vector query) is injected, so tests supply a stub.
"""
import re

# Cosine-distance cutoff below which the vendor + kind of work is "known" and the
# classification may be reused. Same scale as the validator's CATEGORY_DISTANCE.
DEFAULT_CACHE_DISTANCE = 0.35

# Fields the recognition path is ALLOWED to reuse. Anything variable per invoice
# (amount, dates, invoice_number) is deliberately excluded and must be read fresh
# by the LLM extractor. This tuple is the guardrail the tests assert against.
CACHEABLE_CLASSIFICATION_FIELDS = ("vendor", "category", "document_type")


def parse_intake(text):
    """Best-effort, non-LLM parse of an invoice's text layer.

    Returns a dict with vendor/category/invoice_number/amount/description; any
    field it cannot find is None. Only vendor + category feed the recognition
    decision; invoice_number/amount are captured purely as non-authoritative
    routing hints and are NEVER treated as the paid/verdict values.
    """
    text = text or ""

    def _find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    vendor = _find(r"From:\s*(.+)")
    category = _find(r"Category:\s*(.+)")
    invoice_number = _find(r"Invoice\s*Number:\s*([A-Za-z0-9\-]+)")

    amount = None
    m = re.search(r"Total Due:\s*USD\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None

    # Description is the hardest field to get cleanly from a text layer: reportlab
    # emits the two column headers ("Description", "Amount (USD)") and the amount as
    # their own lines, so a naive "line after Description" grabs the wrong text.
    # Scan the line-items region and return the first real work line.
    description = _description(text)

    return {
        "vendor": vendor,
        "category": category,
        "invoice_number": invoice_number,
        "amount": amount,
        "description": description,
    }


def _description(text):
    """Extract the invoice's work description from the line-items region.

    reportlab (and pypdf's read of it) emits the column headers "Description" and
    "Amount (USD)" and the amount as separate lines. Starting after the
    "Description" header, return the first line that is real work text — skipping
    the amount-column header, blank lines, and bare amount lines, and stripping a
    trailing amount when the description and amount share a line. Stops at
    "Total Due". Returns None if nothing usable is found.
    """
    amount_re = re.compile(r"[\d,]+\.\d{2}")
    skip = {"description", "amount (usd)", "amount"}
    started = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        low = line.lower()
        if not started:
            if low.startswith("description"):
                started = True
            continue
        if not line or low in skip:
            continue
        if low.startswith("total due"):
            break
        # strip a trailing amount if the row is "<description>   1,234.00"
        m = re.match(r"(.+?)\s+[\d,]+\.\d{2}\s*$", line)
        candidate = m.group(1).strip() if m else line
        if not candidate or amount_re.fullmatch(candidate):
            continue
        return candidate
    return None


def canonical_text(vendor, category, description):
    """Same embedding text the validator/embedder/seed use. Describes the WORK,
    not the amount, so similar invoices cluster together."""
    return " | ".join(p for p in (vendor, category, description) if p)


def recognize(parsed, query_neighbors, cache_distance=DEFAULT_CACHE_DISTANCE):
    """Decide whether the classifier can reuse classification for this intake.

    `query_neighbors(text, vendor)` is injected: it must return a list of
    neighbors sorted nearest-first, each a dict with at least {"key", "distance"}.
    Any exception it raises is treated as a fallback to NOVEL (never skip on error).

    Returns a decision dict. `skip_classify` is True only when we have a parsed
    vendor, that vendor has history, and the nearest neighbor is within
    `cache_distance`.
    """
    vendor = (parsed or {}).get("vendor")
    if not vendor:
        return {"skip_classify": False, "reason": "unparsed",
                "cached_classification": None, "nearest_key": None, "distance": None,
                "threshold": cache_distance}

    text = canonical_text(vendor, parsed.get("category"), parsed.get("description"))
    try:
        neighbors = query_neighbors(text, vendor) or []
    except Exception as exc:  # never skip work because a lookup failed
        return {"skip_classify": False, "reason": f"query_error: {exc}",
                "cached_classification": None, "nearest_key": None, "distance": None,
                "threshold": cache_distance}

    if not neighbors:
        return {"skip_classify": False, "reason": "new_vendor",
                "cached_classification": None, "nearest_key": None, "distance": None,
                "threshold": cache_distance}

    nearest = min(neighbors, key=lambda n: n["distance"])
    dist = float(nearest["distance"])
    if dist < cache_distance:
        return {
            "skip_classify": True,
            "reason": "recognized",
            "cached_classification": {
                "vendor": vendor,
                "category": parsed.get("category"),
                "document_type": "INVOICE",
            },
            "nearest_key": nearest.get("key"),
            "distance": round(dist, 4),
            "threshold": cache_distance,
        }
    return {"skip_classify": False, "reason": "novel",
            "cached_classification": None, "nearest_key": nearest.get("key"),
            "distance": round(dist, 4), "threshold": cache_distance}


def build_task(invoice_id, parsed, decision):
    """Assemble the tasks/<id>.json payload.

    GUARDRAIL: the reused `cached_classification` may only carry the fields in
    CACHEABLE_CLASSIFICATION_FIELDS. Variable fields (amount/date/invoice_number)
    are never placed in any authoritative/cached slot — they are read fresh by
    the extractor. Parsed hints, if kept at all, live under `intake_hint` and are
    explicitly non-authoritative.
    """
    cached = decision.get("cached_classification")
    if cached is not None:
        illegal = set(cached) - set(CACHEABLE_CLASSIFICATION_FIELDS)
        if illegal:
            raise ValueError(f"cached_classification leaked variable fields: {sorted(illegal)}")

    return {
        "invoice_id": invoice_id,
        "source_pdf": f"incoming/{invoice_id}.pdf",
        "status": "assigned",
        "stage": "extract" if decision.get("skip_classify") else "classify",
        "skip_classify": bool(decision.get("skip_classify")),
        "cached_classification": cached,
        "recognition": {
            "reason": decision.get("reason"),
            "nearest_key": decision.get("nearest_key"),
            "distance": decision.get("distance"),
            "threshold": decision.get("threshold"),
        },
        # Non-authoritative hint only; the extractor still reads these fresh.
        "intake_hint": {
            "invoice_number": (parsed or {}).get("invoice_number"),
            "amount": (parsed or {}).get("amount"),
        },
    }
