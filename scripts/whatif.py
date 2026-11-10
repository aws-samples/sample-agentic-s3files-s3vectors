import json, os, sys, boto3

# Fall back to sane defaults so this works even in a fresh terminal
REGION = os.environ.get("AWS_REGION") or boto3.Session().region_name or "us-west-2"
INDEX = os.environ.get("VECTOR_INDEX", "invoice-history")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
BUCKET = os.environ.get("VECTOR_BUCKET") or f"invoiceiq-memory-{ACCOUNT}-{REGION}"

VENDOR = "Acme Corp"
amount = float(sys.argv[1]) if len(sys.argv) > 1 else 48000.0   # invoice amount to test
factor = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0       # ANOMALY_FACTOR to test

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v = boto3.client("s3vectors", region_name=REGION)

# Embed the canonical content text the agents use, then query this vendor's history
vec = json.loads(bedrock.invoke_model(
    modelId="amazon.titan-embed-text-v2:0",
    body=json.dumps({"inputText": f"{VENDOR} | IT Services | Managed IT support",
                     "dimensions": 1024}))["body"].read())["embedding"]

res = s3v.query_vectors(
    vectorBucketName=BUCKET, indexName=INDEX,
    queryVector={"float32": vec}, topK=10,
    returnDistance=True, returnMetadata=True, filter={"vendor": VENDOR})

# Average only the approved invoices, exactly like the validator
approved = [float(v["metadata"]["amount"]) for v in res.get("vectors", [])
            if str(v["metadata"].get("outcome", "")).lower() in ("approved", "pass")]
avg = round(sum(approved) / len(approved), 2) if approved else None

# Apply the same rule the validator uses -- but write nothing back
anomaly = bool(avg and len(approved) >= 3 and amount > factor * avg)

print(f"Vendor:            {VENDOR}")
print(f"Approved average:  ${avg} over {len(approved)} invoices")
if avg:
    print(f"This invoice:      ${amount:,.0f}  ({amount/avg:.1f}x the average)")
print(f"Threshold:         {factor}x  (ANOMALY_FACTOR)")
print(f"Verdict:           {'FLAGGED (amount anomaly)' if anomaly else 'PASS'}")
