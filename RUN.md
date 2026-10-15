# How to run this project

You need Python and the Tesseract OCR engine installed. Total setup time is about
two minutes, after which one command starts the web application in your browser.

---

## The quick version

| Your computer | Do this |
|---|---|
| **Windows** | Unzip the folder, then **double-click `start-windows.bat`** |
| **macOS / Linux** | Unzip, then in a terminal inside the folder run **`bash start-mac-linux.sh`** |

The launcher checks your Python, checks for Tesseract, installs the Python
packages if they are missing, and opens **http://localhost:8000** in your
browser. Nothing else to type. The first run takes a minute or two while the
packages download; after that it starts in a couple of seconds.

If Tesseract is missing it will tell you and give you the link — install it, then
double-click the launcher again.

**Prefer to do it by hand?** Steps 1–4 below are the manual route.

---

## Step 1 — Get the files

Download `invoice-fraud-ai.zip` and unzip it. You get a folder called
`invoice-fraud-ai`. Open a terminal **inside that folder**:

| Your system | How to open a terminal there |
|---|---|
| Windows | Open the folder in File Explorer, type `cmd` in the address bar, press Enter |
| macOS | Right-click the folder → Services → New Terminal at Folder |
| Linux | `cd /path/to/invoice-fraud-ai` |

---

## Step 2 — Install Python and Tesseract

**Windows**

1. Install Python from [python.org](https://www.python.org/downloads/) — tick
   *"Add Python to PATH"* during setup.
2. Install Tesseract from the
   [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki). Use the
   default install location.

**macOS**

```bash
brew install python tesseract
```

**Ubuntu / Debian**

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip tesseract-ocr
```

---

## Step 3 — Install the Python packages

```bash
# Windows
py -m pip install -r requirements.txt

# macOS / Linux
python3 -m pip install -r requirements.txt
```

---

## Step 4 — Start the application

```bash
# Windows
py run.py

# macOS / Linux
python3 run.py
```

That is the whole thing. `run.py` checks your setup — Python version, Tesseract,
packages, invoice corpus — prints exactly what is missing if anything is, then
starts the server and opens **http://localhost:8000** in your browser.

```
  Automated Invoice Matching & Fraud Detection
  Linux 6.1 | /usr/bin/python3

  Python 3.13.16                       OK
  Tesseract OCR engine 5.5.0           OK  /usr/bin/tesseract
  Python packages (10 required)        OK
  Invoice corpus (21 documents)        OK

  Environment ready.

  ==========================================================================
  Starting the AP workbench
  --------------------------------------------------------------------------
    Open in your browser:  http://localhost:8000
    API documentation:     http://localhost:8000/docs
    Stop the server:       Ctrl+C
  ==========================================================================
```

If port 8000 is busy, `run.py` automatically picks the next free port and tells you.

---

## Step 5 — Use it

The dashboard opens with the bundled corpus already processed (21 invoices,
marked *critical / high severity* findings and a populated review queue). To see
the pipeline run live:

1. Click **Reset data**, then **Process all 21 invoices** — a background job runs
   OCR, matching and all four fraud engines over the corpus in about 80 seconds,
   with a live progress indicator in the header.
2. Open the **Review queue** and click a row — you get the original scan with the
   exact pixels each value was read from highlighted, the evidence for every
   finding, and the PO and goods receipts it was matched against.
3. Edit a field in *Correct the data* and press **Apply corrections & re-assess** —
   the invoice is re-matched and re-scored instantly (no re-scanning) and you see
   the decision change. That pair is stored as training data.
4. Open **Fraud analytics** for the Benford chart, duplicate clusters and
   behavioural anomalies; **Collusion network** for the vendor/employee/bank graph;
   **Ingestion** to upload your own document or simulate a vendor email;
   **Audit & feedback** for the hash-chained ledger and ERP state.
5. Open **Payments** and press **Run payments** — the six invoices the pipeline
   approved are released over the sandbox rail, the ledger fills in, and clicking
   **Advice** shows the remittance the supplier receives. Press the button a second
   time: nothing is paid twice (that is the idempotency guard).
6. Open **Contracts** — ten master agreements with their rate cards, terms and
   discounts, plus the invoices that were billed above the agreed rate.
7. Open **Audit blocks**, press **Seal pending records**, then paste any invoice id
   (from *All invoices*) into the proof box and press **Verify** — the Merkle proof
   is rebuilt in the browser and checked against the sealed root.

---

## Other commands

| Command | What it does |
|---|---|
| `python3 run.py` | environment check, then the web app (start here) |
| `python3 run.py --demo` | command-line demo: decision table for all 21 invoices |
| `python3 run.py --demo --limit 5` | the same, five documents only |
| `python3 run.py --selftest` | run the test suite (exits non-zero on failure) |
| `python3 run.py --check` | verify the environment and exit |
| `python3 run.py --install` | install missing Python packages, then start |
| `python3 run.py --port 9000` | use a different port |
| `python3 run.py --no-browser` | start without opening a browser |
| `python3 scripts/benchmark_extraction.py` | score field-extraction accuracy |
| `python3 scripts/make_report.py` | rebuild `docs/REPORT.docx` from the latest run |
| `python3 scripts/build_dataset.py` | regenerate the invoice corpus and ERP seed |
| `python3 scripts/pay_invoices.py` | headless payment run — dry run by default |
| `python3 scripts/pay_invoices.py --execute` | actually release the approved payments |
| `python3 scripts/pay_invoices.py --execute --pain001 out/pain001.xml` | also write the ISO 20022 credit-transfer file |
| `python3 scripts/run_services.py --list` | print the microservice topology, then exit |
| `python3 scripts/run_services.py` | start the whole service fleet (gateway + 7 services) |
| `python3 scripts/verify_audit_chain.py` | verify the audit trail offline (exit 0 = intact, 1 = tampered) |
| `cd deploy && docker compose up --build` | the full stack with Redis, MinIO and the database |

---

## If something goes wrong

| Message | Fix |
|---|---|
| **`start-windows.bat` flashes and closes** | Right-click it → *Edit* won't help; instead open the folder, type `cmd` in the address bar, press Enter, then type `start-windows.bat` — the error stays on screen. |
| **"Windows protected your PC"** when double-clicking the `.bat` | Click *More info* → *Run anyway*. The file is plain text you can read in Notepad — it only installs packages and starts Python. |
| **`Permission denied` running the `.sh`** | Run it as `bash start-mac-linux.sh` instead of `./start-mac-linux.sh`. |
| `tesseract is not installed or it's not in your PATH` | Tesseract is missing. Install it (Step 2). On Windows it is usually at `C:\Program Files\Tesseract-OCR\tesseract.exe` — `run.py` finds it there automatically. |
| `No module named 'cv2'` / `'fastapi'` / `'uvicorn'` | Run `python3 -m pip install -r requirements.txt`, or `python3 run.py --install`. |
| `error: externally-managed-environment` (recent Linux) | Use a virtual environment: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. |
| `No module named 'ap_intelligence'` | Start the app with `run.py` (the scripts add the source path themselves; only ad-hoc imports need `PYTHONPATH=src`). |
| Port already in use | `run.py` picks the next free port automatically, or pass `--port 9000`. |
| The browser does not open | Open the URL printed in the terminal manually; use `--no-browser` to suppress the attempt. |
| `py: command not found` on Windows | Python was not added to PATH. Re-run the installer and tick *"Add Python to PATH"*, or use `python run.py`. |
| Dashboard looks empty | The shipped database was removed, or you pressed Reset. Click **Process all 21 invoices** — it repopulates in about 80 seconds. |
| Processing feels slow | It is about 4 seconds per invoice, almost all of it OCR. Use **Process 5 invoices** for a quick look. |
| **Payments** view says nothing to pay | Only `AUTO_APPROVED` invoices (or ones a clerk approved) are payable, and each invoice is paid once. Run the corpus first, then press *Run payments*. |
| A payment is withheld | That is the policy working: the reason is printed next to it (unverified bank account, bank details changed inside the cooling-off window, cap exceeded, low confidence). |
| You want to preview a payment run without paying | `python3 scripts/pay_invoices.py` is a dry run: it applies every gate and builds the messages, then stops. Add `--execute` to release. |
| **Audit blocks** shows "0 awaiting" but you just processed invoices | Press **Seal pending records** — sealing is an explicit, auditable act, not a side effect of processing. |
| A rail other than the sandbox is needed | `AP_PAYMENTS_MODE=file` writes pain.001 files for a banking portal; `live` requires `AP_PAYMENT_API_URL` and `AP_PAYMENT_API_KEY`. |

---

## What it needs from you

Nothing, to run everything in this package. It seeds its own ERP sandbox in
`data/erp_seed/erp.sqlite3`, builds its own fraud baselines from 180 historical
invoices, reads documents straight from `data/samples/invoice_images/`, and settles
payments on a sandbox rail. It makes no network calls and touches no real system.

Three things are optional and off by default, because they need something from you:

| Optional | What you provide | What it unlocks |
|---|---|---|
| A live payment rail | `AP_PAYMENT_API_URL`, `AP_PAYMENT_API_KEY`, `AP_PAYMENTS_MODE=live` | real money movement — your bank or provider |
| An LLM provider | `AP_LLM_PROVIDER=openai\|anthropic\|ollama` (+ key) | generative contract parsing and clause Q&A |
| Nothing, if you just want to see the LLM path | `python3 scripts/mock_llm_server.py`, then `AP_LLM_PROVIDER=openai AP_LLM_API_KEY=x AP_LLM_BASE_URL=http://127.0.0.1:8099/v1 python3 scripts/build_contracts.py` | the provider-shaped request, JSON extraction, validation and merge, without an API key |
| An external anchor | `AP_ANCHOR_URL=file://…` or an HTTP notary | block roots published outside your database |
