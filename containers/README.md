# Container images

| Dir | Image | Role |
|-----|-------|------|
| `orchestrator` | `invoiceiq-orchestrator` | Consumes the SQS intake message, recognizes known vendors, writes `tasks/` |
| `classifier` | `invoiceiq-classifier` | Strands + Nova: document type / vendor -> `classified/` |
| `extractor` | `invoiceiq-extractor` | Strands + Nova: structured fields -> `extracted/` |
| `validator` | `invoiceiq-validator` | Prebuilt validator (the walkthrough deploys `validator/agent.py` instead) |
| `embedder` | `invoiceiq-embedder` | Titan embeddings into S3 Vectors, writes `memory/` |
| `pipeline-api` | `invoiceiq-pipeline-api` | Read-only mount viewer + upload endpoint behind the dashboard |
| `frontend` | `invoiceiq-frontend` | React/Vite dashboard |

`scripts/build_images.sh` zips this directory, and the `invoiceiq-image-builder` CodeBuild
project builds every image and pushes it to ECR as `:latest`. Re-run the script after any change,
then `kubectl rollout restart deployment -n invoiceiq`.
