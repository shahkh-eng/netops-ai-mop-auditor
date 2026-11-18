"""
agent.py
--------
Llama 3.2 Change Control Auditor Agent.

Role in Architecture:
Instead of letting an LLM guess raw CLI diffs, Python deterministically computes the
Pre/Post Comparison Matrix and Config Diffs, and Llama 3.2 evaluates:
1. Scope Compliance: Were changes strictly inside the Authorized MOP Scope?
2. Operational Impact: Is anything missing, broken, or misconfigured?
3. Final Verdict: APPROVED FOR SIGN-OFF vs REJECTED (ROLLBACK REQUIRED).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import ollama
from tools import check_bgp_table_prefix, get_napalm_bgp_summary, inspect_interface_config

MODEL = "llama3.2"

AUDIT_TOOLS = {
    "inspect_interface_config": inspect_interface_config,
    "check_bgp_table_prefix": check_bgp_table_prefix,
    "get_napalm_bgp_summary": get_napalm_bgp_summary,
}

AUDITOR_SYSTEM_PROMPT = """You are a Principal Network Change Control Auditor (AIOps).
Your job is to evaluate a completed maintenance activity by comparing the PRE-CHANGE baseline
against the POST-CHANGE state and deciding if the change is safe to sign off.

Evaluation Rules:
1. Compare 'changed_routers' and 'config_diffs' against the 'authorized_scope'. Flag any out-of-scope edits or missing expected edits.
2. Check 'regressions' and the Pre/Post comparison matrix. If ANY regression exists (dropped BGP prefix, inaccessible next-hop, MTU < 1500, disabled unreachables, failed ping, or OSPF drop), you MUST REJECT the change.
3. Optional verification: If a new prefix or interface was added (or a regression occurred), you may call inspect_interface_config or check_bgp_table_prefix to double-check live router state before writing your verdict.
4. Always conclude with a structured Post-Implementation Review (PIR) containing:
   - VERDICT: APPROVED (SIGN-OFF)  OR  REJECTED (ROLLBACK REQUIRED)
   - SCOPE COMPLIANCE: Exact summary of what changed vs. authorized scope
   - MISSING / BROKEN / MISCONFIGURED ITEMS: Explicit list (or "None")
   - RECOMMENDATION: Sign off ticket OR execute immediate rollback."""


def _save_pir_artifact(
    mop_title: str,
    authorized_scope: str,
    comparison: dict,
    verdict_text: str,
    passed: bool,
    trace: list[dict],
) -> str:
    """Persist both Markdown PIR report and structured latest_audit.json for Excel export."""
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = reports_dir / f"pir_{ts}.md"

    lines = [
        f"# Post-Implementation Review: {mop_title}",
        f"- **Timestamp (UTC):** {ts}",
        f"- **AI Auditor Model:** `{MODEL}`",
        f"- **Automated Regression Check:** {'FAILED' if comparison['has_regression'] else 'PASSED'}",
        "",
        "## Regressions Detected",
    ]
    if comparison["regressions"]:
        lines.extend(f"- {r}" for r in comparison["regressions"])
    else:
        lines.append("- None")

    lines.extend(["", "## AI Auditor Verdict", verdict_text])
    out_file.write_text("\n".join(lines), encoding="utf-8")

    # Store structured audit metadata so /api/export-excel includes full AI trace & verdict
    latest_payload = {
        "mop_title": mop_title,
        "authorized_scope": authorized_scope,
        "timestamp_utc": ts,
        "model": MODEL,
        "passed": passed,
        "verdict_text": verdict_text,
        "report_file": str(out_file),
        "trace": trace,
    }
    (reports_dir / "latest_audit.json").write_text(json.dumps(latest_payload, indent=2), encoding="utf-8")
    return str(out_file)


def run_ai_audit_stream(mop_title: str, authorized_scope: str, comparison: dict):
    """Generator streaming step-by-step Llama 3.2 audit reasoning to the Flask UI."""
    yield {"event": "audit_start", "message": "Llama 3.2 Auditor is reviewing Pre/Post diffs and telemetry..."}

    audit_payload = {
        "mop_title": mop_title,
        "authorized_scope": authorized_scope,
        "changed_routers": comparison["changed_routers"],
        "config_diffs": comparison["config_diffs"],
        "regressions_detected": comparison["regressions"],
        "comparison_matrix": comparison["matrix"],
    }

    messages = [
        {"role": "system", "content": AUDITOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Audit this completed maintenance activity using the deterministic Pre/Post report below:\n\n"
                + json.dumps(audit_payload, indent=2)
            ),
        },
    ]

    trace_records = []

    for step in range(1, 6):
        yield {
            "event": "ai_thinking",
            "step": step,
            "message": f"AI Audit Step {step}: Evaluating scope compliance & regressions...",
        }

        try:
            response = ollama.chat(
                model=MODEL,
                messages=messages,
                tools=list(AUDIT_TOOLS.values()),
                options={"temperature": 0.0},
            )
        except Exception as exc:
            yield {"event": "error", "message": f"Ollama error: {exc}"}
            return

        msg = response.message
        messages.append(msg)
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            verdict_text = msg.content
            passed = (not comparison["has_regression"]) and ("REJECTED" not in verdict_text.upper())
            report_file = _save_pir_artifact(
                mop_title, authorized_scope, comparison, verdict_text, passed, trace_records
            )
            yield {
                "event": "audit_verdict",
                "passed": passed,
                "verdict": verdict_text,
                "report_file": report_file,
            }
            return

        for call in tool_calls:
            fn_name = call.function.name
            fn_args = dict(call.function.arguments or {})
            yield {"event": "ai_tool_call", "step": step, "tool": fn_name, "args": fn_args}

            fn = AUDIT_TOOLS.get(fn_name)
            try:
                res = fn(**fn_args) if fn else json.dumps({"error": f"Unknown tool {fn_name}"})
            except Exception as err:
                res = json.dumps({"error": str(err)})

            trace_records.append({
                "step": step,
                "tool": fn_name,
                "args": json.dumps(fn_args),
                "result": str(res),
            })

            yield {"event": "ai_tool_result", "step": step, "tool": fn_name, "result": res}
            messages.append({"role": "tool", "tool_name": fn_name, "content": str(res)})