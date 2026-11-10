import datetime, json, os, re, time
import boto3
from strands import Agent
from strands.models import BedrockModel

# --- Configuration (injected via environment variables) ---
REGION       = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID     = os.environ.get("AGENT_MODEL", "us.amazon.nova-pro-v1:0")
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
MOUNT        = os.environ.get("S3_FILES_MOUNT", "/mnt/s3files")
VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", "")
VECTOR_INDEX  = os.environ.get("VECTOR_INDEX", "invoice-history")
ANOMALY_FACTOR    = float(os.environ.get("ANOMALY_FACTOR", "3"))       # amount > 3x average
NEAR_DUP_DISTANCE = float(os.environ.get("NEAR_DUP_DISTANCE", "0.10")) # near-duplicate cutoff
CATEGORY_DISTANCE = float(os.environ.get("CATEGORY_DISTANCE", "0.45")) # unusual-category cutoff
POLL_SECONDS      = int(os.environ.get("POLL_SECONDS", "5"))

# Directories on the shared Amazon S3 Files mount: the validator reads extracted/, writes validated/
INPUT_DIR   = os.path.join(MOUNT, "extracted")
OUTPUT_DIR  = os.path.join(MOUNT, "validated")
FLAGGED_DIR = os.path.join(MOUNT, "flagged")

# AWS clients
# - Strands Agent handles the LLM reasoning (recommendation generation)
# - boto3 handles embeddings (Titan) and Amazon S3 Vectors (no Strands SDK wrapper for these)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v     = boto3.client("s3vectors", region_name=REGION)
model   = BedrockModel(model_id=MODEL_ID, region_name=REGION)


# --- Helpers ---

def write_json(path, data):
    """Atomic write: write to .tmp then rename, so readers never see a partial file."""
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


def canonical_text(vendor, category, description):
    """The text we embed. It describes the WORK (vendor, category, line items),
    not the amount, so similar invoices land close together in the vector space
    and unusual ones stand apart. Must match the embedder and the seed."""
    return " | ".join(p for p in (vendor, category, description) if p)


def normalize_vendor(vendor):
    """Stable key so "Acme Corp" and "Acme Corporation" resolve to the same
    vendor. Lowercase, drop punctuation and common company suffixes, collapse
    whitespace. Must match the embedder and the seed, or history lookups miss."""
    s = re.sub(r"[^a-z0-9\s]", " ", (vendor or "").lower())
    suffixes = {"inc", "incorporated", "corp", "corporation", "co", "company",
                "llc", "ltd", "limited", "llp", "lp", "plc", "gmbh", "sa", "ag"}
    toks = [t for t in s.split() if t not in suffixes]
    return " ".join(toks) or (vendor or "").strip().lower()


def line_item_summary(doc):
    """A short description of the work, from the extracted line items."""
    items = doc.get("line_items") or []
    parts = [str(li.get("description", "")).strip() for li in items if li.get("description")]
    return "; ".join(parts)[:400] if parts else str(doc.get("description", "")).strip()


def embed(text):
    """Generate a 1024-dim vector using Titan Embeddings V2."""
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024}))
    return json.loads(resp["body"].read())["embedding"]


# --- Memory lookups (Amazon S3 Vectors) ---

def query_history(qvec, vendor):
    """This vendor's history: nearest prior invoices (with cosine distance) and the
    approved average. One vendor-filtered query drives all three memory checks."""
    if not VECTOR_BUCKET:
        return None, 0, []
    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET, indexName=VECTOR_INDEX,
        queryVector={"float32": qvec}, topK=10,
        returnDistance=True, returnMetadata=True,
        filter={"vendor_key": normalize_vendor(vendor)},  # this vendor's history, normalized
    )
    neighbors, approved = [], []
    for v in res.get("vectors", []):
        meta = v.get("metadata", {})
        amt = float(meta.get("amount", 0) or 0)
        neighbors.append({"key": v["key"], "amount": amt,
                          "invoice_number": str(meta.get("invoice_number", "") or ""),
                          "distance": round(float(v.get("distance", 0)), 4)})
        if str(meta.get("outcome", "approved")).lower() in ("approved", "pass"):
            approved.append(amt)
    neighbors.sort(key=lambda n: n["distance"])
    avg = round(sum(approved) / len(approved), 2) if approved else None
    return avg, len(approved), neighbors


def exact_duplicate(qvec, invoice_number):
    """Exact re-submission: a prior invoice with the SAME invoice number. Keyed on
    the invoice number, so two different invoices that total the same never collide."""
    if not VECTOR_BUCKET or not invoice_number:
        return False, None
    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET, indexName=VECTOR_INDEX,
        queryVector={"float32": qvec}, topK=5,
        returnDistance=True, returnMetadata=True,
        filter={"invoice_number": str(invoice_number)},  # exact match on invoice number
    )
    for v in res.get("vectors", []):
        if str(v.get("metadata", {}).get("invoice_number")) == str(invoice_number):
            return True, v["key"]
    return False, None


def near_duplicate(neighbors, invoice_number):
    """Re-submission with the number changed: a prior invoice that is nearly
    identical in content (distance < NEAR_DUP_DISTANCE) but carries a DIFFERENT,
    non-empty invoice number. This is the fuzzy match an exact-key check misses."""
    for n in neighbors:  # nearest first
        if n["distance"] >= NEAR_DUP_DISTANCE:
            break
        if n["invoice_number"] and n["invoice_number"] != str(invoice_number or ""):
            return True, n["key"], n["distance"], n["invoice_number"]
    return False, None, None, None


def category_anomaly(neighbors):
    """Unusual kind of work for this vendor: even the closest thing in their
    history is far away (distance > CATEGORY_DISTANCE). Needs some history first."""
    if len(neighbors) < 3:
        return False, None, None
    nearest = neighbors[0]
    return (nearest["distance"] > CATEGORY_DISTANCE), nearest["distance"], nearest["key"]


# --- Decision logic (deterministic, no LLM) ---

def assess(amount, avg, count, exact_dup, near_dup, cat_anom):
    """PASS / FLAGGED / REJECTED from the rules, not the model. A duplicate (exact
    or near) is a re-submission and is rejected; an amount or category anomaly is
    flagged for review."""
    anomaly = bool(avg and count >= 3 and float(amount) > ANOMALY_FACTOR * avg)
    if exact_dup or near_dup:
        status = "REJECTED"
    elif anomaly or cat_anom:
        status = "FLAGGED"
    else:
        status = "PASS"
    return status, anomaly


def recommend(status, vendor, amount, avg, anomaly, exact_dup, near_dup, cat_anom):
    """Use a Strands agent to write a one-sentence explanation for the reviewer."""
    agent = Agent(model=model, system_prompt=(
        "You are an invoice validator. Write ONE short sentence "
        "(no JSON, no preamble) explaining the recommendation to a reviewer."))
    return str(agent(
        f"Vendor: {vendor}. Amount: {amount}. Vendor approved average: {avg}. "
        f"Amount anomaly: {anomaly}. Exact duplicate: {exact_dup}. "
        f"Near-duplicate resubmission: {near_dup}. Unusual category: {cat_anom}. "
        f"Decision: {status}."
    )).strip()


# --- Main loop: poll extracted/, validate, write results ---

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FLAGGED_DIR, exist_ok=True)
    print(f"[validator] watching {INPUT_DIR}", flush=True)

    while True:
        try:
            for name in sorted(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else []:
                if not name.endswith(".json"):
                    continue
                cid = name[:-5]  # invoice ID = filename without .json
                out_path = os.path.join(OUTPUT_DIR, f"{cid}.json")
                if os.path.exists(out_path):
                    continue  # already validated, skip

                with open(os.path.join(INPUT_DIR, name)) as fh:
                    doc = json.load(fh)
                vendor = doc.get("vendor", "unknown")
                amount = doc.get("amount", 0) or 0
                invoice_number = doc.get("invoice_number")
                category = doc.get("category")

                # One content embedding drives every memory check
                text = canonical_text(vendor, category, line_item_summary(doc))
                qvec = embed(text)

                avg, count, neighbors = query_history(qvec, vendor)
                exact_dup, exact_key = exact_duplicate(qvec, invoice_number)
                near_dup, near_key, near_dist, near_num = near_duplicate(neighbors, invoice_number)
                cat_anom, cat_dist, cat_key = category_anomaly(neighbors)

                status, anomaly = assess(amount, avg, count, exact_dup, near_dup, cat_anom)

                envelope = {
                    "invoice_id": cid,
                    "invoice_number": invoice_number,
                    "vendor": vendor,
                    "amount": amount,
                    "category": category,
                    "validation_status": status,
                    "embed_text": text,  # the embedder stores this exact text
                    "history": {"average": avg, "count": count, "matches": neighbors[:5]},
                    "anomaly": {
                        "flagged": anomaly,
                        "factor": round(float(amount) / avg, 1) if (anomaly and avg) else None,
                        "average": avg, "current_amount": amount,
                    },
                    "duplicate": {
                        "flagged": exact_dup,
                        "matched_invoice_number": invoice_number if exact_dup else None,
                        "matched_key": exact_key,
                    },
                    "near_duplicate": {
                        "flagged": near_dup, "matched_key": near_key,
                        "matched_invoice_number": near_num, "distance": near_dist,
                    },
                    "category_anomaly": {
                        "flagged": cat_anom, "nearest_key": cat_key, "distance": cat_dist,
                    },
                    "recommendation": recommend(status, vendor, amount, avg, anomaly,
                                                exact_dup, near_dup, cat_anom),
                    "validated_at": datetime.datetime.utcnow().isoformat() + "Z",
                }

                write_json(out_path, envelope)
                if status != "PASS":
                    write_json(os.path.join(FLAGGED_DIR, f"{cid}.json"), envelope)
                print(f"[validator] {cid} -> {status}", flush=True)

        except Exception as e:
            print(f"[validator] {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
