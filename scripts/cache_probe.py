import json, os, sys, boto3

REGION  = os.environ.get("AWS_REGION") or boto3.Session().region_name or "us-west-2"
INDEX   = os.environ.get("VECTOR_INDEX", "invoice-history")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
BUCKET  = os.environ.get("VECTOR_BUCKET") or f"invoiceiq-memory-{ACCOUNT}-{REGION}"

# Distance under which the vendor + kind of work is already "known", so the
# classification can be reused instead of a fresh full-PDF classify pass.
# Recognition only: the amount, date, and invoice number are NEVER cached.
CACHE_DISTANCE = float(os.environ.get("CACHE_DISTANCE", "0.35"))

vendor = sys.argv[1] if len(sys.argv) > 1 else "Acme Corp"
text   = sys.argv[2] if len(sys.argv) > 2 else "Acme Corp | IT Services | Managed IT support - March"

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3v     = boto3.client("s3vectors", region_name=REGION)

vec = json.loads(bedrock.invoke_model(
    modelId="amazon.titan-embed-text-v2:0",
    body=json.dumps({"inputText": text, "dimensions": 1024}))["body"].read())["embedding"]

res = s3v.query_vectors(
    vectorBucketName=BUCKET, indexName=INDEX,
    queryVector={"float32": vec}, topK=1,
    returnDistance=True, returnMetadata=True, filter={"vendor": vendor})

hits = res.get("vectors", [])
print(f"Incoming: {text}")
if not hits:
    print(f"No history for {vendor}  ->  NEW: full processing (classify + extract)")
    raise SystemExit

nearest = hits[0]
d = float(nearest["distance"])
print(f"Nearest in memory: {nearest['key']}  distance={d:.3f}  (threshold {CACHE_DISTANCE})")
if d < CACHE_DISTANCE:
    print("RECOGNIZED  ->  reuse classification (skip the classify pass); extract amount/date/number fresh")
else:
    print("NEW         ->  full processing (classify + extract)")
