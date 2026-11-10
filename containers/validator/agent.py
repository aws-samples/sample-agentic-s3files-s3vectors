"""InvoiceIQ — Validator (the memory-aware agent).

Watches /mnt/s3files/extracted/. For each invoice it:

  1. Builds a content embedding of "<vendor> | <category> | <description>" with
     Bedrock Titan. The text describes the WORK, not the amount, so semantically
     similar invoices land near each other in the vector space.
  2. Queries Amazon S3 Vectors for that vendor's history (vendor-filtered kNN),
     which gives it three things at once: the approved average, the nearest prior
     invoices (with cosine distances), and their invoice numbers.
  3. Computes the verdict in code from four signals:
       - amount anomaly    : amount > ANOMALY_FACTOR x the vendor's approved average
       - exact duplicate   : a prior invoice with the same invoice number exists
       - near duplicate    : a prior invoice is within NEAR_DUP_DISTANCE in the
                             vector space but carries a DIFFERENT invoice number
                             (a re-submission with the number changed) — this is
                             the check a plain database key cannot make
       - category anomaly  : the invoice sits far (> CATEGORY_DISTANCE) from
                             everything in the vendor's history, i.e. an unusual
                             kind of work, even when the amount looks normal
  4. Uses a Strands agent (Amazon Bedrock) to phrase a one-sentence recommendation.
  5. Writes validated/<id>.json (and copies to flagged/ when not PASS).

    extracted/<id>.json  --> validated/<id>.json  (+ flagged/<id>.json)

The verdict (PASS / FLAGGED / REJECTED) is decided by the code, not the language
model. The embedding text is written into the envelope as `embed_text`, and the
embedder stores that exact string, so history always lines up.
"""
import datetime
import json
import os
import re
import time

import boto3
from strands import Agent
from strands.models import BedrockModel

REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = os.environ.get("AGENT_MODEL", "us.amazon.nova-pro-v1:0")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
MOUNT = os.environ.get("S3_FILES_MOUNT", "/mnt/s3files")
VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", "")
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "invoice-history")
ANOMALY_FACTOR = float(os.environ.get("ANOMALY_FACTOR", "3"))
# Cosine-distance thresholds for the two semantic checks (calibrated on Titan v2).
NEAR_DUP_DISTANCE = float(os.environ.get("NEAR_DUP_DISTANCE", "0.10"))
CATEGORY_DISTANCE = float(os.environ.get("CATEGORY_DISTANCE", "0.45"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

INPUT_DIR = os.path.join(MOUNT, "extracted")
OUTPUT_DIR = os.path.join(MOUNT, "validated")
FLAGGED_DIR = os.path.join(MOUNT, "flagged")

# Titan embeddings and the vector store are direct boto3 calls (utility work, not
# agent reasoning). The Strands agent is used only to phrase the recommendation.
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)
model = BedrockModel(model_id=MODEL_ID, region_name=REGION)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def canonical_text(vendor, category, description):
    """Shared content embedding text. Keep identical to the embedder and the seed.
    Describes the work (vendor, category, line-item description), not the amount,
    so similar invoices cluster and unusual ones stand apart."""
    return " | ".join(p for p in (vendor, category, description) if p)


def normalize_vendor(vendor):
    """Stable key so 'Acme Corp' and 'Acme Corporation' match. Lowercase, drop
    punctuation and common company suffixes, collapse whitespace. MUST stay
    identical in the validator, embedder, and seed, or history lookups miss."""
    s = re.sub(r"[^a-z0-9\s]", " ", (vendor or "").lower())
    suffixes = {"inc", "incorporated", "corp", "corporation", "co", "company",
                "llc", "ltd", "limited", "llp", "lp", "plc", "gmbh", "sa", "ag"}
    toks = [t for t in s.split() if t not in suffixes]
    return " ".join(toks) or (vendor or "").strip().lower()


def line_item_summary(doc):
    """A short description of the invoice's work, from the extracted line items."""
    items = doc.get("line_items") or []
    parts = [str(li.get("description", "")).strip() for li in items if li.get("description")]
    if parts:
        return "; ".join(parts)[:400]
    return str(doc.get("description", "")).strip()


def embed(text):
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024}),
    )
    return json.loads(resp["body"].read())["embedding"]


def query_history(qvec, vendor):
    """Vendor-filtered kNN against S3 Vectors memory. Returns
    (approved_average, approved_count, neighbors[]) where neighbors are sorted by
    cosine distance and carry key, amount, invoice_number and distance."""
    if not VECTOR_BUCKET:
        return None, 0, []
    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": qvec},
        topK=10,
        returnDistance=True,
        returnMetadata=True,
        filter={"vendor_key": normalize_vendor(vendor)},
    )
    neighbors, approved = [], []
    for v in res.get("vectors", []):
        meta = v.get("metadata", {})
        amt = float(meta.get("amount", 0) or 0)
        neighbors.append({
            "key": v.get("key"),
            "amount": amt,
            "invoice_number": str(meta.get("invoice_number", "") or ""),
            "distance": round(float(v.get("distance", 0)), 4),
        })
        if str(meta.get("outcome", "approved")).lower() in ("approved", "pass"):
            approved.append(amt)
    neighbors.sort(key=lambda n: n["distance"])
    avg = round(sum(approved) / len(approved), 2) if approved else None
    return avg, len(approved), neighbors


def exact_duplicate(qvec, vendor, invoice_number):
    """A prior invoice with the SAME invoice number (an exact re-submission).
    Keyed on the invoice number so two different invoices that happen to total the
    same are never confused, and the seeded history (no invoice number) never trips
    it."""
    if not VECTOR_BUCKET or not invoice_number:
        return False, None
    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": qvec},
        topK=5,
        returnDistance=True,
        returnMetadata=True,
        filter={"invoice_number": str(invoice_number)},
    )
    for v in res.get("vectors", []):
        if str(v.get("metadata", {}).get("invoice_number")) == str(invoice_number):
            return True, v.get("key")
    return False, None


def near_duplicate(neighbors, invoice_number):
    """A re-submission with the invoice number changed: a prior invoice that is
    nearly identical in content (cosine distance < NEAR_DUP_DISTANCE) but carries a
    DIFFERENT, non-empty invoice number. This is the fuzzy match an exact-key lookup
    in a database would miss."""
    for n in neighbors:  # sorted nearest-first
        if n["distance"] >= NEAR_DUP_DISTANCE:
            break
        other = n["invoice_number"]
        if other and str(other) != str(invoice_number or ""):
            return True, n["key"], n["distance"], other
    return False, None, None, None


def category_anomaly(neighbors, history_count):
    """The invoice is an unusual kind of work for this vendor: even the closest
    thing in their history is far away (min cosine distance > CATEGORY_DISTANCE).
    Needs at least a few historical invoices to know what 'normal' looks like."""
    if history_count < 3 or not neighbors:
        return False, None, None
    nearest = neighbors[0]  # smallest distance
    if nearest["distance"] > CATEGORY_DISTANCE:
        return True, nearest["distance"], nearest["key"]
    return False, nearest["distance"], nearest["key"]


def assess(amount, avg, approved_count, exact_dup, near_dup, cat_anomaly):
    """Deterministic verdict. A duplicate (exact or near) is a re-submission and is
    rejected outright; otherwise an amount or category anomaly is flagged for
    review. The model never decides this."""
    amount_anomaly = bool(avg and approved_count >= 3 and float(amount) > ANOMALY_FACTOR * avg)
    if exact_dup or near_dup:
        status = "REJECTED"
    elif amount_anomaly or cat_anomaly:
        status = "FLAGGED"
    else:
        status = "PASS"
    return status, amount_anomaly


def recommend(status, vendor, amount, avg, amount_anomaly, exact_dup, near_dup, cat_anomaly):
    """Phrase the verdict in one sentence with a Strands agent."""
    agent = Agent(
        model=model,
        system_prompt="You are an invoice validator. Write ONE short sentence (no JSON, "
        "no preamble) explaining the recommendation to a reviewer.",
    )
    result = agent(
        f"Vendor: {vendor}. Amount: {amount}. Vendor approved average: {avg}. "
        f"Amount anomaly: {amount_anomaly}. Exact duplicate: {exact_dup}. "
        f"Near-duplicate resubmission: {near_dup}. Unusual category for vendor: {cat_anomaly}. "
        f"Decision: {status}."
    )
    return str(result).strip()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FLAGGED_DIR, exist_ok=True)
    print(f"[validator] watching {INPUT_DIR}; memory index {VECTOR_INDEX}", flush=True)
    while True:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            os.makedirs(FLAGGED_DIR, exist_ok=True)
            for name in sorted(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else []:
                if not name.endswith(".json"):
                    continue
                invoice_id = name[:-5]
                out_path = os.path.join(OUTPUT_DIR, f"{invoice_id}.json")
                if os.path.exists(out_path):
                    continue
                with open(os.path.join(INPUT_DIR, name)) as fh:
                    doc = json.load(fh)
                vendor = doc.get("vendor", "unknown")
                amount = doc.get("amount", 0) or 0
                invoice_number = doc.get("invoice_number")
                category = doc.get("category")
                print(f"[validator] validating {invoice_id} ({vendor} ${amount})", flush=True)

                # One embedding of the invoice content drives every memory lookup.
                text = canonical_text(vendor, category, line_item_summary(doc))
                qvec = embed(text)

                avg, approved_count, neighbors = query_history(qvec, vendor)
                exact_dup, exact_key = exact_duplicate(qvec, vendor, invoice_number)
                near_dup, near_key, near_dist, near_num = near_duplicate(neighbors, invoice_number)
                cat_anom, cat_dist, cat_key = category_anomaly(neighbors, len(neighbors))

                status, amount_anomaly = assess(
                    amount, avg, approved_count, exact_dup, near_dup, cat_anom)
                recommendation = recommend(
                    status, vendor, amount, avg, amount_anomaly, exact_dup, near_dup, cat_anom)

                envelope = {
                    "invoice_id": invoice_id,
                    "invoice_number": invoice_number,
                    "vendor": vendor,
                    "amount": amount,
                    "category": category,
                    "validation_status": status,
                    "embed_text": text,
                    "checks": {
                        "amount_within_historical_range": not amount_anomaly,
                        "not_duplicate": not (exact_dup or near_dup),
                        "category_matches_history": not cat_anom,
                    },
                    "history": {"average": avg, "count": approved_count, "matches": neighbors[:5]},
                    "anomaly": {
                        "flagged": amount_anomaly,
                        "factor": round(float(amount) / avg, 1) if (amount_anomaly and avg) else None,
                        "average": avg,
                        "current_amount": amount,
                    },
                    "duplicate": {
                        "flagged": exact_dup,
                        "matched_invoice_number": invoice_number if exact_dup else None,
                        "matched_key": exact_key,
                    },
                    "near_duplicate": {
                        "flagged": near_dup,
                        "matched_key": near_key,
                        "matched_invoice_number": near_num,
                        "distance": near_dist,
                        "threshold": NEAR_DUP_DISTANCE,
                    },
                    "category_anomaly": {
                        "flagged": cat_anom,
                        "nearest_key": cat_key,
                        "distance": cat_dist,
                        "threshold": CATEGORY_DISTANCE,
                    },
                    "recommendation": recommendation,
                    "validated_at": datetime.datetime.utcnow().isoformat() + "Z",
                }
                write_json(out_path, envelope)
                if status != "PASS":
                    write_json(os.path.join(FLAGGED_DIR, f"{invoice_id}.json"), envelope)
                print(f"[validator] {invoice_id} -> {status}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[validator] {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
