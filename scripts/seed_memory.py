#!/usr/bin/env python3
"""Create the Amazon S3 Vectors bucket + index and seed it with vendor history.

The pipeline builds memory on its own (the embedder stores every invoice it sees). This
baseline makes the very first invoice history-aware and gives the anomaly checks something
to compare against. Safe to re-run.

Usage: source config_files/env.sh && python3 scripts/seed_memory.py
Requires boto3 >= 1.40 (pip install --upgrade "boto3>=1.40.0").
"""
import json, os, re, sys, time
import boto3
from botocore.exceptions import ClientError

if "s3vectors" not in boto3.session.Session().get_available_services():
    sys.exit(f"boto3 {boto3.__version__} does not know Amazon S3 Vectors. "
             'Run: pip install --upgrade "boto3>=1.40.0"')

REGION = os.environ["AWS_REGION"]
BUCKET = os.environ["VECTOR_BUCKET"]
INDEX = os.environ.get("VECTOR_INDEX", "invoice-history")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)

# These two helpers MUST stay identical to the validator and embedder agents.
def canonical_text(vendor, category, description):
    return " | ".join(p for p in (vendor, category, description) if p)

def normalize_vendor(vendor):
    s = re.sub(r"[^a-z0-9\s]", " ", (vendor or "").lower())
    suffixes = {"inc", "incorporated", "corp", "corporation", "co", "company",
                "llc", "ltd", "limited", "llp", "lp", "plc", "gmbh", "sa", "ag"}
    toks = [t for t in s.split() if t not in suffixes]
    return " ".join(toks) or (vendor or "").strip().lower()

def embed(text):
    r = bedrock.invoke_model(modelId=EMBED_MODEL,
                             body=json.dumps({"inputText": text, "dimensions": 1024}))
    return json.loads(r["body"].read())["embedding"]

def ensure(fn, **kw):
    try:
        fn(**kw)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ConflictException",):
            raise

ensure(s3v.create_vector_bucket, vectorBucketName=BUCKET)
ensure(s3v.create_index, vectorBucketName=BUCKET, indexName=INDEX,
       dataType="float32", dimension=1024, distanceMetric="cosine")

# (vendor, amount, category, description)
HISTORY = (
    [("Acme Corp", a, "IT Services", "Managed IT support services")
     for a in (4800, 5200, 4500, 5100, 4950, 5300, 4700, 5050)]
    + [("TechSupply Ltd", a, "Office Supplies", "Office supplies and stationery")
       for a in (2400, 2600, 2200)]
    + [("CloudServe Solutions", a, "Cloud Hosting", "Cloud hosting and infrastructure")
       for a in (7200, 6900, 7400)]
)

vectors = [{
    "key": f"seed-{vendor.lower().replace(' ', '-')}-{i:03d}",
    "data": {"float32": embed(canonical_text(vendor, cat, desc))},
    "metadata": {"vendor": vendor, "vendor_key": normalize_vendor(vendor),
                 "amount": str(amount), "category": cat, "outcome": "approved"},
} for i, (vendor, amount, cat, desc) in enumerate(HISTORY)]

for i in range(0, len(vectors), 10):
    s3v.put_vectors(vectorBucketName=BUCKET, indexName=INDEX, vectors=vectors[i:i + 10])

# Verify the seed is queryable before calling it done.
probe = embed(canonical_text("Acme Corp", "IT Services", "Managed IT support services"))
for attempt in range(1, 6):
    res = s3v.query_vectors(vectorBucketName=BUCKET, indexName=INDEX,
                            queryVector={"float32": probe}, topK=len(vectors) + 5,
                            returnMetadata=True)
    found = len(res.get("vectors", []))
    if found >= len(vectors):
        print(f"Seeded and verified {found} historical invoices in {BUCKET}/{INDEX}")
        sys.exit(0)
    print(f"Only {found}/{len(vectors)} queryable yet (attempt {attempt}/5); waiting...")
    time.sleep(10)
sys.exit("Seed verification failed: vectors not queryable")
