# InvoiceIQ dashboard (frontend)

React + Vite single-page app served by nginx on port 3000. nginx proxies `/api/` to the
`pipeline-api` Service, which reads the shared Amazon S3 Files mount and accepts invoice
uploads (`PUT /api/upload/:id`, written to `incoming/`).

The Service is `ClusterIP`; reach it with `kubectl port-forward` (see the top-level README).
The upload endpoint has no authentication, so do not expose this Service publicly.

Local dev: `npm ci && npm run dev`.
