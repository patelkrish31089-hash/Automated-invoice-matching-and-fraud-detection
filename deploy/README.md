# Cloud-scale deployment: one container image, eight services

The platform is a set of **stateless HTTP services** behind a single gateway, plus a
worker that pulls jobs off a queue. Everything that makes it stateful is externalised:
the queue, the object store, the database and the ERP sandbox are all configured by
environment variable, so the same image scales horizontally without a code change.

```
                    ┌──────────────────────────┐
   browser ───────► │  gateway            :8080 │  UI + aggregation API + routing
                    └────────────┬─────────────┘
        ┌──────────────┬─────────┼──────────┬──────────────┐
        ▼              ▼         ▼          ▼              ▼
   extraction     matching    fraud     payments        anchor
     :8081         :8082      :8083      :8084          :8085
        └──────────────┴─────────┴──────────┴──────────────┘
                              ▼
                    ┌───────────────────┐
                    │ worker       :8086 │  consumes the queue, writes the ledger
                    └───────────────────┘
                              ▼
        redis (queue) · minio (documents) · postgres (records) · ERP sandbox
```

## Service responsibilities

| Service | Port | Responsibility | Replicas | State |
|---|---|---|---|---|
| `gateway` | 8080 | Serves the reviewer workbench, terminates the public API, routes `/svc/*` | 2–4 | stateless |
| `extraction` | 8081 | Upload/webhook intake, OpenCV pre-processing, OCR, field and table extraction | 4–16 | stateless (documents go to the object store) |
| `matching` | 8082 | Three-way match against PO and GRN, tolerance and fuzzy scoring | 2–6 | stateless (reads ERP) |
| `fraud` | 8083 | Duplicates, Benford, Isolation Forest, graph network, risk aggregation | 2–8 | model artefacts read-only |
| `payments` | 8084 | Policy gates, ISO 20022 pain.001, rail submission, reconciliation | 1–2 | writes the payment ledger |
| `anchor` | 8085 | Merkle sealing and external anchoring of the audit trail | 1 | append-only anchor log |
| `worker` | 8086 | Queue consumer: drives documents through the chain, seals blocks | 4–32 | stateless |

Scaling rule of thumb: `extraction` is CPU/IO-bound (scale on queue depth),
`fraud` is memory-bound (scale on model size), everything else is thin.

## Run it locally

```bash
# 1. everything on one machine, still as separate processes and ports
python3 scripts/run_services.py            # starts the whole fleet
python3 scripts/run_services.py --check    # health-check every service and exit
python3 scripts/run_services.py --list     # the topology, with ports and dependencies

# 2. the full stack with the infrastructure it expects
cd deploy
cp .env.example .env                       # then edit the two demo secrets
docker compose up --build
# open http://localhost:8080
```

`docker compose up` starts the eight services plus `redis`, `minio`, `postgres` and a
one-shot `seed` job. Without the infrastructure containers the services fall back to
the in-process queue and the local filesystem, which is why the single-process launch
(`python3 run.py`) needs nothing at all.

## Configuration

Every service reads the same settings object, so configuration is one contract:

| Variable | Meaning | Default |
|---|---|---|
| `AP_SERVICES_MODE` | `single` (in-process), `http` (call peers over HTTP) | `single` |
| `AP_SERVICE_HOSTNAME` | host used to reach peer services | `127.0.0.1` |
| `AP_<SERVICE>_PORT` | override a service port (`AP_EXTRACTION_PORT`, …) | see table above |
| `AP_BROKER` | `memory` or `redis` | `memory` |
| `AP_BROKER_URL` | broker connection string | — |
| `AP_OBJECT_STORE` | `local` or `s3` | `local` |
| `AP_ERP_DRIVER` | `sqlite`, `sap`, `netsuite` | `sqlite` |
| `AP_ANCHOR_URL` | `file://…`, `http(s)://…` notary, or empty for no anchoring | — |
| `AP_PAYMENTS_MODE` | `sandbox`, `file`, `ledger`, `live` | `sandbox` |
| `AP_LLM_PROVIDER` | `openai`, `anthropic`, `ollama` — enables generative contract parsing | — |
| `AP_STP_CONFIDENCE` | straight-through confidence gate | `0.95` |

## Production checklist

1. Set `AP_SERVICES_MODE=http` and give the gateway a load balancer; health probes are
   `GET /health` and `GET /ready` on every service.
2. Move the queue to Redis (`AP_BROKER=redis`), documents to S3/MinIO
   (`AP_OBJECT_STORE=s3`), and the records database to Postgres.
3. Set `AP_ANCHOR_URL` to your notary or WORM endpoint so block roots leave the
   database — the anchor service retries and records every receipt.
4. Only then set `AP_PAYMENTS_MODE=live` with real credentials, and leave the policy
   gates (per-payment cap, per-run cap, bank-change cooling-off, verified-account
   requirement) at values your controller signs off on.
5. Put a secret manager in front of the environment variables. `deploy/.env.example`
   contains only demo placeholders.
