// static/js/app.js

document.addEventListener("alpine:init", () => {
    Alpine.data("automateApp", () => ({
        // Navigation & Global Context
        currentTab: "orders",
        activeCompany: "speedy",
        companies: [],
        dealers: [],
        systemStatus: null,
        
        // Toast notifications
        toast: {
            show: false,
            message: "",
            type: "success", // success, error, info, warning
            timeout: null,
        },

        showToast(message, type = "success") {
            if (this.toast.timeout) clearTimeout(this.toast.timeout);
            this.toast.message = message;
            this.toast.type = type;
            this.toast.show = true;
            this.toast.timeout = setTimeout(() => {
                this.toast.show = false;
            }, 4500);
        },

        // Helper: API Fetch with JSON
        async apiCall(endpoint, method = "GET", body = null) {
            const options = {
                method,
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            };
            if (body) {
                options.body = JSON.stringify(body);
            }
            try {
                const res = await fetch(endpoint, options);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.error || `HTTP ${res.status}: ${res.statusText}`);
                }
                return data;
            } catch (err) {
                console.error(`API Error on ${endpoint}:`, err);
                throw err;
            }
        },

        // Initialisation
        async init() {
            await this.loadCompanies();
            await this.loadSystemStatus();
            await this.loadDealers();
        },

        async loadCompanies() {
            try {
                const res = await this.apiCall("/api/companies");
                this.companies = res.companies || [];
                const active = this.companies.find(c => c.is_active);
                if (active) this.activeCompany = active.key;
            } catch (err) {
                this.showToast("Failed to load companies: " + err.message, "error");
            }
        },

        async loadDealers() {
            try {
                const res = await this.apiCall(`/api/dealers?company=${this.activeCompany}`);
                this.dealers = res.dealers || [];
            } catch (err) {
                console.error("Failed to load dealers:", err);
            }
        },

        async loadSystemStatus() {
            try {
                const res = await this.apiCall("/api/status");
                this.systemStatus = res;
                if (res.active_company) {
                    this.activeCompany = res.active_company.key;
                }
            } catch (err) {
                console.error("Failed to fetch system status:", err);
            }
        },

        async switchCompany(key) {
            try {
                await this.apiCall("/api/company", "POST", { company: key });
                this.activeCompany = key;
                await this.loadCompanies();
                await this.loadSystemStatus();
                await this.loadDealers();
                this.showToast(`Active company switched to ${this.currentCompanyLabel}`, "success");
            } catch (err) {
                this.showToast("Failed to switch company: " + err.message, "error");
            }
        },

        get currentCompanyLabel() {
            const c = this.companies.find(item => item.key === this.activeCompany);
            return c ? c.label : "Speedy Blinds";
        },

        // =====================================================================
        // TAB 1: WhatsApp Orders Processing
        // =====================================================================
        ordersInputText: "",
        ordersList: [],
        ordersSummary: null,
        ordersLoading: false,
        ordersPushLoading: false,
        ordersCompleteLoading: false,
        ordersEmailLoading: false,
        ordersSelectAll: true,
        ordersFilter: "all", // all, orders, reworks, missing_price
        sendSummaryEmail: true,

        failedOrdersModal: {
            show: false,
            writtenOrders: [],
            failedOrders: [],
            totalWritten: 0,
            totalFailed: 0,
            sending: false,
        },

        get filteredOrders() {
            if (this.ordersFilter === "orders") {
                return this.ordersList.filter(o => o.type !== "rework");
            }
            if (this.ordersFilter === "reworks") {
                return this.ordersList.filter(o => o.type === "rework");
            }
            if (this.ordersFilter === "missing_price") {
                return this.ordersList.filter(o => o.price === null || o.price === undefined);
            }
            return this.ordersList;
        },

        get selectedOrders() {
            return this.ordersList.filter(o => o._selected);
        },

        toggleSelectAllOrders() {
            this.ordersSelectAll = !this.ordersSelectAll;
            this.ordersList.forEach(o => {
                o._selected = this.ordersSelectAll;
            });
        },

        loadSampleOrders() {
            this.ordersInputText = `9 June

0246 vtt - benni = 17
0201 Phil - Sajid 46 = 10
0226 Phil - binu add on = 1 ( 1 motor, 1 remote, 1 charger)
0198 Alen - Shakeel = 4
0214 Vtt - shamesh = 14
0224 Han - sarbha = 8
0232 Han - Benny = 5
RW136 Mike -$176.4
RW137 vtt -$30

Total blinds = 59`;
            this.showToast("Sample WhatsApp orders loaded", "info");
        },

        handleOrdersFileUpload(event) {
            const file = event.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = (e) => {
                this.ordersInputText = e.target.result;
                this.showToast(`Loaded file: ${file.name}`, "info");
            };
            reader.readAsText(file);
        },

        async parseOrders() {
            if (!this.ordersInputText.trim()) {
                this.showToast("Please enter or paste WhatsApp text", "warning");
                return;
            }
            this.ordersLoading = true;
            try {
                const res = await this.apiCall("/api/orders/parse", "POST", {
                    text: this.ordersInputText,
                    company: this.activeCompany,
                });
                this.ordersList = (res.orders || []).map((o, idx) => ({
                    ...o,
                    _id: idx + 1,
                    _selected: true,
                    _status: "Pending Push",
                    _range: "",
                }));
                this.ordersSummary = res.summary;
                this.ordersSelectAll = true;
                this.showToast(`Extracted ${res.orders.length} order(s) successfully!`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.ordersLoading = false;
            }
        },

        async pushSelectedOrders() {
            const selected = this.selectedOrders;
            if (selected.length === 0) {
                this.showToast("Please select at least one order to push", "warning");
                return;
            }
            this.ordersPushLoading = true;
            try {
                const res = await this.apiCall("/api/orders/push-sheets", "POST", {
                    orders: selected,
                    company: this.activeCompany,
                    send_email: Boolean(this.sendSummaryEmail),
                });

                // Update row statuses
                const writtenMap = new Map();
                (res.written || []).forEach(w => writtenMap.set(w.order_number, w.range));

                const errorMap = new Map();
                (res.errors || []).forEach(e => errorMap.set(e.order_number, e.error));

                const unmatchedSet = new Set((res.unmatched || []).map(u => u.order_number));

                this.ordersList.forEach(o => {
                    if (writtenMap.has(o.order_number)) {
                        o._status = "Written to Sheet";
                        o._range = writtenMap.get(o.order_number);
                        o._isSuccess = true;
                    } else if (unmatchedSet.has(o.order_number)) {
                        o._status = "Unknown Dealer";
                        o._hasError = true;
                    } else if (errorMap.has(o.order_number)) {
                        o._status = errorMap.get(o.order_number);
                        o._hasError = true;
                    }
                });

                let msg = `Successfully pushed ${res.total_written} order(s) to Google Sheets.`;
                if (res.total_unmatched > 0) msg += ` (${res.total_unmatched} unknown dealer)`;
                if (res.total_errors > 0) msg += ` (${res.total_errors} errors)`;
                if (res.email_sent) {
                    msg += ` 📧 Summary email sent to ${res.email_to || 'inbox'}`;
                } else if (res.email_error) {
                    msg += ` ⚠️ (Email alert failed: ${res.email_error})`;
                }
                this.showToast(msg, res.total_errors > 0 ? "warning" : "success");

                // If any order failed, prompt user whether to include failed order(s) in summary email
                if (res.prompt_summary_email && (res.total_errors > 0 || res.total_unmatched > 0)) {
                    const writtenSet = new Set((res.written || []).map(w => w.order_number));
                    const errorDetails = new Map();
                    (res.errors || []).forEach(e => errorDetails.set(e.order_number, e.error));
                    (res.unmatched || []).forEach(u => errorDetails.set(u.order_number, u.error));

                    this.failedOrdersModal.writtenOrders = selected.filter(o => writtenSet.has(o.order_number));
                    this.failedOrdersModal.failedOrders = selected.filter(o => errorDetails.has(o.order_number)).map(o => ({
                        ...o,
                        _sheet_error: errorDetails.get(o.order_number) || "Failed to push to sheet",
                    }));
                    this.failedOrdersModal.totalWritten = res.total_written;
                    this.failedOrdersModal.totalFailed = res.total_errors + res.total_unmatched;
                    this.failedOrdersModal.show = true;
                }
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.ordersPushLoading = false;
            }
        },

        async confirmSummaryEmail(includeFailed) {
            this.failedOrdersModal.sending = true;
            try {
                const res = await this.apiCall("/api/orders/send-summary-email", "POST", {
                    orders: this.failedOrdersModal.writtenOrders,
                    failed_orders: this.failedOrdersModal.failedOrders,
                    include_failed: includeFailed,
                    company: this.activeCompany,
                });
                this.failedOrdersModal.show = false;
                if (res.email_sent) {
                    const note = includeFailed ? "including unpushed order in revenue totals" : "pushed orders only";
                    this.showToast(`📧 Summary email sent to ${res.email_to} (${note})`, "success");
                } else if (res.email_error) {
                    this.showToast(`Email warning: ${res.email_error}`, "warning");
                }
            } catch (err) {
                this.showToast("Failed to send summary email: " + err.message, "error");
            } finally {
                this.failedOrdersModal.sending = false;
            }
        },

        closeFailedOrdersModal() {
            this.failedOrdersModal.show = false;
        },

        async sendManualSummaryEmail() {
            const targets = this.selectedOrders.length > 0 ? this.selectedOrders : this.ordersList;
            if (targets.length === 0) {
                this.showToast("No orders available to summarize", "warning");
                return;
            }
            if (!confirm(`Send summary email for ${targets.length} order(s) without pushing to sheets?`)) {
                return;
            }
            this.ordersEmailLoading = true;
            try {
                const res = await this.apiCall("/api/orders/send-summary-email", "POST", {
                    orders: targets,
                    company: this.activeCompany,
                    include_failed: false,
                });
                if (res.email_sent) {
                    this.showToast(`📧 Summary email successfully sent to ${res.email_to}`, "success");
                } else {
                    this.showToast(`Email status: ${res.email_error || 'Not sent'}`, "warning");
                }
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.ordersEmailLoading = false;
            }
        },

        async markSelectedOrdersCompleted() {
            const candidates = this.selectedOrders.filter(
                o => o.type !== "rework" && o.erp_id && String(o.erp_status || "").toLowerCase() !== "completed"
            );
            if (candidates.length === 0) {
                this.showToast("No uncompleted ERP orders selected", "warning");
                return;
            }

            if (!confirm(`Mark ${candidates.length} order(s) as completed in ${this.currentCompanyLabel} ERP?`)) {
                return;
            }

            this.ordersCompleteLoading = true;
            try {
                const res = await this.apiCall("/api/orders/mark-completed", "POST", {
                    orders: candidates,
                    company: this.activeCompany,
                });
                
                // Update local status
                const doneMap = new Map();
                (res.results || []).forEach(r => {
                    if (r.success) doneMap.set(r.order_number, true);
                });

                this.ordersList.forEach(o => {
                    if (doneMap.has(o.order_number)) {
                        o.erp_status = "completed";
                    }
                });

                this.showToast(`Updated ERP: ${res.completed_count} marked completed, ${res.failed_count} failed.`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.ordersCompleteLoading = false;
            }
        },

        deleteOrderRow(idx) {
            this.ordersList.splice(idx, 1);
        },

        // =====================================================================
        // TAB 2: MIR Calculator
        // =====================================================================
        mirBatches: [
            { id: 1, installer: "John", text: "", orders: [], isParsed: false, loading: false }
        ],
        mirLedger: null,
        mirCalcLoading: false,
        mirEmailLoading: false,

        addMirBatch() {
            const nextId = this.mirBatches.length + 1;
            this.mirBatches.push({
                id: nextId,
                installer: "",
                text: "",
                orders: [],
                isParsed: false,
                loading: false,
            });
        },

        removeMirBatch(idx) {
            this.mirBatches.splice(idx, 1);
            if (this.mirBatches.length === 0) {
                this.addMirBatch();
            }
        },

        async parseMirBatch(batch) {
            if (!batch.installer.trim()) {
                this.showToast("Please enter installer name", "warning");
                return;
            }
            if (!batch.text.trim()) {
                this.showToast("Please paste order text for this installer", "warning");
                return;
            }
            batch.loading = true;
            try {
                const res = await this.apiCall("/api/mir/parse-batch", "POST", {
                    installer: batch.installer,
                    text: batch.text,
                    company: this.activeCompany,
                });
                batch.orders = res.orders || [];
                batch.isParsed = true;
                this.showToast(`Parsed ${batch.orders.length} order(s) for ${batch.installer}`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                batch.loading = false;
            }
        },

        async calculateMirLedger() {
            const validBatches = this.mirBatches.filter(b => b.orders.length > 0);
            if (validBatches.length === 0) {
                this.showToast("Please parse at least one installer batch with orders first", "warning");
                return;
            }
            this.mirCalcLoading = true;
            try {
                const payload = validBatches.map(b => ({
                    installer: b.installer,
                    orders: b.orders,
                }));
                const res = await this.apiCall("/api/mir/calculate", "POST", {
                    batches: payload,
                    company: this.activeCompany,
                });
                this.mirLedger = res;
                this.showToast("MIR Dual Ledger calculated successfully!", "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.mirCalcLoading = false;
            }
        },

        async sendMirEmail() {
            const validBatches = this.mirBatches.filter(b => b.orders.length > 0);
            if (validBatches.length === 0) {
                this.showToast("No batches to send report for", "warning");
                return;
            }
            if (!confirm(`Send MIR Report email to ${this.systemStatus?.integrations?.gmail_notifications?.to || 'configured recipient'}?`)) {
                return;
            }
            this.mirEmailLoading = true;
            try {
                const payload = validBatches.map(b => ({
                    installer: b.installer,
                    orders: b.orders,
                }));
                const res = await this.apiCall("/api/mir/send-email", "POST", {
                    batches: payload,
                    company: this.activeCompany,
                });
                this.showToast(`MIR Report email sent to ${res.recipient}!`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.mirEmailLoading = false;
            }
        },

        // =====================================================================
        // TAB 3: Bulk Mark Orders Completed
        // =====================================================================
        bulkInputText: "",
        bulkLoading: false,
        bulkExecuteLoading: false,
        bulkResults: null,
        bulkSelectedMap: {},

        get bulkEligibleSelected() {
            if (!this.bulkResults?.eligible) return [];
            return this.bulkResults.eligible.filter(item => this.bulkSelectedMap[item.order_number]);
        },

        toggleAllBulkEligible(checked) {
            if (!this.bulkResults?.eligible) return;
            this.bulkResults.eligible.forEach(item => {
                this.bulkSelectedMap[item.order_number] = checked;
            });
        },

        async lookupBulkOrders() {
            if (!this.bulkInputText.trim()) {
                this.showToast("Please enter order numbers to look up", "warning");
                return;
            }
            this.bulkLoading = true;
            try {
                const res = await this.apiCall("/api/bulk-complete/lookup", "POST", {
                    text: this.bulkInputText,
                    company: this.activeCompany,
                });
                this.bulkResults = res;
                this.bulkSelectedMap = {};
                (res.eligible || []).forEach(item => {
                    this.bulkSelectedMap[item.order_number] = true;
                });
                this.showToast(`Scanned ${res.total_scanned} orders (${res.eligible.length} eligible to complete)`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.bulkLoading = false;
            }
        },

        async executeBulkComplete() {
            const selected = this.bulkEligibleSelected;
            if (selected.length === 0) {
                this.showToast("No eligible orders selected to mark completed", "warning");
                return;
            }
            if (!confirm(`Mark ${selected.length} orders as completed in ${this.currentCompanyLabel} ERP?`)) {
                return;
            }
            this.bulkExecuteLoading = true;
            try {
                const res = await this.apiCall("/api/bulk-complete/execute", "POST", {
                    orders: selected,
                    company: this.activeCompany,
                });
                this.showToast(`Completed ${res.completed_count} orders (${res.failed_count} failed)`, "success");
                // Re-run lookup to refresh status
                await this.lookupBulkOrders();
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.bulkExecuteLoading = false;
            }
        },

        // =====================================================================
        // TAB 4: Price Checker (Cost vs Quote)
        // =====================================================================
        priceCheckInput: "",
        priceCheckLoading: false,
        priceCheckResults: null,

        async runPriceCheck() {
            if (!this.priceCheckInput.trim()) {
                this.showToast("Please enter order numbers to check", "warning");
                return;
            }
            this.priceCheckLoading = true;
            try {
                const res = await this.apiCall("/api/price-checker/run", "POST", {
                    text: this.priceCheckInput,
                    company: this.activeCompany,
                });
                this.priceCheckResults = res;
                this.showToast(`Analyzed ${res.summary.total_orders} order(s) successfully!`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.priceCheckLoading = false;
            }
        },

        exportPriceCheckCSV() {
            if (!this.priceCheckResults?.rows) return;
            const headers = ["Order Number", "Cost ($)", "Quote Price ($)", "Total SqFt", "Rate / SqFt", "Markup ($)", "Markup (%)", "Status"];
            const rows = this.priceCheckResults.rows.map(r => [
                r.order_number,
                r.cost.toFixed(2),
                r.quote.toFixed(2),
                r.sqft.toFixed(2),
                `"${r.rate_str}"`,
                r.markup.toFixed(2),
                r.markup_pct.toFixed(1) + "%",
                r.status,
            ]);
            let csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map(e => e.join(","))].join("\n");
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `price_check_${this.activeCompany}_${new Date().toISOString().slice(0, 10)}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            this.showToast("CSV file exported successfully", "success");
        },

        // =====================================================================
        // TAB 5: Price Reviewer (Schedule A Recalculator)
        // =====================================================================
        priceReviewInput: "",
        priceReviewLoading: false,
        priceReviewResults: null,

        async runPriceReview() {
            if (!this.priceReviewInput.trim()) {
                this.showToast("Please enter order numbers to review", "warning");
                return;
            }
            this.priceReviewLoading = true;
            try {
                const res = await this.apiCall("/api/price-reviewer/run", "POST", {
                    text: this.priceReviewInput,
                    company: this.activeCompany,
                });
                this.priceReviewResults = res;
                this.showToast(`Reviewed ${res.summary.total_orders} order(s) against Schedule A`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.priceReviewLoading = false;
            }
        },

        // =====================================================================
        // TAB 6: Reworks Processor
        // =====================================================================
        reworksRangeInput: "",
        reworksLoading: false,
        reworksEmailLoading: false,
        reworksResults: null,

        async processReworks() {
            if (!this.reworksRangeInput.trim()) {
                this.showToast("Please enter rework request IDs (e.g. 1-5 or 12, 14)", "warning");
                return;
            }
            this.reworksLoading = true;
            try {
                const res = await this.apiCall("/api/reworks/process", "POST", {
                    range: this.reworksRangeInput,
                    company: this.activeCompany,
                });
                this.reworksResults = res;
                this.showToast(`Processed ${res.summary.total_items} rework line(s)`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.reworksLoading = false;
            }
        },

        exportReworksCSV() {
            if (!this.reworksResults?.rows) return;
            const headers = ["RW#", "Dealer", "Order #", "Type", "Line", "Room", "Line Notes", "Attributes", "Original Cost ($)", "Rework Cost ($)"];
            const rows = this.reworksResults.rows.map(r => [
                r.rw_id,
                `"${r.dealer}"`,
                r.order_number,
                r.type,
                r.line,
                `"${r.room}"`,
                `"${r.line_notes}"`,
                `"${r.attributes}"`,
                r.original_cost.toFixed(2),
                r.rework_cost.toFixed(2),
            ]);
            let csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map(e => e.join(","))].join("\n");
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `reworks_summary_${this.activeCompany}_${new Date().toISOString().slice(0, 10)}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            this.showToast("Reworks CSV downloaded", "success");
        },

        async sendReworksEmail() {
            if (!this.reworksResults?.rows?.length) {
                this.showToast("No reworks to send", "warning");
                return;
            }
            if (!confirm(`Email reworks summary CSV to ${this.systemStatus?.integrations?.gmail_notifications?.to || 'configured recipient'}?`)) {
                return;
            }
            this.reworksEmailLoading = true;
            try {
                const res = await this.apiCall("/api/reworks/send-email", "POST", {
                    rows: this.reworksResults.rows,
                    company: this.activeCompany,
                });
                this.showToast(`Email dispatched to ${res.recipient} with CSV attachment!`, "success");
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.reworksEmailLoading = false;
            }
        },

        // =====================================================================
        // TAB 7: System Status & Audit Logs
        // =====================================================================
        logsList: [],
        logsLoading: false,

        async loadAuditLogs() {
            this.logsLoading = true;
            try {
                const res = await this.apiCall("/api/logs?limit=200");
                this.logsList = res.logs || [];
                this.showToast(`Loaded ${this.logsList.length} audit log entries`, "info");
            } catch (err) {
                this.showToast("Failed to load logs: " + err.message, "error");
            } finally {
                this.logsLoading = false;
            }
        },

        // =====================================================================
        // TAB 8: Credentials & Environment (.env & credentials.json)
        // =====================================================================
        credentialsInfo: null,
        credentialsLoading: false,
        showSecrets: false,
        envEditorText: "",
        envSaving: false,
        testingGroq: false,
        testingSheets: false,
        testingErp: {},
        testingEmail: false,

        copyToClipboard(text, label = "Copied") {
            if (!text) return;
            navigator.clipboard.writeText(text).then(() => {
                this.showToast(`${label} copied to clipboard!`, "info");
            }).catch(() => {
                this.showToast("Failed to copy", "error");
            });
        },

        async loadCredentials() {
            this.credentialsLoading = true;
            try {
                const res = await this.apiCall("/api/credentials");
                this.credentialsInfo = res;
                this.envEditorText = res.env_content || "";
            } catch (err) {
                this.showToast("Failed to load credentials: " + err.message, "error");
            } finally {
                this.credentialsLoading = false;
            }
        },

        async testGroqConnection() {
            this.testingGroq = true;
            try {
                const res = await this.apiCall("/api/credentials/test-groq", "POST");
                this.showToast(`Groq AI connected! Latency: ${res.latency_ms}ms (${res.model})`, "success");
            } catch (err) {
                this.showToast("Groq test failed: " + err.message, "error");
            } finally {
                this.testingGroq = false;
            }
        },

        async testSheetsConnection() {
            this.testingSheets = true;
            try {
                const res = await this.apiCall("/api/credentials/test-sheets", "POST");
                this.showToast(`Sheets API connected! Verified: "${res.spreadsheet_title}"`, "success");
            } catch (err) {
                this.showToast("Sheets API test failed: " + err.message, "error");
            } finally {
                this.testingSheets = false;
            }
        },

        async testErpConnection(companyKey) {
            this.testingErp[companyKey] = true;
            try {
                const res = await this.apiCall("/api/credentials/test-erp", "POST", { company: companyKey });
                this.showToast(`Authenticated with ${res.company} ERP! Token valid until ${res.expires_at}`, "success");
                await this.loadCredentials();
                await this.loadSystemStatus();
            } catch (err) {
                this.showToast("ERP login failed: " + err.message, "error");
            } finally {
                this.testingErp[companyKey] = false;
            }
        },

        async testEmailConnection() {
            this.testingEmail = true;
            try {
                const res = await this.apiCall("/api/credentials/test-email", "POST");
                this.showToast(`Test notification sent successfully to ${res.recipient}!`, "success");
            } catch (err) {
                this.showToast("Email test failed: " + err.message, "error");
            } finally {
                this.testingEmail = false;
            }
        },

        async saveEnvFile() {
            if (!this.envEditorText.trim()) {
                this.showToast(".env content cannot be empty", "warning");
                return;
            }
            if (!confirm("Save changes to .env file and reload runtime configuration?")) {
                return;
            }
            this.envSaving = true;
            try {
                await this.apiCall("/api/credentials/save-env", "POST", { content: this.envEditorText });
                this.showToast(".env saved and runtime configuration reloaded!", "success");
                await this.loadCredentials();
                await this.loadSystemStatus();
            } catch (err) {
                this.showToast("Failed to save .env: " + err.message, "error");
            } finally {
                this.envSaving = false;
            }
        },

        // =====================================================================
        // TAB 9: Dealer Management (Names, Aliases & Google Sheet URLs)
        // =====================================================================
        dealersListAll: [],
        dealersFilterCompany: "all",
        dealersSearchQuery: "",
        dealersLoading: false,
        dealerModalOpen: false,
        dealerModalIsEdit: false,
        dealerForm: {
            company: "speedy",
            name: "",
            sheetUrl: "",
            aliases: "",
            originalName: "",
        },
        dealerSaving: false,
        testingDealerSheet: false,
        testedSheetTitle: null,

        get filteredDealersListAll() {
            let list = this.dealersListAll;
            if (this.dealersFilterCompany !== "all") {
                list = list.filter(d => d.company_key === this.dealersFilterCompany);
            }
            if (this.dealersSearchQuery.trim()) {
                const q = this.dealersSearchQuery.trim().toLowerCase();
                list = list.filter(d => 
                    d.dealer_name.toLowerCase().includes(q) ||
                    d.aliases_str.toLowerCase().includes(q) ||
                    d.sheet_id.toLowerCase().includes(q) ||
                    d.company_label.toLowerCase().includes(q)
                );
            }
            return list;
        },

        async loadAllDealers() {
            this.dealersLoading = true;
            try {
                const res = await this.apiCall("/api/dealers/all");
                this.dealersListAll = res.dealers || [];
            } catch (err) {
                this.showToast("Failed to load dealers: " + err.message, "error");
            } finally {
                this.dealersLoading = false;
            }
        },

        openAddDealerModal(defaultCompany = null) {
            this.dealerModalIsEdit = false;
            this.testedSheetTitle = null;
            this.dealerForm = {
                company: defaultCompany || this.activeCompany || "speedy",
                name: "",
                sheetUrl: "",
                aliases: "",
                originalName: "",
                sheetType: "new",
            };
            this.dealerModalOpen = true;
        },

        openEditDealerModal(dealer) {
            this.dealerModalIsEdit = true;
            this.testedSheetTitle = null;
            this.dealerForm = {
                company: dealer.company_key,
                name: dealer.dealer_name,
                sheetUrl: dealer.sheet_url || dealer.sheet_id,
                aliases: dealer.aliases_str || "",
                originalName: dealer.dealer_name,
                sheetType: dealer.sheet_type || "new",
            };
            this.dealerModalOpen = true;
        },

        async testDealerSheetAccess() {
            if (!this.dealerForm.sheetUrl.trim()) {
                this.showToast("Please enter a Google Sheet URL or ID", "warning");
                return;
            }
            this.testingDealerSheet = true;
            this.testedSheetTitle = null;
            try {
                const res = await this.apiCall("/api/dealers/test-sheet", "POST", {
                    sheet_url: this.dealerForm.sheetUrl,
                });
                this.testedSheetTitle = res.title;
                this.showToast(`Verified access: "${res.title}"`, "success");
            } catch (err) {
                this.showToast("Sheet verification failed: " + err.message, "error");
            } finally {
                this.testingDealerSheet = false;
            }
        },

        async saveDealer() {
            if (!this.dealerForm.name.trim()) {
                this.showToast("Please enter a dealer name", "warning");
                return;
            }
            if (!this.dealerForm.sheetUrl.trim()) {
                this.showToast("Please enter a Google Sheet URL or Spreadsheet ID", "warning");
                return;
            }

            this.dealerSaving = true;
            try {
                const res = await this.apiCall("/api/dealers/save", "POST", {
                    company: this.dealerForm.company,
                    name: this.dealerForm.name.trim(),
                    sheet_url: this.dealerForm.sheetUrl.trim(),
                    aliases: this.dealerForm.aliases.trim(),
                    original_name: this.dealerModalIsEdit ? this.dealerForm.originalName : null,
                    sheet_type: this.dealerForm.sheetType || "new",
                });

                this.showToast(`Saved dealer "${res.dealer_name}" successfully!`, "success");
                this.dealerModalOpen = false;
                await this.loadAllDealers();
                await this.loadDealers();
            } catch (err) {
                this.showToast(err.message, "error");
            } finally {
                this.dealerSaving = false;
            }
        },

        async deleteDealer(dealer) {
            if (!confirm(`Are you sure you want to delete dealer "${dealer.dealer_name}" from ${dealer.company_label}?`)) {
                return;
            }
            try {
                await this.apiCall("/api/dealers/delete", "POST", {
                    company: dealer.company_key,
                    name: dealer.dealer_name,
                });
                this.showToast(`Deleted dealer "${dealer.dealer_name}"`, "info");
                await this.loadAllDealers();
                await this.loadDealers();
            } catch (err) {
                this.showToast(err.message, "error");
            }
        },

        // =====================================================================
        // TAB: Payments & Charges (Ledger & Sheets)
        // =====================================================================
        paymentsSubView: "payments", // 'payments' or 'ledger_summary' or 'categorize' or 'columns'
        paymentEntryTab: "payment",  // 'payment' or 'charge' or 'bulk'
        sheetConfigs: {
            old: { name: "Old Sheet Format", tab_name: "2026", month_col: "A", date_col: "C", order_col: "D", customer_col: "E", qty_col: "F", price_col: "G", eft_col: "H", eft_alt_col: "J", cash_col: "I", cash_alt_col: "L", alt_col: "J", comments_col: "K" },
            new: { name: "New Sheet Format", tab_name: "2026", month_col: "B", date_col: "C", order_col: "D", customer_col: "E", qty_col: "F", price_col: "G", eft_col: "H", eft_alt_col: "J", cash_col: "I", cash_alt_col: "L", alt_col: "J", comments_col: "K" },
        },
        sheetConfigsSaving: false,

        // Single Payment Form State
        singlePayment: {
            orderNumber: "",
            dealer: "",
            dealerId: null,
            company: "speedy",
            amount: "",
            isNegative: false,
            type: "EFT",
            sender: "",
            date: new Date().toISOString().split("T")[0],
            targetMode: "primary", // primary, alternate, auto
            postToErp: true,
            pushToSheets: true,
        },
        lookingUpOrder: false,
        lookedUpOrderInfo: null,
        multiOrderItems: [],
        lookingUpMultiOrders: false,

        // Single Charge Form State
        singleCharge: {
            orderNumber: "",
            dealer: "",
            dealerId: null,
            company: "speedy",
            amount: "",
            category: "Additional Fabric Surcharge",
            description: "",
            date: new Date().toISOString().split("T")[0],
            postToErp: true,
            pushToSheets: true,
        },
        lookingUpChargeOrder: false,
        lookedUpChargeInfo: null,

        // Bulk Payments & Charges Parsing
        bulkPaymentsText: "",
        bulkPaymentsDate: new Date().toISOString().split("T")[0],
        bulkPaymentsCompany: "all",
        parsingBulkPayments: false,

        // Pending Queue & History
        pendingPayments: [],
        pushedPaymentsHistory: [],
        selectedPaymentIndices: [],
        pushingPayments: false,
        precheckingSheets: false,
        queueFilter: "all", // all, payments, charges, ready, conflicts, appended

        // Accounts & Dealer Ledger Summaries
        accountsSummary: null,
        loadingAccountsSummary: false,
        selectedDealerBalance: null,
        loadingDealerBalance: false,

        // Drag & Drop State
        draggedDealer: null,
        dragOverTarget: null,

        get queueStats() {
            let totalEft = 0;
            let totalCash = 0;
            let totalCharges = 0;
            let paymentCount = 0;
            let chargeCount = 0;

            for (const p of this.pendingPayments) {
                const amt = parseFloat(p.amount) || 0;
                const isChg = p.entry_type === "charge" || (p.type || "").toUpperCase() === "CHARGE";
                if (isChg) {
                    totalCharges += amt;
                    chargeCount++;
                } else {
                    paymentCount++;
                    if ((p.type || "EFT").toUpperCase() === "EFT") {
                        totalEft += amt;
                    } else {
                        totalCash += amt;
                    }
                }
            }
            return {
                totalCount: this.pendingPayments.length,
                paymentCount: paymentCount,
                chargeCount: chargeCount,
                totalEft: totalEft,
                totalCash: totalCash,
                totalPayments: totalEft + totalCash,
                totalCharges: totalCharges,
                netTotal: (totalEft + totalCash) - totalCharges,
                selectedCount: this.selectedPaymentIndices.length,
            };
        },

        get filteredPendingPayments() {
            if (this.queueFilter === "payments") {
                return this.pendingPayments.filter(p => p.entry_type !== "charge" && (p.type || "").toUpperCase() !== "CHARGE");
            } else if (this.queueFilter === "charges") {
                return this.pendingPayments.filter(p => p.entry_type === "charge" || (p.type || "").toUpperCase() === "CHARGE");
            } else if (this.queueFilter === "ready") {
                return this.pendingPayments.filter(p => !p.sheetCheck || (p.sheetCheck.found && !p.sheetCheck.is_primary_occupied));
            } else if (this.queueFilter === "conflicts") {
                return this.pendingPayments.filter(p => p.sheetCheck && p.sheetCheck.is_primary_occupied);
            } else if (this.queueFilter === "appended") {
                return this.pendingPayments.filter(p => p.sheetCheck && !p.sheetCheck.found);
            }
            return this.pendingPayments;
        },

        async loadPaymentsTab() {
            await this.loadSheetConfigs();
            await this.loadAccountsSummary();
            if (!this.dealersListAll.length) {
                await this.loadAllDealers();
            }
        },

        async loadAccountsSummary() {
            this.loadingAccountsSummary = true;
            try {
                const res = await this.apiCall(`/api/ledger/accounts-summary?company=${this.activeCompany}`);
                if (res.data) {
                    this.accountsSummary = res.data;
                }
            } catch (err) {
                console.warn("Failed to fetch accounts summary:", err);
            } finally {
                this.loadingAccountsSummary = false;
            }
        },

        async loadDealerBalance(dealerId) {
            if (!dealerId) return;
            this.loadingDealerBalance = true;
            try {
                const res = await this.apiCall(`/api/ledger/dealers/${dealerId}/balance-summary?company=${this.activeCompany}`);
                if (res.data) {
                    this.selectedDealerBalance = res.data;
                }
            } catch (err) {
                console.warn("Failed to fetch dealer balance:", err);
            } finally {
                this.loadingDealerBalance = false;
            }
        },

        async loadSheetConfigs() {
            try {
                const res = await this.apiCall("/api/sheet-configs");
                if (res.configs) {
                    this.sheetConfigs = res.configs;
                }
            } catch (err) {
                console.error("Failed to load sheet configs:", err);
            }
        },

        async saveSheetConfigs() {
            this.sheetConfigsSaving = true;
            try {
                const res = await this.apiCall("/api/sheet-configs", "POST", {
                    configs: this.sheetConfigs,
                });
                if (res.configs) {
                    this.sheetConfigs = res.configs;
                }
                this.showToast("Sheet column layouts saved successfully!", "success");
            } catch (err) {
                this.showToast("Failed to save column layouts: " + err.message, "error");
            } finally {
                this.sheetConfigsSaving = false;
            }
        },

        // Multi-order detection helper
        hasMultipleOrders(val) {
            if (!val) return false;
            const str = String(val).trim();
            if (str.includes(",") || str.includes(";")) return true;
            const matches = str.match(/(?:ORD[-_ ]?)?\d+/gi);
            return matches && matches.length > 1;
        },

        getDetectedOrderList(val) {
            if (!val) return [];
            const str = String(val).trim();
            const parts = str.split(/[,;\s]+/).filter(Boolean);
            const list = [];
            for (const p of parts) {
                const clean = p.trim().replace(/^ORD[-_ ]?/i, "");
                if (clean) {
                    const norm = clean.match(/^\d+$/) ? `ORD-${clean.padStart(4, "0")}` : `ORD-${clean}`;
                    if (!list.includes(norm)) {
                        list.push(norm);
                    }
                }
            }
            return list;
        },

        async fetchMultiOrderDetails() {
            const raw = (this.singlePayment.orderNumber || "").trim();
            const orders = this.getDetectedOrderList(raw);
            if (!orders.length) return;
            this.lookingUpMultiOrders = true;
            try {
                const res = await this.apiCall("/api/payments/lookup-multi-orders", "POST", {
                    order_numbers: orders,
                    company: this.singlePayment.company || this.activeCompany,
                });
                if (res.orders) {
                    const totalEntered = parseFloat(this.singlePayment.amount) || 0;
                    const count = res.orders.length;
                    this.multiOrderItems = res.orders.map(o => ({
                        ...o,
                        split_amount: totalEntered > 0 ? (totalEntered / count) : (o.total_price || 0),
                    }));
                    if (res.common_dealer) {
                        this.singlePayment.dealer = res.common_dealer;
                    }
                    if (res.common_dealer_id) {
                        this.singlePayment.dealerId = res.common_dealer_id;
                        this.loadDealerBalance(res.common_dealer_id);
                    }
                    this.showToast(`Fetched details for ${res.count} orders. Common dealer: ${res.common_dealer || "Multiple"}`, "info");
                }
            } catch (err) {
                this.showToast("Failed to fetch multi-order details: " + err.message, "error");
            } finally {
                this.lookingUpMultiOrders = false;
            }
        },

        getMultiOrderTotalAllocated() {
            if (!this.multiOrderItems) return 0;
            return this.multiOrderItems.reduce((acc, it) => acc + (parseFloat(it.split_amount) || 0), 0);
        },

        distributeAmountProportionally() {
            if (!this.multiOrderItems.length) return;
            this.multiOrderItems.forEach(it => {
                it.split_amount = parseFloat(it.total_price) || 0;
            });
            const totalSum = this.getMultiOrderTotalAllocated();
            this.singlePayment.amount = totalSum.toFixed(2);
            this.showToast(`Updated split amounts to match ERP wholesale prices (Total: $${totalSum.toFixed(2)})`, "success");
        },

        addMultiOrdersToQueue() {
            if (!this.multiOrderItems.length) return;
            const dealer = (this.singlePayment.dealer || "").trim();
            if (!dealer) {
                this.showToast("Please specify a Dealer Name for the payments", "warning");
                return;
            }

            let addedCount = 0;
            const orderCount = this.multiOrderItems.length;

            for (let idx = 0; idx < this.multiOrderItems.length; idx++) {
                const mo = this.multiOrderItems[idx];
                const amt = parseFloat(mo.split_amount);
                if (isNaN(amt) || amt === 0) continue;

                const item = {
                    id: "pay_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
                    entry_type: "payment",
                    order_number: mo.order_number,
                    dealer: mo.dealer || dealer,
                    dealer_id: mo.dealer_id || this.singlePayment.dealerId || null,
                    company_key: mo.company_key || this.singlePayment.company || "speedy",
                    amount: this.singlePayment.isNegative ? -Math.abs(amt) : Math.abs(amt),
                    is_negative: Boolean(this.singlePayment.isNegative),
                    type: this.singlePayment.type || "EFT",
                    description: `Split payment (${idx + 1} of ${orderCount}) | ${this.singlePayment.sender || 'Payer'}`,
                    sender: this.singlePayment.sender || `Split ${idx + 1}/${orderCount}`,
                    payment_date: this.singlePayment.date || new Date().toISOString().split("T")[0],
                    target_column_mode: this.singlePayment.targetMode || "primary",
                    post_to_erp: Boolean(this.singlePayment.postToErp),
                    push_to_sheets: Boolean(this.singlePayment.pushToSheets),
                    sheetCheck: null,
                    status: "pending",
                };

                this.pendingPayments.unshift(item);
                addedCount++;
            }

            this.showToast(`Added ${addedCount} individual split payment rows to queue!`, "success");
            this.singlePayment.orderNumber = "";
            this.singlePayment.amount = "";
            this.singlePayment.sender = "";
            this.singlePayment.isNegative = false;
            this.multiOrderItems = [];
            this.lookedUpOrderInfo = null;
        },

        // Single Payment Order Lookup via ERP
        async lookupSinglePaymentOrder() {
            const ord = (this.singlePayment.orderNumber || "").trim();
            if (!ord) return;
            if (this.hasMultipleOrders(ord)) {
                await this.fetchMultiOrderDetails();
                return;
            }
            this.lookingUpOrder = true;
            this.lookedUpOrderInfo = null;
            try {
                const res = await this.apiCall("/api/payments/lookup-order", "POST", {
                    order_number: ord,
                    company: this.singlePayment.company || this.activeCompany,
                });
                if (res.found) {
                    this.lookedUpOrderInfo = res;
                    if (res.dealer) {
                        this.singlePayment.dealer = res.dealer;
                    }
                    if (res.dealer_id) {
                        this.singlePayment.dealerId = res.dealer_id;
                        this.loadDealerBalance(res.dealer_id);
                    }
                    if (res.company_key) {
                        this.singlePayment.company = res.company_key;
                    }
                    this.showToast(`Found Order: ${res.order_number} (${res.dealer || "Unknown"}) • Wholesale: $${res.total_price || 0} (${res.payment_status || "due"})`, "info");
                } else {
                    this.lookedUpOrderInfo = { found: false, message: "Order not found in ERP (can still push to sheet)" };
                }
            } catch (err) {
                console.warn("ERP order lookup failed:", err);
            } finally {
                this.lookingUpOrder = false;
            }
        },

        // Single Charge Order Lookup via ERP
        async lookupSingleChargeOrder() {
            const ord = (this.singleCharge.orderNumber || "").trim();
            if (!ord) return;
            this.lookingUpChargeOrder = true;
            this.lookedUpChargeInfo = null;
            try {
                const res = await this.apiCall("/api/payments/lookup-order", "POST", {
                    order_number: ord,
                    company: this.singleCharge.company || this.activeCompany,
                });
                if (res.found) {
                    this.lookedUpChargeInfo = res;
                    if (res.dealer) {
                        this.singleCharge.dealer = res.dealer;
                    }
                    if (res.dealer_id) {
                        this.singleCharge.dealerId = res.dealer_id;
                        this.loadDealerBalance(res.dealer_id);
                    }
                    if (res.company_key) {
                        this.singleCharge.company = res.company_key;
                    }
                    this.showToast(`Found Order for Charge: ${res.order_number} (${res.dealer}) • Total: $${res.total_price || 0}`, "info");
                } else {
                    this.lookedUpChargeInfo = { found: false, message: "Order not found in ERP (can still push to sheet)" };
                }
            } catch (err) {
                console.warn("ERP charge lookup failed:", err);
            } finally {
                this.lookingUpChargeOrder = false;
            }
        },

        toggleSingleMinus() {
            this.singlePayment.isNegative = !this.singlePayment.isNegative;
            if (this.singlePayment.amount) {
                const num = parseFloat(this.singlePayment.amount);
                if (!isNaN(num)) {
                    this.singlePayment.amount = Math.abs(num).toString();
                }
            }
        },

        addSinglePaymentToQueue() {
            let ord = (this.singlePayment.orderNumber || "").trim();
            const isLumpSum = !ord || ord.toUpperCase() === "LUMP-SUM" || ord.toUpperCase() === "GENERAL";
            if (isLumpSum) {
                ord = "LUMP-SUM";
            }
            const dealer = (this.singlePayment.dealer || "").trim();
            if (!dealer) {
                this.showToast("Please select or enter a Dealer Name", "warning");
                return;
            }
            let amt = parseFloat(this.singlePayment.amount);
            if (isNaN(amt) || amt === 0) {
                this.showToast("Please enter a valid payment Amount", "warning");
                return;
            }
            if (this.singlePayment.isNegative && amt > 0) {
                amt = -amt;
            }

            const item = {
                id: "pay_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
                entry_type: "payment",
                order_number: isLumpSum ? "LUMP-SUM" : (ord.toUpperCase().startsWith("ORD-") ? ord : `ORD-${ord.replace(/^0+/, '').padStart(4, '0')}`),
                dealer: dealer,
                dealer_id: this.singlePayment.dealerId || null,
                company_key: this.singlePayment.company || "speedy",
                amount: amt,
                is_negative: this.singlePayment.isNegative || amt < 0,
                is_general_deposit: isLumpSum,
                type: this.singlePayment.type || "EFT",
                description: (this.singlePayment.sender || "").trim() || (isLumpSum ? `Lump-sum deposit from ${dealer}` : `Payment for ${ord}`),
                sender: (this.singlePayment.sender || "").trim() || (isLumpSum ? "Lump-sum Deposit" : ""),
                payment_date: this.singlePayment.date || new Date().toISOString().split("T")[0],
                target_column_mode: this.singlePayment.targetMode || "primary",
                post_to_erp: Boolean(this.singlePayment.postToErp),
                push_to_sheets: Boolean(this.singlePayment.pushToSheets),
                sheetCheck: null,
                status: "pending",
            };

            this.pendingPayments.unshift(item);
            this.showToast(isLumpSum ? `Added General Lump-Sum Deposit ($${Math.abs(amt)}) for ${item.dealer} to queue` : `Added Payment for ${item.order_number} (${item.dealer}) to queue`, "success");

            // Reset form partially
            this.singlePayment.orderNumber = "";
            this.singlePayment.amount = "";
            this.singlePayment.sender = "";
            this.singlePayment.isNegative = false;
            this.multiOrderItems = [];
            this.lookedUpOrderInfo = null;
        },

        addSingleChargeToQueue() {
            const ord = (this.singleCharge.orderNumber || "").trim();
            if (!ord) {
                this.showToast("Please enter an Order Number for the charge", "warning");
                return;
            }
            const dealer = (this.singleCharge.dealer || "").trim();
            if (!dealer) {
                this.showToast("Please select or enter a Dealer Name", "warning");
                return;
            }
            let amt = parseFloat(this.singleCharge.amount);
            if (isNaN(amt) || amt <= 0) {
                this.showToast("Please enter a valid positive Charge Amount", "warning");
                return;
            }

            const desc = (this.singleCharge.description || "").trim() || this.singleCharge.category;
            const item = {
                id: "chg_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
                entry_type: "charge",
                order_number: ord.toUpperCase().startsWith("ORD-") ? ord : `ORD-${ord.replace(/^0+/, '').padStart(4, '0')}`,
                dealer: dealer,
                dealer_id: this.singleCharge.dealerId || null,
                company_key: this.singleCharge.company || "speedy",
                amount: amt,
                is_negative: false,
                type: "Charge",
                category: this.singleCharge.category,
                description: desc,
                sender: `CHARGE: ${desc}`,
                payment_date: this.singleCharge.date || new Date().toISOString().split("T")[0],
                target_column_mode: "primary",
                post_to_erp: Boolean(this.singleCharge.postToErp),
                push_to_sheets: Boolean(this.singleCharge.pushToSheets),
                sheetCheck: null,
                status: "pending",
            };

            this.pendingPayments.unshift(item);
            this.showToast(`Added Charge ($${amt}) for ${item.order_number} (${item.dealer}) to queue`, "warning");

            // Reset form partially
            this.singleCharge.orderNumber = "";
            this.singleCharge.amount = "";
            this.singleCharge.description = "";
            this.lookedUpChargeInfo = null;
        },

        async parseBulkPayments() {
            const text = (this.bulkPaymentsText || "").trim();
            if (!text) {
                this.showToast("Please paste payment or charge text into the bulk box", "warning");
                return;
            }
            this.parsingBulkPayments = true;
            try {
                const res = await this.apiCall("/api/payments/parse", "POST", {
                    text: text,
                    default_date: this.bulkPaymentsDate,
                    company: this.bulkPaymentsCompany === "all" ? null : this.bulkPaymentsCompany,
                });

                const parsed = res.payments || [];
                if (!parsed.length) {
                    this.showToast("No payment or charge entries could be parsed from the text", "warning");
                    return;
                }

                let count = 0;
                for (const it of parsed) {
                    const isCharge = it.entry_type === "charge" || (it.type || "").toUpperCase() === "CHARGE";
                    const item = {
                        id: (isCharge ? "chg_" : "pay_") + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
                        entry_type: isCharge ? "charge" : "payment",
                        order_number: it.order_number,
                        dealer: it.dealer || "",
                        dealer_id: it.dealer_id || null,
                        company_key: it.company_key || "speedy",
                        amount: parseFloat(it.amount) || 0,
                        is_negative: Boolean(it.is_negative || (parseFloat(it.amount) < 0)),
                        type: isCharge ? "Charge" : ((it.type || "EFT").toUpperCase() === "CASH" ? "Cash" : "EFT"),
                        description: it.description || it.sender || (isCharge ? "Charge" : "Payment"),
                        sender: it.sender || (isCharge ? `CHARGE: ${it.description || 'Charge'}` : ""),
                        payment_date: it.payment_date || this.bulkPaymentsDate,
                        target_column_mode: "primary",
                        post_to_erp: true,
                        push_to_sheets: true,
                        sheetCheck: null,
                        status: "pending",
                    };
                    this.pendingPayments.unshift(item);
                    count++;
                }

                this.showToast(`Parsed & queued ${count} entries (payments & charges)!`, "success");
                this.bulkPaymentsText = "";
            } catch (err) {
                this.showToast("Failed to parse payments: " + err.message, "error");
            } finally {
                this.parsingBulkPayments = false;
            }
        },

        toggleSelectAllPayments() {
            if (this.selectedPaymentIndices.length === this.pendingPayments.length) {
                this.selectedPaymentIndices = [];
            } else {
                this.selectedPaymentIndices = this.pendingPayments.map((_, i) => i);
            }
        },

        togglePaymentSelection(idx) {
            const pos = this.selectedPaymentIndices.indexOf(idx);
            if (pos > -1) {
                this.selectedPaymentIndices.splice(pos, 1);
            } else {
                this.selectedPaymentIndices.push(idx);
            }
        },

        async checkPaymentSheet(item) {
            if (!item.dealer) {
                this.showToast("Cannot check sheet: Dealer is not specified", "warning");
                return;
            }
            item.checking = true;
            try {
                const res = await this.apiCall("/api/payments/check-sheet", "POST", {
                    dealer: item.dealer,
                    order_number: item.order_number,
                    type: item.type,
                    company: item.company_key,
                });
                item.sheetCheck = res;
            } catch (err) {
                item.sheetCheck = { found: false, error: err.message };
            } finally {
                item.checking = false;
            }
        },

        async precheckAllInQueue() {
            if (!this.pendingPayments.length) return;
            this.precheckingSheets = true;
            this.showToast("Checking Google Sheets for all queued orders...", "info");
            try {
                const promises = this.pendingPayments.map(p => this.checkPaymentSheet(p));
                await Promise.allSettled(promises);
                this.showToast("Pre-check complete for all queue items!", "success");
            } catch (err) {
                this.showToast("Pre-check completed with warnings", "warning");
            } finally {
                this.precheckingSheets = false;
            }
        },

        async pushSinglePayment(item, idx) {
            if (!item.dealer && !item.dealer_id) {
                this.showToast("Cannot push: Dealer or Dealer ID is required", "error");
                return;
            }
            item.pushing = true;
            try {
                const payload = {
                    entry_type: item.entry_type || "payment",
                    order_number: item.order_number,
                    amount: item.amount,
                    type: item.type,
                    description: item.description || item.sender,
                    sender: item.sender,
                    payment_date: item.payment_date,
                    dealer: item.dealer,
                    dealer_id: item.dealer_id,
                    company_key: item.company_key,
                    is_negative: item.is_negative,
                    target_column_mode: item.target_column_mode || "primary",
                    post_to_erp: item.post_to_erp !== false,
                    push_to_sheets: item.push_to_sheets !== false,
                };
                const res = await this.apiCall("/api/ledger/execute-transaction", "POST", payload);
                
                // Add to history
                this.pushedPaymentsHistory.unshift({
                    ...item,
                    result: res,
                    pushed_at: new Date().toLocaleTimeString(),
                });

                // Remove from pending queue
                const realIdx = this.pendingPayments.indexOf(item);
                if (realIdx > -1) {
                    this.pendingPayments.splice(realIdx, 1);
                    this.selectedPaymentIndices = this.selectedPaymentIndices.filter(i => i !== realIdx).map(i => i > realIdx ? i - 1 : i);
                }

                if (res.sheet_result?.status === "appended_not_found") {
                    this.showToast(`Order ${item.order_number} appended at Row ${res.sheet_result.row} in Red & posted to ERP!`, "warning");
                } else if (res.sheet_result?.warning) {
                    this.showToast(`Applied ${item.order_number}: ${res.sheet_result.warning}`, "warning");
                } else {
                    this.showToast(`Successfully posted ${item.entry_type.toUpperCase()} for ${item.order_number}!`, "success");
                }

                // Refresh accounts summary
                this.loadAccountsSummary();
            } catch (err) {
                this.showToast(`Failed to execute ${item.order_number}: ` + err.message, "error");
            } finally {
                item.pushing = false;
            }
        },

        async pushApprovedPayments() {
            const targets = this.selectedPaymentIndices.length > 0 
                ? this.selectedPaymentIndices.map(i => this.pendingPayments[i]).filter(Boolean)
                : this.pendingPayments;

            if (!targets.length) {
                this.showToast("No items in queue to push", "warning");
                return;
            }

            if (!confirm(`Are you ready to execute & post ${targets.length} transactions (ERP ledger and/or Google Sheets)?`)) {
                return;
            }

            this.pushingPayments = true;
            this.showToast(`Processing ${targets.length} transactions...`, "info");

            let successCount = 0;
            let failCount = 0;

            const remaining = [...this.pendingPayments];

            for (const item of targets) {
                try {
                    const payload = {
                        entry_type: item.entry_type || "payment",
                        order_number: item.order_number,
                        amount: item.amount,
                        type: item.type,
                        description: item.description || item.sender,
                        sender: item.sender,
                        payment_date: item.payment_date,
                        dealer: item.dealer,
                        dealer_id: item.dealer_id,
                        company_key: item.company_key,
                        is_negative: item.is_negative,
                        target_column_mode: item.target_column_mode || "primary",
                        post_to_erp: item.post_to_erp !== false,
                        push_to_sheets: item.push_to_sheets !== false,
                    };
                    const res = await this.apiCall("/api/ledger/execute-transaction", "POST", payload);
                    this.pushedPaymentsHistory.unshift({
                        ...item,
                        result: res,
                        pushed_at: new Date().toLocaleTimeString(),
                    });

                    const remIdx = remaining.indexOf(item);
                    if (remIdx > -1) remaining.splice(remIdx, 1);
                    successCount++;
                } catch (err) {
                    failCount++;
                    item.error = err.message;
                }
            }

            this.pendingPayments = remaining;
            this.selectedPaymentIndices = [];
            this.pushingPayments = false;

            this.showToast(`Done! Successfully posted ${successCount} items (${failCount} failed).`, successCount > 0 ? "success" : "warning");
            this.loadAccountsSummary();
        },

        deletePayment(item) {
            const idx = this.pendingPayments.indexOf(item);
            if (idx > -1) {
                this.pendingPayments.splice(idx, 1);
                this.selectedPaymentIndices = this.selectedPaymentIndices.filter(i => i !== idx).map(i => i > idx ? i - 1 : i);
            }
        },

        deleteSelectedPayments() {
            if (!this.selectedPaymentIndices.length) return;
            if (!confirm(`Delete ${this.selectedPaymentIndices.length} selected payments from queue?`)) return;
            this.pendingPayments = this.pendingPayments.filter((_, i) => !this.selectedPaymentIndices.includes(i));
            this.selectedPaymentIndices = [];
            this.showToast("Deleted selected payments", "info");
        },

        clearPaymentsQueue() {
            if (!this.pendingPayments.length) return;
            if (!confirm("Are you sure you want to clear all payments in the queue?")) return;
            this.pendingPayments = [];
            this.selectedPaymentIndices = [];
            this.showToast("Queue cleared", "info");
        },

        // Inline Editing in Payments Queue
        updatePaymentAmount(item, val) {
            if (val === "" || val === null || val === undefined) {
                item.amount = 0;
                return;
            }
            const str = String(val).trim();
            if (str.startsWith("-")) {
                item.is_negative = true;
            }
            const num = Math.abs(parseFloat(str) || 0);
            item.amount = (item.is_negative || (item.entry_type !== "charge" && str.startsWith("-"))) ? -num : num;
        },

        togglePaymentNegative(item) {
            if (item.entry_type === "charge" || (item.type || "").toUpperCase() === "CHARGE") return;
            item.is_negative = !item.is_negative;
            const absVal = Math.abs(parseFloat(item.amount) || 0);
            item.amount = item.is_negative ? -absVal : absVal;
        },

        onPaymentDealerChange(item) {
            const raw = (item.dealer || "").trim();
            if (!raw) return;

            // Invalidate existing pre-check since the target sheet changed
            item.sheetCheck = null;

            // Match against known dealers in dealersListAll (case-insensitive exact or token match)
            const lower = raw.toLowerCase();
            const tokens = lower.split(/[\s\-_,]+/).filter(w => w.length >= 2);

            let match = (this.dealersListAll || []).find(d => {
                const dName = (d.dealer_name || d.name || "").toLowerCase();
                if (dName === lower) return true;
                if (Array.isArray(d.aliases) && d.aliases.some(a => (a || "").toLowerCase() === lower)) return true;
                return false;
            });

            if (!match && tokens.length > 0) {
                match = (this.dealersListAll || []).find(d => {
                    const dName = (d.dealer_name || d.name || "").toLowerCase();
                    const aliases = (d.aliases || []).map(a => (a || "").toLowerCase());
                    for (const t of tokens) {
                        if (t === dName || aliases.includes(t)) return true;
                        if (dName.length >= 4 && t.startsWith(dName)) return true;
                        if (aliases.some(a => a.length >= 4 && t.startsWith(a))) return true;
                    }
                    return false;
                });
            }

            if (match) {
                item.dealer = match.dealer_name || match.name;
                item.sheet_type = match.sheet_type || "new";
                if (match.company_key && !item.company_key) {
                    item.company_key = match.company_key;
                }
                if (match.dealer_id) {
                    item.dealer_id = match.dealer_id;
                }
            }
        },

        toggleItemSheetType(item) {
            item.sheet_type = item.sheet_type === "old" ? "new" : "old";
            item.sheetCheck = null;
        },

        // Drag and Drop Sheet Categorization
        handleDealerDragStart(e, dealer) {
            this.draggedDealer = dealer;
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", JSON.stringify({
                company_key: dealer.company_key,
                dealer_name: dealer.dealer_name,
                sheet_type: dealer.sheet_type || "new",
            }));
        },

        handleDealerDragOver(e, targetType) {
            e.preventDefault();
            this.dragOverTarget = targetType;
        },

        handleDealerDragLeave(e) {
            this.dragOverTarget = null;
        },

        async handleDealerDrop(e, targetType) {
            e.preventDefault();
            this.dragOverTarget = null;
            if (!this.draggedDealer) return;

            const dealer = this.draggedDealer;
            if ((dealer.sheet_type || "new") === targetType) {
                this.draggedDealer = null;
                return; // Already in this category
            }

            const oldType = dealer.sheet_type || "new";
            dealer.sheet_type = targetType; // optimistic update

            try {
                await this.apiCall("/api/dealers/sheet-type", "POST", {
                    company: dealer.company_key,
                    name: dealer.dealer_name,
                    sheet_type: targetType,
                });
                this.showToast(`Moved "${dealer.dealer_name}" to ${targetType === "old" ? "Old Sheet Format" : "New Sheet Format"}`, "success");
            } catch (err) {
                dealer.sheet_type = oldType; // revert
                this.showToast("Failed to update dealer sheet type: " + err.message, "error");
            } finally {
                this.draggedDealer = null;
            }
        },

        async toggleDealerSheetTypeBtn(dealer) {
            const current = dealer.sheet_type || "new";
            const targetType = current === "old" ? "new" : "old";
            dealer.sheet_type = targetType;
            try {
                await this.apiCall("/api/dealers/sheet-type", "POST", {
                    company: dealer.company_key,
                    name: dealer.dealer_name,
                    sheet_type: targetType,
                });
                this.showToast(`Switched "${dealer.dealer_name}" to ${targetType === "old" ? "Old Sheet" : "New Sheet"}`, "success");
            } catch (err) {
                dealer.sheet_type = current;
                this.showToast("Failed to switch sheet category: " + err.message, "error");
            }
        },
    }));
});

