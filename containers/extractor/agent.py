"""InvoiceIQ — Extractor (Strands Agents SDK).

Watches /mnt/s3files/classified/. For each newly classified invoice it re-reads
the PDF from the mount and uses a Strands agent to pull out the structured fields
(vendor, amount, line items, dates), then writes them to extracted/.

    classified/<id>.json  --> extracted/<id>.json
    incoming/<id>.pdf  (read again for full-fidelity extraction)
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

INPUT_DIR = os.path.join(MOUNT, "classified")
INCOMING_DIR = os.path.join(MOUNT, "incoming")
OUTPUT_DIR = os.path.join(MOUNT, "extracted")

model = BedrockModel(model_id=MODEL_ID, region_name=REGION)

SYSTEM_PROMPT = (
    "You are an invoice data extraction agent. Read the attached invoice and respond "
    "with ONLY valid JSON, no prose, in exactly this shape:\n"
    '{ "vendor": "vendor name",'
    '  "invoice_number": "invoice id",'
    '  "amount": 0.0,'
    '  "currency": "USD",'
    '  "invoice_date": "YYYY-MM-DD",'
    '  "due_date": "YYYY-MM-DD or null",'
    '  "line_items": [ { "description": "", "amount": 0.0 } ],'
    '  "payment_terms": "e.g. Net 30",'
    '  "reasoning": "ONE short sentence on where you found the total and invoice number" }\n'
    "amount MUST be the numeric grand total."
)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def extract(invoice_id, pdf_bytes):
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
    result = agent([
        {"document": {"format": "pdf", "name": f"invoice-{invoice_id}",
                      "source": {"bytes": pdf_bytes}}},
        {"text": "Extract the fields from this invoice."},
    ])
    text = str(result)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"[extractor] watching {INPUT_DIR}", flush=True)
    while True:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            for name in sorted(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else []:
                if not name.endswith(".json"):
                    continue
                invoice_id = name[:-5]
                out_path = os.path.join(OUTPUT_DIR, f"{invoice_id}.json")
                pdf_path = os.path.join(INCOMING_DIR, f"{invoice_id}.pdf")
                if os.path.exists(out_path) or not os.path.exists(pdf_path):
                    continue
                print(f"[extractor] extracting {invoice_id}", flush=True)
                with open(os.path.join(INPUT_DIR, name)) as fh:
                    classification = json.load(fh)
                with open(pdf_path, "rb") as fh:
                    pdf_bytes = fh.read()
                fields = extract(invoice_id, pdf_bytes)
                fields["invoice_id"] = invoice_id
                fields["document_type"] = classification.get("document_type")
                fields["category"] = classification.get("category")
                fields["extracted_at"] = datetime.datetime.utcnow().isoformat() + "Z"
                write_json(out_path, fields)
                print(f"[extractor] {invoice_id} -> {fields.get('vendor')} ${fields.get('amount')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[extractor] {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
