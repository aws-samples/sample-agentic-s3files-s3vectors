#!/usr/bin/env bash
# Delete everything the walkthrough created, in dependency order.
# Usage: source config_files/env.sh && ./scripts/cleanup.sh
set -uo pipefail
: "${STACK_NAME:?source config_files/env.sh first}"
read -r -p "Delete InvoiceIQ in account ${ACCOUNT_ID} / ${AWS_REGION}? Type 'yes': " ok
[ "$ok" = "yes" ] || { echo "Aborted."; exit 1; }

echo "== Kubernetes resources (deleting the PVC removes its access point)"
kubectl delete namespace invoiceiq --ignore-not-found --wait=true
kubectl delete storageclass s3files-sc --ignore-not-found

if [ -n "${S3FILES_FS_ID:-}" ]; then
  echo "== S3 Files file system $S3FILES_FS_ID"
  aws s3files delete-file-system-policy --file-system-id "$S3FILES_FS_ID" 2>/dev/null
  for mt in $(aws s3files list-mount-targets --file-system-id "$S3FILES_FS_ID" \
      --query 'mountTargets[].mountTargetId' --output text); do
    aws s3files delete-mount-target --mount-target-id "$mt"
  done
  for ap in $(aws s3files list-access-points --file-system-id "$S3FILES_FS_ID" \
      --query 'accessPoints[].accessPointId' --output text); do
    aws s3files delete-access-point --access-point-id "$ap"
  done
  for _ in $(seq 1 40); do
    n="$(aws s3files list-mount-targets --file-system-id "$S3FILES_FS_ID" \
      --query 'length(mountTargets)' --output text 2>/dev/null || echo 0)"
    [ "$n" = "0" ] && break; sleep 15
  done
  aws s3files delete-file-system --file-system-id "$S3FILES_FS_ID"
  echo "Waiting for the file system to finish deleting..."
  while aws s3files list-file-systems --bucket "$BUCKET_ARN" \
      --query 'fileSystems[0].fileSystemId' --output text 2>/dev/null | grep -q '^fs-'; do
    sleep 15
  done
fi

echo "== S3 Vectors"
aws s3vectors delete-index --vector-bucket-name "$VECTOR_BUCKET" --index-name "$VECTOR_INDEX" 2>/dev/null
aws s3vectors delete-vector-bucket --vector-bucket-name "$VECTOR_BUCKET" 2>/dev/null

echo "== Empty the versioned buckets (all versions + delete markers)"
python3 - "$BUCKET_NAME" "$ARTIFACT_BUCKET" <<'PY'
import sys, boto3
for name in sys.argv[1:]:
    try:
        boto3.resource("s3").Bucket(name).object_versions.delete()
        print(f"emptied {name}")
    except Exception as e:
        print(f"skip {name}: {e}")
PY

echo "== EKS cluster (10-15 minutes; also removes its Pod Identity associations)"
eksctl delete cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --wait

echo "== CloudFormation stack"
aws cloudformation delete-stack --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" && echo "All resources deleted."
