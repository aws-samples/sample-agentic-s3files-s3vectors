# Source this file (do not execute it):  source config_files/env.sh
# Re-run it any time you open a new terminal. Every value is derived, nothing is secret.

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export STACK_NAME="${STACK_NAME:-invoiceiq-infra}"
export CLUSTER_NAME="${CLUSTER_NAME:-invoiceiq}"
export AGENT_MODEL="${AGENT_MODEL:-us.amazon.nova-pro-v1:0}"
export EMBED_MODEL="${EMBED_MODEL:-amazon.titan-embed-text-v2:0}"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

_out() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  export BUCKET_NAME="$(_out InvoiceBucketName)"
  export BUCKET_ARN="$(_out InvoiceBucketArn)"
  export ARTIFACT_BUCKET="$(_out ArtifactBucketName)"
  export VECTOR_BUCKET="$(_out VectorBucketName)"
  export VECTOR_INDEX="$(_out VectorIndexName)"
  export SQS_QUEUE_URL="$(_out IntakeQueueUrl)"
  export S3FILES_ROLE_ARN="$(_out S3FilesDataAccessRoleArn)"
  export CSI_CONTROLLER_ROLE_ARN="$(_out CsiControllerRoleArn)"
  export CSI_NODE_ROLE_ARN="$(_out CsiNodeRoleArn)"
  export AGENT_ROLE_ARN="$(_out AgentRoleArn)"
  export PIPELINE_API_ROLE_ARN="$(_out PipelineApiRoleArn)"
  export IMAGE_BUILD_PROJECT="$(_out ImageBuildProjectName)"
  export ECR_REGISTRY="$(_out EcrRegistry)"
else
  echo "Stack $STACK_NAME not found yet in $AWS_REGION (fine before step 1)."
fi

if aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  export CLUSTER_VPC_ID="$(aws eks describe-cluster --name "$CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text)"
  export CLUSTER_SG="$(aws eks describe-cluster --name "$CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)"
  # Private subnets of the cluster, one per AZ (S3 Files allows one mount target per AZ).
  export CLUSTER_PRIVATE_SUBNETS="$(aws ec2 describe-subnets \
    --subnet-ids $(aws eks describe-cluster --name "$CLUSTER_NAME" \
      --query 'cluster.resourcesVpcConfig.subnetIds' --output text) \
    --filters Name=map-public-ip-on-launch,Values=false \
    --query 'Subnets[].[AvailabilityZone,SubnetId]' --output text \
    | sort -u -k1,1 | awk '{print $2}' | tr '\n' ' ')"
fi

if [ -n "${BUCKET_ARN:-}" ]; then
  _fs="$(aws s3files list-file-systems --bucket "$BUCKET_ARN" \
    --query 'fileSystems[0].fileSystemId' --output text 2>/dev/null)"
  if [ -n "$_fs" ] && [ "$_fs" != "None" ]; then
    export S3FILES_FS_ID="$_fs"
    export S3FILES_FS_ARN="$(aws s3files list-file-systems --bucket "$BUCKET_ARN" \
      --query 'fileSystems[0].fileSystemArn' --output text)"
  fi
fi

echo "Region:     $AWS_REGION   Account: $ACCOUNT_ID"
echo "Stack:      $STACK_NAME   Cluster: $CLUSTER_NAME"
echo "Bucket:     ${BUCKET_NAME:-<not yet>}"
echo "Vectors:    ${VECTOR_BUCKET:-<not yet>}/${VECTOR_INDEX:-}"
echo "VPC / SG:   ${CLUSTER_VPC_ID:-<not yet>} / ${CLUSTER_SG:-<not yet>}"
echo "Subnets:    ${CLUSTER_PRIVATE_SUBNETS:-<not yet>}"
echo "FileSystem: ${S3FILES_FS_ID:-<not yet>}"
