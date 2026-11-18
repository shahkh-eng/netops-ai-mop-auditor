"""
app.py
------
Flask web server orchestrating the strict 4-step Maintenance Activity Workflow:
1. Select MOP Scenario
2. Capture PRE Snapshot -> Clear Engineer for Changes
3. Engineer makes changes (Simulated Clean / Simulated Mistake / Manual GNS3 CLI)
4. Manual Engineer Confirmation -> Capture POST Snapshot + Diff Matrix + SSE AI Audit
5. Export formatted 4-sheet Excel (.xlsx) Audit Report (+ Optional One-Click Rollback)
"""

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, jsonify, render_template, request, send_file
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from agent import run_ai_audit_stream
from guardrails import push_maintenance_commands
from scenarios import MOP_SCENARIOS
from tools import capture_full_snapshot, compute_pre_post_comparison, load_snapshot

app = Flask(__name__)

# Stores the active simulated change metadata so rollback knows exact inverse commands
ACTIVE_SESSION = {"last_sim": None}


@app.route("/")
def index():
    return render_template("index.html", scenarios=MOP_SCENARIOS)


@app.route("/api/pre-snapshot", methods=["POST"])
def api_pre_snapshot():
    """Step 2: Capture PRE_ACTIVITY Golden Baseline across R1, R2, R3."""
    try:
        ACTIVE_SESSION["last_sim"] = None
        snap = capture_full_snapshot("PRE_ACTIVITY")
        return jsonify({
            "ok": True,
            "timestamp": snap["timestamp"],
            "summary": {
                "r2_ospf": snap["routers"]["R2"]["ospf_neighbors"],
                "r1_bgp": snap["routers"]["R1"]["bgp_peers"],
                "r2_bgp": snap["routers"]["R2"]["bgp_peers"],
                "mtu_policy": f"MTU={snap['r2_fa2_0_mtu']}, Unreachables={snap['r2_fa2_0_unreachables']}",
                "ping_1500": snap["data_plane"]["HQ_to_DC_1500B_DF"]["detail"],
            },
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/simulate-change", methods=["POST"])
def api_simulate_change():
    """Step 3 (Optional Simulation): Apply a clean or misconfigured MOP change to GNS3."""
    data = request.json or {}
    mop_id = data.get("mop_id")
    mode = data.get("mode", "clean")  # "clean" or "broken"

    scenario = MOP_SCENARIOS.get(mop_id)
    if not scenario:
        return jsonify({"ok": False, "error": "Invalid MOP ID"}), 400

    change_spec = scenario.get("clean_change" if mode == "clean" else "broken_change")
    if not change_spec:
        return jsonify({"ok": False, "error": "This scenario is Manual CLI mode only."}), 400

    try:
        push_maintenance_commands(change_spec["router"], change_spec["commands"])
        ACTIVE_SESSION["last_sim"] = change_spec
        return jsonify({"ok": True, "summary": change_spec["summary"], "router": change_spec["router"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/post-snapshot-diff", methods=["POST"])
def api_post_snapshot_diff():
    """Step 4a: After manual engineer confirmation, capture POST Snapshot and compute Pre/Post diffs."""
    try:
        pre = load_snapshot("PRE_ACTIVITY")
        if not pre:
            return jsonify({"ok": False, "error": "PRE_ACTIVITY snapshot missing. Run Pre-Check first."}), 400

        post = capture_full_snapshot("POST_ACTIVITY")
        comparison = compute_pre_post_comparison(pre, post)
        return jsonify({"ok": True, "comparison": comparison})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/audit-stream")
def api_audit_stream():
    """Step 4b: Stream Llama 3.2 AI Auditor reasoning & final verdict via SSE."""
    mop_id = request.args.get("mop_id", "MOP-101")
    custom_scope = request.args.get("scope", "").strip()
    scenario = MOP_SCENARIOS.get(mop_id, MOP_SCENARIOS["MOP-101"])
    scope_to_use = custom_scope or scenario["authorized_scope"]

    def event_stream():
        pre = load_snapshot("PRE_ACTIVITY")
        post = load_snapshot("POST_ACTIVITY")
        if not pre or not post:
            yield f"data: {json.dumps({'event': 'error', 'message': 'Missing PRE or POST snapshot.'})}\n\n"
            return

        comparison = compute_pre_post_comparison(pre, post)
        for ev in run_ai_audit_stream(scenario["title"], scope_to_use, comparison):
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    """Optional Rollback: Reverts the last simulated change if Post-Check rejected the activity."""
    last_sim = ACTIVE_SESSION.get("last_sim")
    if not last_sim:
        return jsonify({
            "ok": False,
            "error": "No simulated change recorded in session. For manual GNS3 edits, revert directly in console.",
        }), 400

    try:
        push_maintenance_commands(last_sim["router"], last_sim["rollback"])
        ACTIVE_SESSION["last_sim"] = None
        return jsonify({"ok": True, "status": f"Rolled back {last_sim['router']} to clean pre-maintenance state."})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _auto_fit_columns(ws, max_width: int = 85):
    """Automatically resize Excel columns with sensible padding and max width."""
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        longest = 0
        for cell in col:
            val = str(cell.value or "")
            for line in val.splitlines() or [""]:
                longest = max(longest, len(line))
        ws.column_dimensions[col_letter].width = min(max(longest + 4, 14), max_width)


@app.route("/api/export-excel", methods=["GET"])
def api_export_excel():
    """
    Build and stream a formatted 4-tab Excel workbook (.xlsx) containing:
    1. Executive Summary & AI Verdict
    2. Pre vs. Post Comparison Matrix
    3. Unified Configuration Diffs
    4. AI Audit Trace
    """
    pre = load_snapshot("PRE_ACTIVITY")
    post = load_snapshot("POST_ACTIVITY")
    if not pre or not post:
        return jsonify({"ok": False, "error": "Complete Pre-Check and Post-Check before exporting."}), 400

    comparison = compute_pre_post_comparison(pre, post)
    audit_path = Path("reports/latest_audit.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}

    wb = Workbook()

    # Reusable Excel styles
    header_fill = PatternFill("solid", fgColor="0F172A")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    bold_font = Font(bold=True, color="0F172A", size=10)
    mono_font = Font(name="Consolas", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    fill_pass = PatternFill("solid", fgColor="DCFCE7")
    font_pass = Font(bold=True, color="166534")
    fill_warn = PatternFill("solid", fgColor="FEF3C7")
    font_warn = Font(bold=True, color="92400E")
    fill_fail = PatternFill("solid", fgColor="FEE2E2")
    font_fail = Font(bold=True, color="991B1B")

    # ------------------------------------------------------------------
    # Sheet 1: Executive Summary & Verdict
    # ------------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "Executive Summary"
    ws1.append(["Field", "Details"])
    for cell in ws1[1]:
        cell.fill = header_fill
        cell.font = header_font

    passed = audit.get("passed", not comparison["has_regression"])
    verdict_label = "APPROVED (SIGN-OFF)" if passed else "REJECTED (ROLLBACK REQUIRED)"
    regressions_str = "\n".join(comparison["regressions"]) if comparison["regressions"] else "None"

    summary_rows = [
        ("MOP Scenario", audit.get("mop_title", "Maintenance Activity Audit")),
        ("Audit Timestamp (UTC)", audit.get("timestamp_utc", datetime.now(timezone.utc).isoformat())),
        ("AI Auditor Model", audit.get("model", "llama3.2")),
        ("Authorized Scope", audit.get("authorized_scope", "N/A")),
        ("Routers Modified", ", ".join(comparison["changed_routers"]) or "None"),
        ("Regressions Detected", regressions_str),
        ("Final Verdict", verdict_label),
        ("AI Auditor Narrative", audit.get("verdict_text", "AI Audit not yet completed.")),
    ]

    for idx, (k, v) in enumerate(summary_rows, start=2):
        ws1.append([k, v])
        ws1.cell(idx, 1).font = bold_font
        ws1.cell(idx, 1).border = thin_border
        val_cell = ws1.cell(idx, 2)
        val_cell.border = thin_border
        val_cell.alignment = Alignment(wrap_text=True, vertical="top")
        if k == "Final Verdict":
            val_cell.fill = fill_pass if passed else fill_fail
            val_cell.font = font_pass if passed else font_fail

    ws1.freeze_panes = "A2"
    ws1.column_dimensions["A"].width = 26
    ws1.column_dimensions["B"].width = 92

    # ------------------------------------------------------------------
    # Sheet 2: Pre-Post Comparison Matrix
    # ------------------------------------------------------------------
    ws2 = wb.create_sheet("Pre-Post Matrix")
    ws2.append(["Category", "Check Item", "PRE Baseline", "POST State", "Status"])
    for cell in ws2[1]:
        cell.fill = header_fill
        cell.font = header_font

    for idx, row in enumerate(comparison["matrix"], start=2):
        ws2.append([row["category"], row["check"], row["pre"], row["post"], row["status"]])
        for c in range(1, 6):
            ws2.cell(idx, c).border = thin_border
            ws2.cell(idx, c).alignment = Alignment(vertical="top", wrap_text=True)

        st_cell = ws2.cell(idx, 5)
        st_val = row["status"]
        if st_val == "PASS":
            st_cell.fill, st_cell.font = fill_pass, font_pass
        elif st_val in ("CHANGED", "IMPROVED"):
            st_cell.fill, st_cell.font = fill_warn, font_warn
        else:
            st_cell.fill, st_cell.font = fill_fail, font_fail

    ws2.freeze_panes = "A2"
    _auto_fit_columns(ws2)

    # ------------------------------------------------------------------
    # Sheet 3: Configuration Diffs
    # ------------------------------------------------------------------
    ws3 = wb.create_sheet("Config Diffs")
    ws3.append(["Router", "Change Status", "Line Type", "Configuration Line"])
    for cell in ws3[1]:
        cell.fill = header_fill
        cell.font = header_font

    row_idx = 2
    for r in ["R1", "R2", "R3"]:
        diff_text = comparison["config_diffs"].get(r, "")
        if not diff_text:
            ws3.append([r, "Unchanged", "NONE", "(No configuration changes detected)"])
            for c in range(1, 5):
                ws3.cell(row_idx, c).border = thin_border
            row_idx += 1
            continue

        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                ltype, lfill, lfont = "ADDED", fill_pass, font_pass
            elif line.startswith("-") and not line.startswith("---"):
                ltype, lfill, lfont = "REMOVED", fill_fail, font_fail
            else:
                ltype, lfill, lfont = "CONTEXT", None, mono_font

            ws3.append([r, "Modified", ltype, line])
            for c in range(1, 5):
                ws3.cell(row_idx, c).border = thin_border
            ws3.cell(row_idx, 4).font = mono_font
            if lfill:
                ws3.cell(row_idx, 3).fill, ws3.cell(row_idx, 3).font = lfill, lfont
                ws3.cell(row_idx, 4).fill = lfill
            row_idx += 1

    ws3.freeze_panes = "A2"
    _auto_fit_columns(ws3, max_width=95)

    # ------------------------------------------------------------------
    # Sheet 4: AI Audit Trace
    # ------------------------------------------------------------------
    ws4 = wb.create_sheet("AI Audit Trace")
    ws4.append(["Step", "Tool Called", "Arguments", "Observation / Result"])
    for cell in ws4[1]:
        cell.fill = header_fill
        cell.font = header_font

    trace_rows = audit.get("trace", [])
    if not trace_rows:
        ws4.append([
            1,
            "Direct Deterministic Audit",
            "{}",
            "Llama 3.2 evaluated the Pre/Post Comparison Matrix and Config Diffs directly without needing extra live CLI queries.",
        ])
    else:
        for idx, t in enumerate(trace_rows, start=2):
            ws4.append([t.get("step"), t.get("tool"), t.get("args"), t.get("result")])
            for c in range(1, 5):
                ws4.cell(idx, c).border = thin_border
                ws4.cell(idx, c).alignment = Alignment(wrap_text=True, vertical="top")

    ws4.freeze_panes = "A2"
    _auto_fit_columns(ws4, max_width=85)

    # Stream workbook directly from memory
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    ts_name = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"MOP_Audit_Report_{ts_name}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)