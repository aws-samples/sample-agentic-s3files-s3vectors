"""InvoiceIQ — Orchestrator.

The orchestrator is the only agent with an external trigger. When an invoice PDF
lands in the S3 bucket, S3 -> EventBridge -> SQS delivers one message. The
orchestrator reads that message, then *assigns work* by writing a task file onto
the shared Amazon S3 Files mount:

    /mnt/s3files/incoming/<id>.pdf        (the uploaded invoice, synced by S3 Files)
    /mnt/s3files/tasks/<id>.json          <-- orchestrator writes this

From here on there are no queues and no agent-to-agent calls. Every downstream
agent simply watches a directory on the mount, reads the previous stage's output
with open(), and writes its own. The filesystem is the message bus.

At intake it also does a cheap, non-LLM recognition check: it reads the PDF text
layer, parses the vendor and category, and asks S3 Vectors whether this vendor's
work is already well represented in memory. If so, the task is marked
`skip_classify` with a reusable `cached_classification`, and the classifier reuses
it instead of paying a full Nova pass. It never reuses the variable fields
(amount, date, invoice number) — those are always read fresh by the extractor, and
the authoritative history check still happens later, in the validator.
"""
import datetime
import json
import os
import time

import boto3
from pypdf import PdfReader

import recognition as R

REGION = os.environ.get("AWS_REGION", "us-east-1")
MOUNT = os.environ.get("S3_FILES_MOUNT", "/mnt/s3files")
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
VECTOR_BUCKET = os.environ.get("VECTOR_BUCKET", "")
VECTOR_INDEX = os.environ.get("VECTOR_INDEX", "invoice-history")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
CACHE_DISTANCE = float(os.environ.get("CACHE_DISTANCE", "0.35"))
MAX_INTAKE_ATTEMPTS = int(os.environ.get("MAX_INTAKE_ATTEMPTS", "6"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

INCOMING_DIR = os.path.join(MOUNT, "incoming")
TASKS_DIR = os.path.join(MOUNT, "tasks")

sqs = boto3.client("sqs", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)

# Bounded retry state for the S3 Files sync race: how many sweeps we have deferred
# an invoice whose PDF is not yet readable on the mount.
_intake_attempts = {}


def write_json(path, data):
    """Write atomically: write to <path>.tmp then rename into place."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def invoice_id_from_key(key):
    """contracts land as incoming/<id>.pdf — derive <id>."""
    base = os.path.basename(key)
    return base[:-4] if base.endswith(".pdf") else base


def embed(text):
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": 1024}),
    )
    return json.loads(resp["body"].read())["embedding"]


def read_pdf_text(pdf_path):
    """Cheap, non-LLM read of the PDF text layer. Best-effort: returns "" if the
    file has no extractable text (e.g. a scan), which makes recognition fall back
    to a full classify."""
    try:
        reader = PdfReader(pdf_path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        print(f"[orchestrator] pdf read failed: {exc}", flush=True)
        return ""


def query_neighbors(text, vendor):
    """Vendor-filtered nearest-neighbor lookup in S3 Vectors, using the same
    canonical embedding text the validator and embedder use. Returns neighbors as
    [{"key", "distance"}]; recognize() takes the minimum distance."""
    vec = embed(text)
    res = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": vec},
        topK=10,
        returnDistance=True,
        returnMetadata=True,
        filter={"vendor": vendor},
    )
    return [{"key": v.get("key"), "distance": float(v.get("distance", 1.0))}
            for v in res.get("vectors", [])]


def assign(invoice_id):
    task_path = os.path.join(TASKS_DIR, f"{invoice_id}.json")
    if os.path.exists(task_path):
        return False

    decision = {"skip_classify": False, "reason": "no_vector_store", "cached_classification": None}
    parsed = {"vendor": None}

    if VECTOR_BUCKET:
        text = read_pdf_text(os.path.join(INCOMING_DIR, f"{invoice_id}.pdf"))
        if not text.strip():
            # PDF not yet synced onto the mount (S3 Files lag). Defer so a later
            # sweep retries with recognition, rather than permanently marking it
            # novel. Bounded, so a genuinely text-less PDF (e.g. a scan) still gets
            # processed as a normal full classify instead of stalling forever.
            n = _intake_attempts.get(invoice_id, 0) + 1
            _intake_attempts[invoice_id] = n
            if n < MAX_INTAKE_ATTEMPTS:
                print(f"[orchestrator] {invoice_id} pdf not readable yet "
                      f"(attempt {n}/{MAX_INTAKE_ATTEMPTS}); deferring recognition", flush=True)
                return False
            print(f"[orchestrator] {invoice_id} pdf unreadable after {n} attempts; "
                  f"assigning full classify", flush=True)
        else:
            parsed = R.parse_intake(text)
            try:
                decision = R.recognize(parsed, query_neighbors, CACHE_DISTANCE)
            except Exception as exc:  # never let recognition block intake
                print(f"[orchestrator] recognition error, full classify: {exc}", flush=True)
                decision = {"skip_classify": False, "reason": "error", "cached_classification": None}

    task = R.build_task(invoice_id, parsed, decision)
    task["assigned_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    write_json(task_path, task)
    _intake_attempts.pop(invoice_id, None)
    print(f"[orchestrator] assigned {invoice_id} -> tasks/{invoice_id}.json "
          f"({decision.get('reason')}, skip_classify={task['skip_classify']})", flush=True)
    return True


def handle_message(msg):
    """An EventBridge 'Object Created' event wrapped in an SQS message."""
    body = json.loads(msg["Body"])
    detail = body.get("detail", body)
    key = detail.get("object", {}).get("key", "")
    if not key.startswith("incoming/") or not key.endswith(".pdf"):
        return
    assign(invoice_id_from_key(key))


def main():
    os.makedirs(TASKS_DIR, exist_ok=True)
    print(f"[orchestrator] watching SQS {QUEUE_URL}; writing tasks to {TASKS_DIR}", flush=True)
    while True:
        try:
            # The SQS long-poll is the only external trigger in the whole system.
            if QUEUE_URL:
                resp = sqs.receive_message(
                    QueueUrl=QUEUE_URL,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=20,
                )
                for msg in resp.get("Messages", []):
                    try:
                        handle_message(msg)
                    finally:
                        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

            # Safety net: also sweep incoming/ directly so a demo works even if
            # the SQS trigger is slow. Idempotent (assign() skips existing tasks).
            if os.path.isdir(INCOMING_DIR):
                for name in sorted(os.listdir(INCOMING_DIR)):
                    if name.endswith(".pdf"):
                        assign(name[:-4])
        except Exception as exc:  # noqa: BLE001
            print(f"[orchestrator] {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
