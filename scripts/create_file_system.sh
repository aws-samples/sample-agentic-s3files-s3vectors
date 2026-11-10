#!/usr/bin/env bash
# Create the Amazon S3 Files file system on the invoice bucket, set its synchronization
# rules and file-system policy, and add one mount target per AZ in the cluster's private
# subnets (with the cluster security group, so EKS nodes can reach NFS port 2049).
# Usage: source config_files/env.sh && ./scripts/create_file_system.sh && source config_files/env.sh
set -euo pipefail
: "${BUCKET_ARN:?source config_files/env.sh first}"
: "${CLUSTER_SG:?create the EKS cluster and re-source env.sh first}"

fs_id="$(aws s3files list-file-systems --bucket "$BUCKET_ARN" \
  --query 'fileSystems[0].fileSystemId' --output text)"
if [ -z "$fs_id" ] || [ "$fs_id" = "None" ]; then
  echo "Creating file system on $BUCKET_ARN"
  fs_id="$(aws s3files create-file-system --bucket "$BUCKET_ARN" \
    --role-arn "$S3FILES_ROLE_ARN" --accept-bucket-warning \
    --query 'fileSystemId' --output text)"
fi
fs_arn="$(aws s3files list-file-systems --bucket "$BUCKET_ARN" \
  --query 'fileSystems[0].fileSystemArn' --output text)"
echo "File system: $fs_id"

echo "Waiting for the file system to become available..."
for _ in $(seq 1 60); do
  st="$(aws s3files get-file-system --file-system-id "$fs_id" --query 'status' --output text 2>/dev/null || true)"
  case "$(printf '%s' "$st" | tr '[:upper:]' '[:lower:]')" in
    available) break ;;
    *error*|*fail*) echo "File system status: $st"; exit 1 ;;
  esac
  sleep 10
done

# Invoice PDFs are larger than the 128 KiB default import threshold; raise it under incoming/.
aws s3files put-synchronization-configuration --file-system-id "$fs_id" \
  --import-data-rules '[
    {"prefix": "", "trigger": "ON_DIRECTORY_FIRST_ACCESS", "sizeLessThan": 131072},
    {"prefix": "incoming/", "trigger": "ON_DIRECTORY_FIRST_ACCESS", "sizeLessThan": 10485760}
  ]' \
  --expiration-data-rules '[{"daysAfterLastAccess": 7}]' >/dev/null
echo "Synchronization rules set."

# Only the CSI node role may mount, write, and act as root on this file system.
aws s3files put-file-system-policy --file-system-id "$fs_id" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"AllowEksCsiNodeMount\",
    \"Effect\": \"Allow\",
    \"Principal\": {\"AWS\": \"${CSI_NODE_ROLE_ARN}\"},
    \"Action\": [\"s3files:ClientMount\", \"s3files:ClientWrite\", \"s3files:ClientRootAccess\"],
    \"Resource\": \"${fs_arn}\"
  }]
}" >/dev/null
echo "File-system policy set."

existing="$(aws s3files list-mount-targets --file-system-id "$fs_id" \
  --query 'mountTargets[].subnetId' --output text)"
for subnet in $CLUSTER_PRIVATE_SUBNETS; do
  if printf '%s' "$existing" | grep -q "$subnet"; then
    echo "Mount target already exists in $subnet"; continue
  fi
  echo "Creating mount target in $subnet"
  aws s3files create-mount-target --file-system-id "$fs_id" \
    --subnet-id "$subnet" --security-groups "$CLUSTER_SG" >/dev/null
done

echo "Waiting for mount targets (typically 2-5 minutes)..."
for _ in $(seq 1 60); do
  pending="$(aws s3files list-mount-targets --file-system-id "$fs_id" \
    --query "length(mountTargets[?status!='available'])" --output text)"
  [ "$pending" = "0" ] && break
  sleep 15
done
aws s3files list-mount-targets --file-system-id "$fs_id" \
  --query 'mountTargets[].{Id:mountTargetId,Subnet:subnetId,Status:status}' --output table
[ "${pending:-1}" = "0" ] || { echo "Mount targets not all available yet; re-run to wait."; exit 1; }
echo "Done. Run: source config_files/env.sh"
