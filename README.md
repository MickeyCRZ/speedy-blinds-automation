# Dealer Order Automation — Speedy / Inspira / Houston Blinds

A comprehensive order automation suite for window blinds distribution across **Speedy Blinds**, **Inspira Blinds**, and **Houston Blinds**.

Provides both a **modern web application dashboard** and an interactive **CLI tool** for extracting completed dealer orders from WhatsApp, looking up real ERP prices, writing records to dealer Google Sheets, calculating installer pay & dealer charges (MIR), and auditing pricing metrics.

---

## 🚀 Quick Start (Web Application)

Start the web application with a single command:

```bash
# Recommended
python3 app.py

# Or on a custom port (default is 5001)
python3 app.py --port 5001

# Or from main.py
python3 speedy_blinds/main.py --web
```

Open your browser to: **[http://127.0.0.1:5001](http://127.0.0.1:5001)**

---

## 💻 CLI Usage

You can also run the terminal-based interactive CLI:

```bash
python3 speedy_blinds/main.py
```

### CLI Arguments
| Flag | Description |
| :--- | :--- |
| `--web` | Launch the web application interface |
| `--port <PORT>` | Custom port for the web application (default: 5001) |
| `--file <PATH>`, `-f` | Load raw WhatsApp orders text directly from a file |
| `--no-confirm` | Automatically approve and push all orders without prompts |
| `--dry-run` | Parse orders and query ERP prices, but skip Google Sheet writes |

---

## 🌟 Web Application Features

The web interface is divided into dedicated modules:

### 1. 📱 WhatsApp Orders Processing
- **AI Extraction**: Uses Groq AI (`openai/gpt-oss-120b`) to parse dates, order numbers (`ORD-XXXX`), dealer names, customers, quantities, and reworks (`RW###`).
- **Parallel ERP Enrichment**: Queries Houston/Speedy ERP across configured tenants to fetch total cost.
- **Interactive Review Table**: Filter by All, Regular Orders, Reworks, or Missing Price. Inline editing for all fields prior to pushing.
- **One-Click Google Sheets Sync**: Writes records to the appropriate dealer's Google Sheet worksheet (`2026!B:G`).
- **ERP Status Update**: Mark successfully written orders as `completed` in ERP with one click.

### 2. 🧮 MIR Calculator (Mobile Installer Report)
- **Multi-Installer Batches**: Create separate batches for installers (e.g. John, Mike, Alen) and paste their order lists.
- **Pre-fetch ERP Dealers**: Auto-resolves dealers and customers before committing calculations.
- **Dual Ledger**:
  - **Installer Payable**: Install ($3), Uninstall ($3), Rework ($3), Tiered Big Ladder (+$20 1st, +$5 each additional).
  - **Dealer Receivable**: Install ($4), Uninstall + Install ($8), Uninstall ($4), Tiered Big Ladder (+$25 1st, +$10 each additional).
- **Email Report**: Dispatches a single combined HTML/plain email report to configured recipients.

### 3. ✅ Bulk Mark Orders Completed
- Paste any messy string of order numbers (`ON 121, 122, ORD-0123`).
- Normalizes order numbers and checks current ERP status in parallel.
- Categorizes orders into **Eligible to Complete**, **Already Completed**, and **Not Found**.
- Batch-patches eligible orders to `completed` in ERP with an append-only audit log in `erp_updates.log`.

### 4. 🔍 Price Checker (Cost vs Quote Analysis)
- Analyzes orders against ERP across all configured tenants (strictly read-only).
- Computes Total Cost, Quoted Price, Square Footage, Rate/SqFt, and Markup Margin %.
- Download findings via the **Export CSV** button.

### 5. 📊 Price Reviewer (Schedule A Recalculator)
- Fetches order lines (fabric, color, dimensions, product type) from ERP.
- Recalculates expected prices against Schedule A rates and displays line-by-line variance (+/-).

### 6. 🛠️ Reworks Processor
- Enter rework request IDs or ranges (`1-5` or `12, 14, 18`).
- Fetches linked orders from ERP and computes fabric reworks (70% of line price) vs basic reworks ($15 flat).
- Instant CSV export and email dispatch.

### 7. 📜 Audit Logs & Diagnostics
- Live viewer for `erp_updates.log`.
- System diagnostics and token expiration monitors.

### 8. 🔑 Credentials & .env Management
- **Groq AI**: View/copy `GROQ_API_KEY`, view active model, and run latency tests.
- **Google Sheets API**: View `credentials.json` status, service account email, and run connectivity tests.
- **ERP Authentication**: Per-company login credentials, cached Bearer tokens, token expiration status, and on-demand token refresh.
- **Gmail Notifications**: Inspect SMTP sender/recipient and send test emails.
- **Live .env Editor**: Edit and save environment variables directly from the browser with automatic runtime reloading.

### 9. 🏪 Dealer Names, Aliases & Google Sheet URLs
- **Add & Edit Dealers Across All Tenants**: Seamlessly configure dealer names, Google Spreadsheet URLs (or bare IDs), and fuzzy-matching aliases for **Speedy Blinds**, **Inspira Blinds**, and **Houston Blinds**.
- **Auto-Extraction of Sheet IDs**: Paste full Google Sheets browser URLs (e.g. `https://docs.google.com/spreadsheets/d/.../edit`); the ID is extracted automatically.
- **Live Permission Verification**: Verify that the Google Service Account has read/write access to the sheet with one click.
- **Persistent Storage**: Saved in `dealers.json` and synced immediately to both Web and CLI runtime configurations.

### 10. 💳 Payments & Charges Management (Google Sheets & ERP Ledger)
- **Unified Payments & Charges Processing**: Dedicated tabs to post dealer payments (`/admin/dealers/{id}/record-payment`) or add dealer charges (`/admin/dealers/{id}/record-charge`) directly to ERP, push to Google Sheets, or both simultaneously.
- **Single & Bulk Entry**: Add individual payments/charges with instant ERP order lookup or paste bulk freeform text parsed by Groq AI.
- **Auto-Populate Dealers via ERP**: Checks the project's ERP endpoint to automatically resolve and populate the canonical dealer name for any order.
- **Support for Deductions & Refunds**: Dedicated `[-] Sheet Minus` toggle button to apply negative amounts to Google Sheet cells (e.g. `-$150.00`). The ERP ledger always strictly receives sanitized positive amounts (`round(abs(amount), 2)`).
- **Missing Order Numbers & General Account Deposits**:
  - Unlinked payments (or blank order numbers) automatically record as `LUMP-SUM` deposits.
  - Appended to the first truly empty row scanning columns `A:M`, populated with Month, Date, Order #, Amount, and Comments, highlighted in **soft amber** (`#FFF3CD`).
  - **Guaranteed WhatsApp Order Protection**: WhatsApp order processors scan `A:M` and treat payment/lump-sum rows as occupied, always appending below them (`last_row + 1`) so they are never overwritten.
  - In ERP, posted with `order_number=null` to record general ledger credits.
- **Multi-Order Split Assistant**:
  - Automatically activates when multiple order numbers are entered (e.g. `0101, 0102, 0103`).
  - Pre-fetches order prices from ERP, balances allocations across items, and queues them as individual rows.
- **Independent Alternate Columns for EFT vs Cash**:
  - If Primary EFT cell is filled, payment auto-diverts to **Alternate EFT Column** (`eft_alt_col`, default `J`).
  - If Primary Cash cell is filled, payment auto-diverts to **Alternate Cash Column** (`cash_alt_col`, default `L`).
- **Pending Queue & Live Pre-Check**: Review queued payments, check whether the order row exists in the dealer's sheet, and detect if cells already have values.
- **Configurable Old vs New Sheet Categorization**:
  - **Drag-and-Drop Board**: Visual 2-column Kanban board to drag dealers between **Old Sheet Format** and **New Sheet Format**.
  - **Configurable Column Letters**: Edit Order Search Column, Primary EFT Column, Alternate EFT Column, Primary Cash Column, Alternate Cash Column, and Comments Column directly from the UI.
- **Google Sheets Push**:
  - Automatically writes payment amounts to primary or type-specific alternate columns.
  - Appends `<Date>-<Sender>` comments into the configured comments column (preserving prior notes).
  - If a specific order is not found in the sheet, appends a new row at the bottom with the order number, amount, and comments, highlighted in **light red** (`#FFD4D4`).

---

## 📁 Project Structure

```
Automate/
├── app.py                     # Flask web application server
├── dealers.json               # Configured dealer names, sheet IDs, aliases & sheet configs
├── templates/
│   └── index.html             # Responsive dashboard (Tailwind + Alpine.js)
├── static/
│   ├── css/custom.css         # Custom styling, animations, and badges
│   └── js/app.js              # Alpine.js reactive state management
├── speedy_blinds/
│   ├── web_services.py        # Non-interactive API service layer
│   ├── payments_service.py    # EFT & Cash payments processing and sheet sync
│   ├── dealers_manager.py     # Dealer persistence, aliases & sheet layout configs
│   ├── main.py                # CLI entry point (supports --web and mode 7)
│   ├── config.py              # Configuration, company definitions, dealer maps
│   ├── parser.py              # Groq-powered WhatsApp order extraction
│   ├── erp.py                 # Houston/Speedy ERP API REST client
│   ├── sheets.py              # Google Sheets API service account writer
│   ├── mir.py                 # Mobile Installer Report & dual ledger
│   ├── bulk_complete.py       # Bulk status checker & batch updater
│   ├── price_checker.py       # Cost vs quote analysis & metrics
│   ├── price_reviewer.py      # Schedule A price recalculator
│   ├── rework_processor.py    # Rework requests processor
│   ├── notifier.py            # Gmail SMTP & macOS notifications
│   ├── tests/                 # Unit & integration test suite
│   │   ├── test_payments.py   # Tests for payments, sheet layouts & drag-drop
│   │   ├── test_web_api.py    # Tests for REST API routes
│   │   └── ...
│   └── requirements.txt       # Dependencies
├── credentials.json           # Google Service Account key
├── .env                       # Environment variables & token cache
└── erp_updates.log            # Append-only ERP update audit log
```

---

## ⚙️ Requirements & Setup

Install Python dependencies:

```bash
pip install -r speedy_blinds/requirements.txt
```

Core dependencies:
- `flask>=3.0.0`
- `groq>=0.9.0`
- `google-api-python-client>=2.120.0`
- `google-auth>=2.28.0`
- `requests>=2.31.0`
- `python-dotenv>=1.0.1`
- `tabulate>=0.9.0`

---

## 🧪 Testing

Run unit and integration tests:

```bash
# Run EFT & Cash payments tests
pytest speedy_blinds/tests/test_payments.py

# Run web application tests
pytest speedy_blinds/tests/test_web_api.py
