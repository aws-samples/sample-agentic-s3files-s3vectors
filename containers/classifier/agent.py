"""InvoiceIQ — Classifier (Strands Agents SDK).

Watches /mnt/s3files/tasks/ for work the orchestrator assigned. For each task it
reads the invoice PDF off the shared mount and uses a Strands agent (backed by
Amazon Bedrock) to decide what kind of document it is and who the vendor is, then
writes the answer to classified/.

No SQS, no call to another agent: it reads a file and writes a file.

    tasks/<id>.json        --> classified/<id>.json
    incoming/<id>.pdf  (read directly from the mount, handed to the agent as a PDF)
"""
import datetime
import json
import os
import time

from strands import Agent
from strands.models import BedrockModel

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("AGENT_MODEL", "us.amazon.nova-pro-v1:0")
MOUNT = os.environ.get("S3_FILES_MOUNT", "/mnt/s3files")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

TASKS_DIR = os.path.join(MOUNT, "tasks")
INCOMING_DIR = os.path.join(MOUNT, "incoming")
OUTPUT_DIR = os.path.join(MOUNT, "classified")

# One Bedrock model, reused across invoices. A fresh Agent is created per invoice
# so each classification is independent (no conversation history carried over).
model = BedrockModel(model_id=MODEL_ID, region_name=REGION)

SYSTEM_PROMPT = (
    "You are an invoice classification agent. Read the attached document and respond "
    "with ONLY valid JSON, no prose, in exactly this shape:\n"
    '{ "document_type": "INVOICE | CREDIT_NOTE | PURCHASE_ORDER | RECEIPT",'
    '  "vendor_name": "the vendor/supplier name",'
    '  "category": "a short spend category, e.g. IT Services, Office Supplies",'
    '  "reasoning": "ONE short sentence on how you identified the type and vendor",'
    '  "confidence": 0.0 }'
)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def classify(invoice_id, pdf_bytes):
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
    result = agent([
        {"document": {"format": "pdf", "name": f"invoice-{invoice_id}",
                      "source": {"bytes": pdf_bytes}}},
        {"text": "Classify this document."},
    ])
    text = str(result)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"[classifier] watching {TASKS_DIR}; reading PDFs from {INCOMING_DIR}", flush=True)
    while True:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            for name in sorted(os.listdir(TASKS_DIR)) if os.path.isdir(TASKS_DIR) else []:
                if not name.endswith(".json"):
                    continue
                invoice_id = name[:-5]
                out_path = os.path.join(OUTPUT_DIR, f"{invoice_id}.json")
                pdf_path = os.path.join(INCOMING_DIR, f"{invoice_id}.pdf")
                if os.path.exists(out_path) or not os.path.exists(pdf_path):
                    continue
                task_path = os.path.join(TASKS_DIR, name)
                try:
                    with open(task_path) as fh:
                        task = json.load(fh)
                except Exception:
                    task = {}
                if task.get("skip_classify") and task.get("cached_classification"):
                    # Orchestrator recognized this invoice at intake: reuse the
                    # classification and skip the Bedrock call. Only the stable
                    # fields (vendor, category, document type) come from memory;
                    # the extractor still reads amount/date/number fresh.
                    cc = task["cached_classification"]
                    print(f"[classifier] {invoice_id} recognized at intake -> reusing classification (no Bedrock call)", flush=True)
                    result = {
                        "document_type": cc.get("document_type", "INVOICE"),
                        "vendor_name": cc.get("vendor"),
                        "category": cc.get("category"),
                        "reasoning": "Recognized at intake; classification reused from vendor memory, Nova classify skipped.",
                        "confidence": 1.0,
                        "classify_skipped": True,
                        "recognition": task.get("recognition"),
                    }
                else:
                    print(f"[classifier] classifying {invoice_id}", flush=True)
                    with open(pdf_path, "rb") as fh:
                        pdf_bytes = fh.read()
                    result = classify(invoice_id, pdf_bytes)
                    result["classify_skipped"] = False
                result["invoice_id"] = invoice_id
                result["classified_at"] = datetime.datetime.utcnow().isoformat() + "Z"
                write_json(out_path, result)
                print(f"[classifier] {invoice_id} -> {result.get('document_type')} / {result.get('vendor_name')} (skipped={result.get('classify_skipped')})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[classifier] {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
