# Automated Invoice Matching & Fraud Detection

An Accounts Payable automation platform: it reads supplier invoices out of scanned
images, extracts their contents with layout-aware reasoning, reconciles them against
ERP purchase orders and goods receipts, scores them with four independent fraud
engines, checks the billed prices against the **master agreement**, pays what it
approves over a real payment rail, and seals every decision into an
**externally anchored, hash-linked audit chain**.

It is built as **eight independently scalable services** behind one gateway
(`deploy/README.md`), and it all runs on one machine with no API keys — a real
22-document corpus through real OCR, a real ISO 20022 payment file, a real Merkle
proof you can verify with a standalone script.

```bash
python3 run.py          # checks the environment, then opens the workbench on localhost
```

---

## Results

Measured on the bundled corpus by the shipped code (`scripts/selftest.py`,
`scripts/verify_audit_chain.py`, the **Audit & feedback** tab):

| Metric | Result |
|---|---|
| Core field extraction accuracy (vendor, invoice no., PO no., subtotal, total) | **99.1%** |
| Line items reconcile to the printed subtotal | **95.2%** |
| Straight-through processing (6 auto-approved, zero human touch) | **27.3% of the book** |
| Decision agreement with ground-truth labels | **21 / 22 (95.5%)** |
| Value flagged for investigation / recovery | **$277,377.74** on $666,798.98 billed |
| Contract-breach exposure caught by reading the agreements | **$178,682** on live invoices |
| Automatic payment run (6 invoices, ACH, settled and reconciled) | **$100,650.18**, **$580.72** early-payment discount captured |
| Audit chain: records sealed, Merkle proofs, independent verifier | **22/22 records**, 6/6 checks **PASSED**, tamper → exit 1 |
| Test suite | **82 passed, 0 failed, 2 skipped** |
| Cost per invoice (compute) vs. $15.00 manual benchmark | **$0.0097** |
| End-to-end cycle time per invoice | **4.2 s** |

The one disagreement is deliberate and worth knowing: a clean $331 invoice whose
scan only read at 0.92 confidence is sent to a clerk because the straight-through
gate is **0.95** (`AP_STP_CONFIDENCE`). Lower the gate and the automation takes it;
the point of the metric is that the policy is explicit and adjustable rather than
baked into the code.

What the engines catch on this corpus:

| Engine | Findings |
|---|---|
| 3-way match | +18% unit-price inflation, goods billed but never received, a total inflated by $375 above subtotal + tax, off-contract spend with no PO |
| Duplicate detection | exact resubmission, re-numbered resubmission (`INV-…-A`), re-worded resubmission at 93% embedding similarity |
| Benford / digit analysis | amounts clustered just below the $5,000 approval threshold |
| Isolation Forest | a 4-invoice billing burst worth $178k against a $24k vendor norm |
| Graph network | a shell vendor sharing an **employee's payroll account and home address**; two vendors sharing one bank account |
| Contract compliance | Vertex billed $186.56 for a tank the agreement prices at $149.25; Halcyon billed an enterprise seat at 100× the contracted rate |

---

## The four capabilities beyond the core brief

### 1. Cloud-scale microservices

`services/` is a real decomposition, not a diagram: eight FastAPI services with
their own ports, health and readiness endpoints, a service registry that declares
responsibilities, dependencies and scaling hints, a worker that consumes jobs from a
broker, and a gateway that routes between them.

```
python3 scripts/run_services.py --list      # topology: name, port, duty, replicas
python3 scripts/run_services.py             # start the fleet locally
python3 scripts/run_services.py --check     # health-check every service
cd deploy && docker compose up --build      # gateway + 7 services + redis + minio
```

The application layer talks to services through one caller (`services/caller.py`):
in `single` mode it calls them in-process, in `http` mode over the network — the
same code path, so the fleet can be scaled out without touching business logic.
Proven on a live fleet: `document.process` over HTTP finished in 8.1 s with
`transport {extraction: http, matching: http, fraud: http}`.

### 2. Automatic, real-world invoice payments

Approved invoices leave the building. `src/ap_intelligence/payments/` implements the
whole cycle: policy gates → ISO 20022 **pain.001** credit-transfer message →
submission to a **rail** → reconciliation against the returning **camt.053** bank
statement → remittance advice to the supplier.

| Rail (`AP_PAYMENTS_MODE`) | What it does |
|---|---|
| `sandbox` (default) | deterministic settlement simulation plus a camt.053 bank reply, no external connectivity |
| `file` | writes a schema-shaped pain.001 file for upload to a banking portal |
| `ledger` | internal booking only |
| `live` | REST submission to a payments provider (Stripe/Modern Treasury/your bank API) — needs credentials and an explicit opt-in |

Money moves only for invoices the pipeline actually approved, and only if every gate
passes: confidence ≥ 93%, decision `AUTO_APPROVED` (or a human approval), amount caps
per payment and per run, vendor velocity limits, verified bank account, no bank-account
change inside the cooling-off window, name match, sanctions screen, duplicate window.
Anything that fails is **withheld with the rule that withheld it** — never silently
dropped. Every run is idempotent, so a cron job can call it repeatedly:

```bash
python3 scripts/pay_invoices.py                    # dry run: what would tonight release
python3 scripts/pay_invoices.py --execute          # release it
python3 scripts/pay_invoices.py --execute --pain001 out/pain001.xml
```

Measured: 6 invoices released, **$100,650.18** settled, **$580.72** of early-payment
discount captured, 6/6 reconciled against the bank statement, and a replayed run
reports `paid: 0` because the idempotency key already exists.

**Being straight about "real-world":** the *automation* is real — real policy, real
ISO 20022 messages, real reconciliation, real idempotency — but the sandbox rail
settles in-process. No code in this repository contains or needs bank credentials;
pointing it at your bank's API is a configuration change (`AP_PAYMENT_API_URL`,
`AP_PAYMENT_API_KEY`, `AP_PAYMENTS_MODE=live`) plus your risk sign-off. Nobody should
want a demo that moves real money out of the box.

### 3. Blockchain audit trail

Every decision was already hash-chained; it is now **Merkle-sealed and externally
anchored** so a third party can verify it without trusting this application.

```
ledger record ──▶ SHA-256 record hash ──▶ chained record ──▶ Merkle tree
        └──▶ block (root, prev-hash, seq range) ──▶ anchor sink (file / HTTP notary)
```

* `GET /api/ledger/blocks` — sealed blocks, roots, anchor receipts
* `POST /api/ledger/seal` — seal the pending records into a new block
* `GET /api/ledger/proof/{invoice_id}` — Merkle inclusion proof for one invoice
* `python3 scripts/verify_audit_chain.py` — an independent stdlib verifier: re-hashes
  every record, rebuilds every root, checks the anchor receipts. It exits **0** when
  intact and **1** when tampered — and the tamper case is tested, not assumed.

### 4. Generative-AI contract matching

Master service agreements are parsed into a machine-checkable rate card, and every
invoice line is priced against it. Two parsing paths share one contract model: a
**deterministic parser** that always runs, and an **LLM path** (`AP_LLM_PROVIDER` =
openai / anthropic / ollama) for the free-text clauses. Ten agreements covering 17
purchase orders and 42 rate lines ship with the corpus.

The LLM never gets the last word: its rates must be positive, inside a plausible band,
present in the source text, and consistent with the rule-based pass. A hallucinated
rate is **rejected with the reason recorded** rather than paid — the test suite feeds
the parser a lying model (`$99,000,000` for a bracket) and asserts the value never
reaches a finding. Verified against a live mock provider: agreement `agree`, token
cost $0.000432, method `llm+rule-based`.

Findings the matcher raises: `CONTRACT_RATE_EXCEEDED`, `CONTRACT_ESCALATION_EXCEEDED`,
`CONTRACT_EXPIRED`, `NO_CONTRACT`, `PROHIBITED_ITEM`.

---

## Quick start

**Windows:** unzip and double-click `start-windows.bat`.
**macOS / Linux:** unzip and run `bash start-mac-linux.sh`.
Both install the Python packages, check for Tesseract, and open the browser.

By hand it is three lines:

```bash
pip install -r requirements.txt        # Python deps
sudo apt-get install -y tesseract-ocr  # the OCR engine (macOS: brew install tesseract)

python3 run.py                         # check environment, start the app, open the browser
```

The app is served at **http://localhost:8000** (API docs at `/docs`). New to this?
`RUN.md` is the step-by-step walkthrough. Windows: use `py run.py`.

| Command | What it does |
|---|---|
| `python3 run.py` | web workbench (default) |
| `python3 run.py --demo` | command-line demo: decision table for the corpus |
| `python3 run.py --selftest` | 82-check test suite across matching, fraud, contracts, payments, blockchain, services, pipeline and API |
| `python3 scripts/pay_invoices.py` | headless payment run (add `--execute` to release) |
| `python3 scripts/run_services.py` | start the microservice fleet (`--list`, `--check`) |
| `python3 scripts/verify_audit_chain.py` | independently verify the audit trail (`--tamper-test`) |
| `python3 scripts/benchmark_extraction.py` | field-level extraction accuracy |
| `python3 scripts/make_report.py` | regenerate `docs/REPORT.docx` from the latest run |

---

## The workbench

| View | What it is for |
|---|---|
| **Overview** | six KPIs against target (straight-through rate, value flagged, cost per invoice, decision agreement, cycle time), routing breakdown, log-scale spend-versus-risk scatter, per-engine sensitivity, stage timings, highest-priority findings, recent activity, filterable invoice ledger |
| **Review queue** | only the invoices automation refused to approve — sorted by money at stake, with the scan, the evidence trail, and the PO/GRN comparison |
| **All invoices** | the whole book with filters by decision and risk band, free-text search, sortable columns and paging |
| **Fraud analytics** | findings by type and severity, Benford observed-versus-expected chart with chi-square and MAD, duplicate clusters with value exposed, Isolation Forest behavioural anomalies |
| **Collusion network** | vendors, employees, bank accounts and addresses as a graph, with conflicts of interest called out |
| **Ingestion** | per-channel counters, drag-and-drop upload, a webhook/REST channel for provider mail, and a raw-email console showing the envelope, SPF/DKIM/DMARC results, MIME sniffing, prompt-injection attempts and the resulting verdict |
| **Payments** | the rail and its mode, policy gates, the payment ledger, a live payment run, remittance advice and reconciliation |
| **Contracts** | every master agreement with its rate card, terms and discounts, generative-parsing status and what the agreements caught |
| **Audit blocks** | sealed Merkle blocks with roots and anchors, a proof checker for any invoice, and the offline verification recipe |
| **Audit & feedback** | hash-chained ledger with live chain verification, ERP state, clerk corrections feeding the retraining loop |

The dashboard is a single self-contained HTML file — no CDN, no build step, no
network access — so it renders identically offline and in a locked-down browser.
Dark and light themes, a collapsible rail, and `Ctrl/⌘-K` search across every
invoice and finding.

Two things worth trying: open a flagged invoice, go to **Document** and toggle the
overlays — the boxes show the **exact pixels each value was read from**, colour-coded
by field, line item and finding; then edit a field in **Correct** and press
*Apply corrections & re-assess* — the invoice is re-matched and re-scored in
milliseconds without re-scanning, the decision changes in front of you, and the
pair is stored as training data. The **Trace** tab then shows both the pipeline
timings and the human review trail.

---

## How it works

```
                  ┌──────────── ingress ────────────┐
   email (.eml) ──┤ SPF/DKIM/DMARC, MIME sniffing,  │
   upload  ───────┤ prompt-injection screening      │
                  └────────────────┬────────────────┘
                                   ▼
 [1] PREPROCESS   OpenCV: upscale → denoise → deskew (Hough) → adaptive threshold
 [2] OCR          Tesseract 5 (Textract / Azure adapters) → words + boxes + confidence
 [3] EXTRACT      layout-aware field extraction; LayoutLMv3 token classification when a
                  checkpoint is present, spatial heuristics otherwise
 [4] TABLE PARSE  graph-based row/column reconstruction, decimal repair, line items
 [5] 3-WAY MATCH  PO × GRN × invoice: fuzzy descriptions, price/qty/total tolerances,
                  plus an arithmetic check that catches totals tampered above subtotal+tax
 [6] FRAUD        duplicates │ Benford │ Isolation Forest │ graph network
 [7] RISK/ROUTE   noisy-OR aggregation + hard blocks → auto-approve / review / quarantine / reject
 [8] ACT          idempotent ERP posting, or queue for a clerk with the evidence attached
 [9] AUDIT        hash-chained JSONL ledger, every stage timed
```

### Design decisions worth knowing

**The matching engine is deterministic; only the fraud layer is probabilistic.**
Controllers must be able to audit exactly why an invoice was auto-approved, so
tolerances and fuzzy thresholds are explicit numbers in `config.py`. Machine-learning
risk scores can only *downgrade* a decision, never upgrade one.

**Extraction confidence is calibrated with independent checks, not self-reported
scores.** A field is trusted when an independent test agrees: the line items sum to the
subtotal, subtotal + tax + shipping equals the printed total, the PO number resolves in
the ERP, the vendor exists in the vendor master. Those checks lift clean documents from
0.74 to 0.97 confidence.

**A wrong value is worse than a missing one.** Identifier fields carry validators; if a
candidate fails, the field is reported as missing rather than guessed — a wrong tax ID
raises a false conflict-of-interest alert and burns a clerk's time.

**The API never does OCR inside a request.** Processing runs in a worker pool with
progress persisted to the store, which is the single-node form of the Celery/RabbitMQ
queue described in the architecture: `submit()` is unchanged when the broker moves
off-box.

---

## Project layout

```
run.py                       environment check + launcher (web app, demo, tests)
app/
  main.py                    FastAPI routes: invoices, ingestion, fraud, jobs, ledger
  service.py                 application service: processing, analytics, HITL re-assessment
  store.py                   SQLite persistence (invoices, jobs, corrections, ledger)
  jobs.py                    background worker pool for CPU-bound OCR
  serialization.py           domain objects ⇄ JSON, including the bbox provenance
  static/index.html          the whole UI: no CDN, no build step, works offline
src/ap_intelligence/
  config.py                  every business threshold, in one place
  models.py                  typed domain objects
  ingestion/                 OpenCV preprocessing + email ingestion
  ocr/engine.py              Tesseract / Textract / Azure behind one interface
  extraction/                spatial extractor, graph table parser, LayoutLMv3 path
  matching/                  fuzzy scoring + the deterministic three-way engine
  fraud/                     duplicates, Benford, Isolation Forest, graph, risk engine
  contracts/                 agreement parser (deterministic + LLM), rate-card matcher
  payments/                  policy gates, ISO 20022 pain.001 / camt.053, rails, engine
  audit/blockchain.py        Merkle blocks, anchor sinks, inclusion proofs
  erp/connector.py           SQLite sandbox + SAP / NetSuite adapter shapes
  pipeline/orchestrator.py   the nine-stage pipeline, trace, ledger, re-assessment
  feedback/                  corrections → active-learning training manifests
services/                    gateway, extraction, matching, fraud, payments, anchor, worker
deploy/                      docker-compose stack, Dockerfile, deployment guide
scripts/
  run_demo.py                command-line demo
  benchmark_extraction.py    field-level accuracy harness
  selftest.py                82-check test suite (exit code is a CI gate)
  build_dataset.py           renders the corpus + ERP seed
  build_contracts.py         derives the master agreements from the ERP price book
  pay_invoices.py            headless payment run for cron / Task Scheduler
  run_services.py            launches the microservice fleet
  verify_audit_chain.py      independent audit-trail verifier
  make_report.py             generates docs/REPORT.docx from live results
docs/REPORT.docx             project report
RUN.md                       step-by-step run instructions
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | environment, OCR engine, model backend, corpus size |
| GET | `/api/invoices` | list with filters (`decision`, `risk_band`, `review_status`, `q`) |
| GET | `/api/invoices/{id}` | full dossier: fields + boxes, totals, line items, match, findings, trace |
| GET | `/api/invoices/{id}/image` | the source document |
| POST | `/api/invoices/{id}/corrections` | apply clerk corrections → re-assess without OCR |
| POST | `/api/invoices/{id}/review` | approve / reject / escalate |
| POST | `/api/ingest/upload` | upload a document (portal/API channel) |
| POST | `/api/ingest/email` | raw RFC-822 message through the AP inbox path |
| POST | `/api/ingest/email/webhook` | **webhook receiver** for SendGrid, Mailgun, Microsoft Graph or a raw MIME body |
| GET | `/api/fraud/benford`, `/duplicates`, `/behaviour`, `/network` | per-engine analytics |
| POST | `/api/jobs/corpus` | queue a corpus run; `GET /api/jobs/{id}` for progress |
| GET | `/api/ledger` | audit trail + hash-chain verification |
| GET | `/api/ledger/blocks` | sealed Merkle blocks, roots and anchor receipts |
| POST | `/api/ledger/seal` | seal pending records into a new anchored block |
| GET | `/api/ledger/proof/{invoice_id}` | Merkle inclusion proof for one invoice |
| GET | `/api/payments/rail` | rail, mode and the full policy gate table |
| GET | `/api/payments` | payment history: released, withheld, discounts, reconciliation |
| POST | `/api/payments/run` | run a payment cycle (`{"dry_run": true}` to preview) |
| POST | `/api/payments/reconcile` | reconcile a camt.053 bank statement |
| POST | `/api/payments/remittance` | remittance advice for one payment |
| GET | `/api/contracts` | agreements, rate cards, terms, coverage and exposure |
| GET | `/api/platform` | topology, runtime configuration, process state |
| GET | `/api/feedback` | corrections, retraining status, uncertainty sampling queue |

Interactive documentation is served at `/docs`.

---

## Configuration

Every business threshold is a named constant in `src/ap_intelligence/config.py`:

```python
ToleranceConfig:
    absolute_amount = 0.05       # $ variance allowed on totals
    relative_amount = 0.005      # 0.5% variance allowed
    price_variance_pct = 0.02    # 2% unit-price drift tolerated

FraudConfig:
    duplicate_similarity_threshold = 0.92
    benford_min_samples = 60
    approval_threshold = 5000.0  # the limit that threshold-gaming clusters under

RoutingConfig:
    stp_confidence_min = 0.95    # below this, a human looks at it (AP_STP_CONFIDENCE)
    stp_risk_max = 0.10          # above this, a human looks at it (AP_STP_RISK_MAX)
```

The confidence gate is the single knob that trades straight-through rate against
residual risk: at the briefed **0.95** the corpus auto-clears the clean invoices
and sends everything borderline to a clerk. Raise or lower it without touching
code — `AP_STP_CONFIDENCE=0.90 python3 run.py`.

---

## Honest limitations

Listed rather than hidden — these are what a production rollout must address:

- **Vendor tax ID is the weakest extracted field.** Where OCR loses the digits, the
  pipeline reports the field as missing rather than guessing. Correct behaviour, but
  it means tax-ID verification is only partially exercised until a fine-tuned
  LayoutLMv3 checkpoint or a cloud OCR engine is in place; the integration path
  exists (`extraction/layoutlm.py`) but no GPU was available to train it here.
- **The shipped extractor is the spatial heuristic fallback**, not a trained
  transformer. Same output contract, lower accuracy on unusual layouts.
- **Multi-page invoices** with tables continuing across page breaks are not handled.
- **The graph engine rebuilds its graph per invoice** — fine for 13 vendors, wrong
  for 10,000; that belongs in a graph database with incremental updates.
- **Live payment rails need your credentials.** The `live` rail is implemented as an
  HTTP adapter and refuses to run without an endpoint and key; the sandbox rail is
  what the numbers above were produced with, so no money moved anywhere.
- **The docker stack is not exercised in CI.** The images, compose file and service
  fleet are complete and the fleet is verified locally (`run_services.py` →
  7/7 `/health` 200), but Docker was unavailable in the environment this was built
  in, so treat `docker compose up` as untested-here rather than proven.
- **SQLite, a file broker and a local object store** are the defaults. Postgres,
  Redis and S3/MinIO are wired through configuration (`deploy/docker-compose.yml`),
  which is where a production deployment starts.
- **Synthetic fraud labels.** The detectors are proven to fire on the injected
  patterns; real-world precision must be calibrated against a labelled sample of the
  client's own invoices before the thresholds are trusted.
- **Anchoring is only as strong as the sink.** The default sink is a local file. A
  notary or WORM endpoint (`AP_ANCHOR_URL`) is what makes the trail independent of
  the operator — the code path exists and is tested over an HTTP sink; the
  organisational decision about *whose* notary is yours to make.
- **The generative contract path was validated against a mock provider**, not a live
  paid model, because no API key was available here. The guard rails around it are
  tested directly (a lying model is rejected in the suite).
