"""InvoiceIQ — Embedder (builds the memory).

Watches /mnt/s3files/validated/. For each finished invoice it generates an
embedding with Bedrock Titan and stores it in Amazon S3 Vectors together with the
vendor, amount, category and outcome. That single put is what lets the validator
recognise the next invoice from the same vendor — the system gets smarter with
every document it sees.

    validated/<id>.json  --> S3 Vectors put  (+ memory/<id>.json marker on the mount)

The validator writes the exact text it embedded into the envelope as `embed_text`.
The embedder stores that same string, so the vector it stores is identical to the
one the validator queried with, and history always lines up. If `embed_text` is
missing (older envelope), it falls back to the shared canonical_text().
"""
import datetime
import json
import os
import re
import time

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
MOUNT = os.environ.get("S3_FILES_MOUNT", "/mnt/s3files")
VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", "")
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "invoice-history")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

INPUT_DIR = os.path.join(MOUNT, "validated")
MEMORY_DIR = os.path.join(MOUNT, "memory")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def canonical_text(vendor, category, description):
    """Fallback content embedding text — keep identical to the validator and seed."""
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


def embed(text):
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024}),
    )
    return json.loads(resp["body"].read())["embedding"]


def remember(invoice_id, invoice_number, vendor, amount, category, outcome, text):
    vec = embed(text)
    s3v.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        vectors=[{
            "key": invoice_id,
            "data": {"float32": vec},
            "metadata": {
                "vendor": vendor,
                "vendor_key": normalize_vendor(vendor),
                "invoice_number": str(invoice_number or ""),
                "amount": str(int(round(float(amount)))),
                "category": category or "",
                "outcome": outcome,
            },
        }],
    )
    # Hand back a small, human-readable view of what was embedded so the marker
    # (and the dashboard) can show the text -> vector -> metadata mapping.
    return {
        "embedded_text": text,
        "vector_dim": len(vec),
        "vector_preview": [round(float(x), 4) for x in vec[:8]],
    }


def main():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    print(f"[embedder] watching {INPUT_DIR}; writing to S3 Vectors {VECTOR_INDEX}", flush=True)
    while True:
        try:
            os.makedirs(MEMORY_DIR, exist_ok=True)
            for name in sorted(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else []:
                if not name.endswith(".json"):
                    continue
                invoice_id = name[:-5]
                marker = os.path.join(MEMORY_DIR, f"{invoice_id}.json")
                if os.path.exists(marker):
                    continue
                with open(os.path.join(INPUT_DIR, name)) as fh:
                    doc = json.load(fh)
                vendor = doc.get("vendor", "unknown")
                amount = doc.get("amount", 0) or 0
                invoice_number = doc.get("invoice_number")
                category = doc.get("category")
                outcome = "approved" if doc.get("validation_status") == "PASS" else "flagged"
                # Prefer the exact text the validator embedded; fall back if absent.
                text = doc.get("embed_text") or canonical_text(vendor, category, doc.get("description"))
                if not VECTOR_BUCKET:
                    print("[embedder] VECTOR_BUCKET not set; skipping", flush=True)
                    continue
                print(f"[embedder] remembering {invoice_id} ({vendor} ${amount}, {outcome})", flush=True)
                embed_info = remember(invoice_id, invoice_number, vendor, amount, category, outcome, text)
                write_json(marker, {
                    "invoice_id": invoice_id,
                    "invoice_number": invoice_number,
                    "vendor": vendor,
                    "amount": amount,
                    "category": category,
                    "outcome": outcome,
                    "index": VECTOR_INDEX,
                    "remembered_at": datetime.datetime.utcnow().isoformat() + "Z",
                    **embed_info,
                })
                print(f"[embedder] {invoice_id} stored in memory", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[embedder] {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
