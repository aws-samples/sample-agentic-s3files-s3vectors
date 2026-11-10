#!/usr/bin/env bash
# Build and push all seven images with AWS CodeBuild (no local Docker needed).
# Usage: source config_files/env.sh && ./scripts/build_images.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ARTIFACT_BUCKET:?source config_files/env.sh first}"
: "${IMAGE_BUILD_PROJECT:?source config_files/env.sh first}"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
zip -qr "$tmp/invoiceiq-src.zip" containers config_files/buildspec.yml \
  -x '*/node_modules/*' -x '*/dist/*' -x '*/__pycache__/*' -x '*.pyc'
aws s3 cp "$tmp/invoiceiq-src.zip" "s3://${ARTIFACT_BUCKET}/source/invoiceiq-src.zip" --only-show-errors

build_id="$(aws codebuild start-build --project-name "$IMAGE_BUILD_PROJECT" --query 'build.id' --output text)"
echo "Started $build_id (about 5-10 minutes)"
while true; do
  status="$(aws codebuild batch-get-builds --ids "$build_id" --query 'builds[0].buildStatus' --output text)"
  case "$status" in
    SUCCEEDED) echo "Images built and pushed."; break ;;
    IN_PROGRESS) sleep 20 ;;
    *) echo "Build ended with status $status. Logs:"; \
       echo "  aws logs tail /aws/codebuild/invoiceiq-image-builder --since 1h"; exit 1 ;;
  esac
done
for svc in orchestrator classifier extractor validator embedder pipeline-api frontend; do
  aws ecr describe-images --repository-name "invoiceiq-${svc}" --image-ids imageTag=latest \
    --query 'imageDetails[0].imagePushedAt' --output text | sed "s/^/  invoiceiq-${svc}: pushed /"
done
