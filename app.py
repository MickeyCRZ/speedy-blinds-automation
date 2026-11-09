"""
app.py — Modern Web Application for Speedy / Inspira / Houston Blinds Automation
=================================================================================
Run with:
    python3 app.py
    python3 app.py --port 5001
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

# Ensure speedy_blinds package is in python path
_ROOT_DIR = Path(__file__).parent.resolve()
_SB_DIR = _ROOT_DIR / "speedy_blinds"
if str(_SB_DIR) not in sys.path:
    sys.path.insert(0, str(_SB_DIR))

import config
import web_services

app = Flask(
    __name__,
    template_folder=str(_ROOT_DIR / "templates"),
    static_folder=str(_ROOT_DIR / "static"),
)
app.config["JSON_SORT_KEYS"] = False


# ── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Endpoint not found"}), 404
    return render_template("index.html")


@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    traceback.print_exc()
    return jsonify({
        "error": str(e),
        "type": type(e).__name__,
    }), 500


# ── Page Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── System & Company APIs ─────────────────────────────────────────────────────

@app.route("/api/status", methods=["GET"])
def api_status():
    status = web_services.get_system_status()
    return jsonify(status)


@app.route("/api/companies", methods=["GET"])
def api_companies():
    companies = web_services.get_companies()
    return jsonify({"companies": companies})


@app.route("/api/company", methods=["POST"])
def api_set_company():
    data = request.get_json(silent=True) or {}
    company_key = data.get("company", "speedy")
    result = web_services.set_active_company(company_key)
    return jsonify(result)


@app.route("/api/dealers", methods=["GET"])
def api_dealers():
    company_key = request.args.get("company")
    if company_key:
        web_services.set_active_company(company_key)

    dealers_list = sorted(list(config.DEALER_SHEETS.keys()))
    return jsonify({
        "company": config.ACTIVE_COMPANY_LABEL,
        "dealers": dealers_list,
        "aliases": config.DEALER_ALIASES,
    })


@app.route("/api/logs", methods=["GET"])
def api_logs():
    limit = int(request.args.get("limit", 150))
    logs = web_services.get_audit_logs(limit=limit)
    return jsonify({"logs": logs})


# ── Mode 1: WhatsApp Orders ───────────────────────────────────────────────────

@app.route("/api/orders/parse", methods=["POST"])
def api_orders_parse():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text", "")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please provide WhatsApp message text"}), 400

    try:
        result = web_services.parse_and_enrich_orders(raw_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/orders/push-sheets", methods=["POST"])
def api_orders_push_sheets():
    data = request.get_json(silent=True) or {}
    orders = data.get("orders", [])
    company_key = data.get("company")

    if not orders:
        return jsonify({"error": "No orders provided to push"}), 400

    try:
        result = web_services.push_orders_to_sheets(orders, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/orders/mark-completed", methods=["POST"])
def api_orders_mark_completed():
    data = request.get_json(silent=True) or {}
    orders = data.get("orders", [])
    company_key = data.get("company")

    if not orders:
        return jsonify({"error": "No orders provided to mark completed"}), 400

    try:
        result = web_services.mark_orders_completed_in_erp(orders, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 2: MIR Calculator ────────────────────────────────────────────────────

@app.route("/api/mir/parse-batch", methods=["POST"])
def api_mir_parse_batch():
    data = request.get_json(silent=True) or {}
    installer_name = data.get("installer", "Unknown")
    raw_text = data.get("text", "")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please enter order text for this installer"}), 400

    try:
        result = web_services.mir_parse_batch(installer_name, raw_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/mir/calculate", methods=["POST"])
def api_mir_calculate():
    data = request.get_json(silent=True) or {}
    batches = data.get("batches", [])
    company_key = data.get("company")

    if not batches:
        return jsonify({"error": "No installer batches provided"}), 400

    try:
        result = web_services.mir_calculate(batches, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mir/send-email", methods=["POST"])
def api_mir_send_email():
    data = request.get_json(silent=True) or {}
    batches = data.get("batches", [])
    company_key = data.get("company")

    if not batches:
        return jsonify({"error": "No installer batches provided"}), 400

    try:
        result = web_services.mir_send_email_report(batches, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 3: Bulk Complete ─────────────────────────────────────────────────────

@app.route("/api/bulk-complete/lookup", methods=["POST"])
def api_bulk_complete_lookup():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text", "")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please provide order numbers"}), 400

    try:
        result = web_services.bulk_lookup_orders(raw_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/bulk-complete/execute", methods=["POST"])
def api_bulk_complete_execute():
    data = request.get_json(silent=True) or {}
    orders = data.get("orders", [])
    company_key = data.get("company")

    if not orders:
        return jsonify({"error": "No orders selected to mark completed"}), 400

    try:
        result = web_services.bulk_execute_complete(orders, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 4: Price Checker ─────────────────────────────────────────────────────

@app.route("/api/price-checker/run", methods=["POST"])
def api_price_checker_run():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text", "")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please provide order numbers"}), 400

    try:
        result = web_services.run_price_check(raw_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 5: Price Reviewer ────────────────────────────────────────────────────

@app.route("/api/price-reviewer/run", methods=["POST"])
def api_price_reviewer_run():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text", "")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please provide order numbers to review"}), 400

    try:
        result = web_services.run_price_review(raw_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 6: Reworks Processor ─────────────────────────────────────────────────

@app.route("/api/reworks/process", methods=["POST"])
def api_reworks_process():
    data = request.get_json(silent=True) or {}
    range_text = data.get("range", "")
    company_key = data.get("company")

    if not range_text.strip():
        return jsonify({"error": "Please provide rework request ID(s)"}), 400

    try:
        result = web_services.process_reworks_range(range_text, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/reworks/send-email", methods=["POST"])
def api_reworks_send_email():
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    company_key = data.get("company")

    if not rows:
        return jsonify({"error": "No rework rows provided"}), 400

    try:
        result = web_services.send_reworks_email_report(rows, company_key=company_key)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Credentials & Environment APIs ───────────────────────────────────────────

@app.route("/api/credentials", methods=["GET"])
def api_get_credentials():
    try:
        info = web_services.get_credentials_info()
        return jsonify(info)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/credentials/save-env", methods=["POST"])
def api_save_env():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400
    try:
        res = web_services.save_env_file(content)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/credentials/test-groq", methods=["POST"])
def api_test_groq():
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key")
    try:
        res = web_services.test_groq_connection(api_key=api_key)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/credentials/test-sheets", methods=["POST"])
def api_test_sheets():
    try:
        res = web_services.test_sheets_connection()
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/credentials/test-erp", methods=["POST"])
def api_test_erp():
    data = request.get_json(silent=True) or {}
    company_key = data.get("company", "speedy")
    try:
        res = web_services.test_erp_connection(company_key)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/credentials/test-email", methods=["POST"])
def api_test_email():
    data = request.get_json(silent=True) or {}
    to_email = data.get("to")
    try:
        res = web_services.test_email_connection(to_email=to_email)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ── Dealer Management APIs ────────────────────────────────────────────────────

@app.route("/api/dealers/all", methods=["GET"])
def api_dealers_all():
    company = request.args.get("company")
    try:
        dealers = web_services.get_dealers_list(company_filter=company)
        return jsonify({"dealers": dealers})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/dealers/save", methods=["POST"])
def api_dealers_save():
    data = request.get_json(silent=True) or {}
    company_key = data.get("company", "speedy")
    dealer_name = data.get("name", "")
    sheet_url = data.get("sheet_url", "")
    aliases = data.get("aliases", "")
    original_name = data.get("original_name")
    sheet_type = data.get("sheet_type")

    if not dealer_name.strip():
        return jsonify({"error": "Dealer name cannot be empty"}), 400
    if not sheet_url.strip():
        return jsonify({"error": "Google Sheet URL or Spreadsheet ID is required"}), 400

    try:
        res = web_services.save_dealer_config(
            company_key=company_key,
            dealer_name=dealer_name,
            sheet_url_or_id=sheet_url,
            aliases=aliases,
            original_name=original_name,
            sheet_type=sheet_type,
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/dealers/delete", methods=["POST"])
def api_dealers_delete():
    data = request.get_json(silent=True) or {}
    company_key = data.get("company", "speedy")
    dealer_name = data.get("name", "")

    if not dealer_name.strip():
        return jsonify({"error": "Dealer name is required"}), 400

    try:
        res = web_services.delete_dealer_config(company_key=company_key, dealer_name=dealer_name)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/dealers/test-sheet", methods=["POST"])
def api_dealers_test_sheet():
    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheet_url", "")
    if not sheet_url.strip():
        return jsonify({"error": "Sheet URL or ID is required"}), 400
    try:
        res = web_services.test_dealer_sheet_access(sheet_url)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ── Sheet Configurations & Drag-and-Drop Categorization ───────────────────────

@app.route("/api/sheet-configs", methods=["GET", "POST"])
def api_sheet_configs():
    if request.method == "GET":
        try:
            from speedy_blinds import dealers_manager
            configs = dealers_manager.get_sheet_configs()
            return jsonify({"configs": configs})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    data = request.get_json(silent=True) or {}
    configs = data.get("configs", {})
    try:
        from speedy_blinds import dealers_manager
        saved = dealers_manager.save_sheet_configs(configs)
        return jsonify({"success": True, "configs": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/dealers/sheet-type", methods=["POST"])
def api_dealer_sheet_type():
    data = request.get_json(silent=True) or {}
    company_key = data.get("company", "speedy")
    dealer_name = data.get("name", "")
    sheet_type = data.get("sheet_type", "new")

    if not dealer_name.strip():
        return jsonify({"error": "Dealer name is required"}), 400

    try:
        from speedy_blinds import dealers_manager
        res = dealers_manager.update_dealer_sheet_type(
            company_key=company_key,
            dealer_name=dealer_name,
            sheet_type=sheet_type,
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/dealers/batch-sheet-types", methods=["POST"])
def api_dealers_batch_sheet_types():
    data = request.get_json(silent=True) or {}
    updates = data.get("updates", [])
    if not updates:
        return jsonify({"error": "No updates provided"}), 400

    try:
        from speedy_blinds import dealers_manager
        res = dealers_manager.batch_update_sheet_types(updates)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ── Mode 8: EFT & Cash Payments ───────────────────────────────────────────────

@app.route("/api/payments/lookup-order", methods=["POST"])
def api_payments_lookup_order():
    data = request.get_json(silent=True) or {}
    order_number = data.get("order_number", "")
    company_key = data.get("company")

    if not order_number.strip():
        return jsonify({"error": "Order number is required"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.lookup_order_in_erp(order_number, preferred_company=company_key)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/payments/lookup-multi-orders", methods=["POST"])
def api_payments_lookup_multi_orders():
    data = request.get_json(silent=True) or {}
    order_numbers = data.get("order_numbers", [])
    company_key = data.get("company")

    if not order_numbers:
        return jsonify({"error": "No order numbers provided"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.lookup_multiple_orders_in_erp(order_numbers, preferred_company=company_key)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500



@app.route("/api/payments/parse", methods=["POST"])
def api_payments_parse():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text", "")
    default_date = data.get("default_date")
    company_key = data.get("company")

    if not raw_text.strip():
        return jsonify({"error": "Please provide payment text to parse"}), 400

    try:
        from speedy_blinds import payments_service
        # Step 1: Groq AI parse (or regex fallback)
        parsed = payments_service.parse_bulk_payments_with_groq(raw_text, default_date=default_date)
        # Step 2: Enrich with ERP dealers
        enriched = payments_service.enrich_payments_with_erp(parsed, preferred_company=company_key)
        return jsonify({"payments": enriched, "count": len(enriched)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/payments/check-sheet", methods=["POST"])
def api_payments_check_sheet():
    data = request.get_json(silent=True) or {}
    dealer = data.get("dealer", "")
    order_number = data.get("order_number", "")
    payment_type = data.get("type", "EFT")
    company_key = data.get("company")

    if not dealer.strip():
        return jsonify({"error": "Dealer name is required"}), 400
    if not order_number.strip():
        return jsonify({"error": "Order number is required"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.check_payment_in_sheet(
            dealer=dealer,
            order_number=order_number,
            payment_type=payment_type,
            company_key=company_key,
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/payments/push", methods=["POST"])
def api_payments_push():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Payment payload is required"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.push_payment_to_sheet(data)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/payments/push-batch", methods=["POST"])
def api_payments_push_batch():
    data = request.get_json(silent=True) or {}
    payments = data.get("payments", [])
    if not payments:
        return jsonify({"error": "No payments selected to push"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.push_payments_batch(payments)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Mode 9: ERP Financial Ledger (Payments & Charges) ─────────────────────────

@app.route("/api/ledger/accounts-summary", methods=["GET"])
def api_ledger_accounts_summary():
    company = request.args.get("company")
    try:
        from speedy_blinds import payments_service
        data = payments_service.get_tenant_accounts_summary(company_key=company)
        return jsonify({"success": True, "data": data})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/ledger/dealers/<int:dealer_id>/balance-summary", methods=["GET"])
def api_ledger_dealer_balance_summary(dealer_id: int):
    company = request.args.get("company")
    try:
        from speedy_blinds import payments_service
        data = payments_service.get_dealer_erp_balance_summary(dealer_id=dealer_id, company_key=company)
        return jsonify({"success": True, "data": data})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/ledger/record-charge", methods=["POST"])
def api_ledger_record_charge():
    data = request.get_json(silent=True) or {}
    dealer_id = data.get("dealer_id")
    amount = data.get("amount")
    order_number = data.get("order_number")
    description = data.get("description")
    company = data.get("company")

    if not dealer_id:
        return jsonify({"error": "Dealer ID is required to record charge in ERP"}), 400
    if not amount:
        return jsonify({"error": "Charge amount is required"}), 400

    try:
        import config as _cfg
        from speedy_blinds import erp
        if company and company in _cfg.COMPANY:
            _cfg.set_company(company)
        res = erp.record_dealer_charge(
            dealer_id=int(dealer_id),
            amount=abs(float(amount)),
            order_number=order_number,
            description=description,
        )
        return jsonify({"success": True, "data": res})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ledger/record-payment", methods=["POST"])
def api_ledger_record_payment():
    data = request.get_json(silent=True) or {}
    dealer_id = data.get("dealer_id")
    amount = data.get("amount")
    order_number = data.get("order_number")
    payment_method = data.get("payment_method", "EFT")
    description = data.get("description")
    company = data.get("company")

    if not dealer_id:
        return jsonify({"error": "Dealer ID is required to record payment in ERP"}), 400
    if not amount:
        return jsonify({"error": "Payment amount is required"}), 400

    try:
        import config as _cfg
        from speedy_blinds import erp
        if company and company in _cfg.COMPANY:
            _cfg.set_company(company)
        res = erp.record_dealer_payment(
            dealer_id=int(dealer_id),
            amount=abs(float(amount)),
            order_number=order_number,
            payment_method=payment_method,
            description=description,
        )
        return jsonify({"success": True, "data": res})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ledger/execute-transaction", methods=["POST"])
def api_ledger_execute_transaction():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Transaction item payload is required"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.execute_transaction(data)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ledger/execute-batch", methods=["POST"])
def api_ledger_execute_batch():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items provided for batch execution"}), 400

    try:
        from speedy_blinds import payments_service
        res = payments_service.execute_transactions_batch(items)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500



# ── CLI & Web Launcher ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Speedy Blinds Web Application Server")
    parser.add_argument("--port", "-p", type=int, default=int(os.getenv("PORT", 5001)), help="Port to listen on (default 5001)")
    parser.add_argument("--host", "-H", type=str, default="127.0.0.1", help="Host interface (default 127.0.0.1)")
    parser.add_argument("--debug", "-d", action="store_true", help="Run with Flask debug mode")
    args = parser.parse_args()

    # Pre-select speedy as default company
    config.set_company("speedy")

    print("\n" + "=" * 60)
    print("  🚀 Dealer Order Automation — Web Application")
    print(f"  🏢 Active Company : {config.ACTIVE_COMPANY_LABEL}")
    print(f"  🌐 Local URL      : http://{args.host}:{args.port}")
    print("=" * 60 + "\n")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()

