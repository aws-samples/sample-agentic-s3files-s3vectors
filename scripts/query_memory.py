import json, os, boto3

# Fall back to sane defaults so this works even in a fresh terminal
region = os.environ.get("AWS_REGION") or boto3.Session().region_name or "us-west-2"
index = os.environ.get("VECTOR_INDEX", "invoice-history")
account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
bucket = os.environ.get("VECTOR_BUCKET") or f"invoiceiq-memory-{account}-{region}"
bedrock = boto3.client("bedrock-runtime", region_name=region)
s3v = boto3.client("s3vectors", region_name=region)

resp = bedrock.invoke_model(
    modelId="amazon.titan-embed-text-v2:0",
    body=json.dumps({"inputText": "Acme Corp | IT Services | Managed IT support", "dimensions": 1024}),
)
qvec = json.loads(resp["body"].read())["embedding"]

res = s3v.query_vectors(
    vectorBucketName=bucket, indexName=index,
    queryVector={"float32": qvec}, topK=10,
    returnDistance=True, returnMetadata=True,
    filter={"vendor": "Acme Corp"},
)
amounts = []
for v in res["vectors"]:
    m = v["metadata"]
    amounts.append(float(m["amount"]))
    print(f"{v['key']:28} ${m['amount']:>7}  distance={v['distance']:.3f}")
if amounts:
    print(f"\nAcme Corp average: ${sum(amounts)/len(amounts):,.0f} over {len(amounts)} invoices")
