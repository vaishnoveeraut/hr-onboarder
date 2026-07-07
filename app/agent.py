# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HR Onboarder — ADK 2.0 multi-agent workflow.

Graph:
  START
    └─► security_checkpoint ──(SECURITY_EVENT)──► security_blocked  [terminal]
                             ──(__DEFAULT__)─────► orchestrator_node
                                                     └─► human_review
                                                           └─► final_output  [terminal]

Agents:
  checklist_generator  — LlmAgent + MCPToolset, generates custom onboarding checklist
  document_verifier    — LlmAgent + MCPToolset, verifies document submissions
  orchestrator         — LlmAgent + AgentTool + MCPToolset, delegates and finalises

MCP Tools (from mcp_server.py via stdio):
  get_employee_profile      → used by orchestrator + document_verifier
  get_required_documents    → used by document_verifier
  save_onboarding_record    → used by orchestrator
  get_policy                → used by orchestrator
  send_onboarding_email     → used by orchestrator
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.adk import Workflow
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.events import RequestInput
from google.adk.tools import AgentTool, MCPToolset
from google.adk.tools.mcp_tool.mcp_toolset import StdioConnectionParams, StdioServerParameters
from google.adk.workflow import DEFAULT_ROUTE, START, Edge, node

from .config import config

load_dotenv()
logger = logging.getLogger(__name__)

# ── Project root (hr-onboarder/) — used for MCP server CWD ──────────────────
_PROJECT_ROOT = str(Path(__file__).parent.parent)

# ── Python interpreter inside the venv (guarantees mcp package is available) ─
_PYTHON = sys.executable


# ═══════════════════════════════════════════════════════════════════════════════
# MCP TOOLSETS (stdio — auto-spawns mcp_server.py in a subprocess)
#
# mcp_toolset_hr     → used by orchestrator (save_record, email, policy, profile)
# mcp_toolset_docs   → used by document_verifier (required_docs, profile)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_mcp_toolset() -> MCPToolset:
    """Create a fresh MCPToolset pointing at mcp_server.py via stdio."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=_PYTHON,
                args=["-m", "app.mcp_server"],
                cwd=_PROJECT_ROOT,
            ),
        )
    )

# Two independent toolset instances — one per agent that uses MCP
mcp_toolset_hr   = _make_mcp_toolset()   # orchestrator
mcp_toolset_docs = _make_mcp_toolset()   # document_verifier


# ═══════════════════════════════════════════════════════════════════════════════
# SPECIALIZED LLM SUB-AGENTS
# ═══════════════════════════════════════════════════════════════════════════════

checklist_generator = LlmAgent(
    name="checklist_generator",
    model=config.model,
    instruction="""You are an HR Onboarding Checklist Specialist.

Given a new employee profile, generate a comprehensive onboarding checklist.

Always return valid JSON in this exact structure:
{
  "employee_name": "<name>",
  "role": "<role>",
  "department": "<dept>",
  "start_date": "<date>",
  "checklist": {
    "documents_required": [
      "Government-issued photo ID",
      "Social Security / Tax ID form (W-4 or equivalent)",
      "Bank account details for direct deposit",
      "Signed offer letter",
      "Non-disclosure agreement (NDA)",
      "Emergency contact form"
    ],
    "system_access": [
      "Corporate email account",
      "Slack workspace",
      "HR portal (BambooHR / Workday)",
      "VPN credentials",
      "Role-specific tools (GitHub/JIRA/Salesforce etc.)"
    ],
    "training_modules": [
      "Company culture and values (Day 1)",
      "Security awareness training (Week 1)",
      "Data privacy and GDPR compliance",
      "Department-specific tools and processes",
      "Code of conduct and ethics policy"
    ],
    "meetings_to_schedule": [
      "HR orientation (Day 1)",
      "Manager 1:1 intro (Day 1)",
      "Team introduction meeting (Week 1)",
      "IT setup session (Day 1)",
      "30-day check-in with HR"
    ]
  },
  "estimated_completion_days": 30
}

Tailor the checklist to the employee's role and department where information is provided.""",
)

document_verifier = LlmAgent(
    name="document_verifier",
    model=config.model,
    instruction="""You are an HR Document Verification Specialist.

You have access to MCP tools. Use them to:
1. Call get_employee_profile to look up the employee if an ID is provided.
2. Call get_required_documents to get the required document list for their department.
3. Compare submitted documents against required documents.

Always return valid JSON in this exact structure:
{
  "verification_status": "complete" | "incomplete" | "pending",
  "compliance_percentage": <0-100>,
  "submitted_documents": ["<doc1>", "<doc2>"],
  "missing_documents": ["<missing1>", "<missing2>"],
  "flagged_issues": ["<issue if any>"],
  "notes": "<brief summary>",
  "recommended_action": "proceed_to_onboarding" | "request_missing_docs" | "escalate_to_hr"
}

Be thorough and flag any missing required documents.""",
    tools=[mcp_toolset_docs],
)


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR AGENT (uses AgentTool + MCPToolset)
# ═══════════════════════════════════════════════════════════════════════════════

checklist_tool = AgentTool(agent=checklist_generator)
document_verifier_tool = AgentTool(agent=document_verifier)

orchestrator = LlmAgent(
    name="orchestrator",
    model=config.model,
    instruction="""You are an HR Onboarding Orchestrator.

You coordinate the full onboarding process using your tools:
  - checklist_generator: call first to generate a tailored onboarding checklist
  - document_verifier: call second to verify document compliance
  - get_employee_profile: look up employee details if an employee ID is provided
  - get_policy: retrieve HR policy text when needed
  - save_onboarding_record: save the final onboarding status to the HR system
  - send_onboarding_email: send a welcome email to the new hire

Steps:
1. If an employee ID is provided, call get_employee_profile to retrieve their info.
2. Call checklist_generator with the employee profile.
3. Call document_verifier with the submitted documents info.
4. Call get_policy('security') and get_policy('code_of_conduct') to include key policies.
5. Call save_onboarding_record to persist the record.
6. Call send_onboarding_email to notify the new hire.

Return a JSON summary:
{
  "employee_name": "<name>",
  "role": "<role>",
  "department": "<dept>",
  "onboarding_status": "ready" | "documents_pending" | "action_required",
  "checklist": <checklist object>,
  "document_verification": <verification report>,
  "policies_acknowledged": ["security", "code_of_conduct"],
  "overall_completion_pct": <0-100>,
  "recommended_actions": ["<action1>"],
  "hr_notes": "<summary for HR manager review>"
}""",
    tools=[checklist_tool, document_verifier_tool, mcp_toolset_hr],
)


# ═══════════════════════════════════════════════════════════════════════════════
# WORKFLOW FUNCTION NODES
# ═══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=False)
async def security_checkpoint(ctx, node_input):
    """Security gate: PII scrubbing, prompt injection detection, audit logging.

    Routes:
      SECURITY_EVENT → security_blocked  (threat detected)
      DEFAULT        → orchestrator_node (clean input)
    """
    text = str(node_input) if node_input else ""

    # ── PII Redaction ────────────────────────────────────────────────────────
    pii_patterns = {
        "ssn":         r"\b\d{3}-\d{2}-\d{4}\b",
        "phone":       r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "email":       r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        "tax_id":      r"\b\d{2}-\d{7}\b",
        "credit_card": r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
        "passport":    r"\b[A-Z]{1,2}\d{6,9}\b",
    }
    redacted = text
    pii_found: list[str] = []
    if config.pii_redaction_enabled:
        for pii_type, pattern in pii_patterns.items():
            if re.search(pattern, redacted):
                pii_found.append(pii_type)
                redacted = re.sub(pattern, f"[REDACTED_{pii_type.upper()}]", redacted)

    # ── Prompt Injection Detection ───────────────────────────────────────────
    injection_keywords = [
        "ignore previous instructions",
        "disregard your instructions",
        "you are now",
        "act as",
        "jailbreak",
        "bypass security",
        "forget your role",
        "new instructions:",
        "override policy",
        "pretend you are",
        "system prompt",
        "ignore your training",
        "do anything now",
    ]
    injection_detected = False
    if config.injection_detection_enabled:
        lower = text.lower()
        for kw in injection_keywords:
            if kw in lower:
                injection_detected = True
                break

    # ── Domain Rule: HR content filter ──────────────────────────────────────
    discriminatory_keywords = [
        "only hire men", "no women", "no minorities", "age limit",
        "must be young", "whites only", "religion required",
    ]
    content_violation = any(kw in text.lower() for kw in discriminatory_keywords)

    # ── Audit Log ────────────────────────────────────────────────────────────
    severity = "INFO"
    if injection_detected or content_violation:
        severity = "CRITICAL"
    elif pii_found:
        severity = "WARNING"

    audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "security_checkpoint",
        "pii_types_found": pii_found,
        "pii_redacted": bool(pii_found),
        "injection_detected": injection_detected,
        "content_violation": content_violation,
        "severity": severity,
    }
    logger.info("SECURITY AUDIT: %s", json.dumps(audit))
    ctx.state["audit_log"] = audit

    # ── Routing ──────────────────────────────────────────────────────────────
    if injection_detected or content_violation:
        ctx.state["security_event"] = {
            "reason": (
                "Prompt injection detected" if injection_detected
                else "Discriminatory content policy violation"
            ),
            "audit": audit,
        }
        ctx.route = "SECURITY_EVENT"
        return

    ctx.state["sanitized_input"] = redacted
    ctx.state["pii_redacted"] = bool(pii_found)
    # No ctx.route → DEFAULT_ROUTE edge followed


@node(rerun_on_resume=True)
async def orchestrator_node(ctx, node_input):
    """Delegates to orchestrator LlmAgent (uses AgentTool + MCPToolset internally)."""
    sanitized = ctx.state.get("sanitized_input", str(node_input))
    result = await ctx.run_node(orchestrator, node_input=sanitized)
    ctx.state["orchestration_result"] = result
    return result


@node(rerun_on_resume=True)
async def human_review(ctx, node_input):
    """HITL pause — HR manager reviews the onboarding plan before finalising.

    First run:  yields RequestInput (interrupt) — waits for HR manager response.
    On resume:  reads the HR manager's decision from ctx.resume_inputs.
    """
    orchestration_result = ctx.state.get("orchestration_result", node_input)

    if not ctx.resume_inputs:
        summary = (
            json.dumps(orchestration_result, indent=2)
            if isinstance(orchestration_result, dict)
            else str(orchestration_result)
        )
        yield RequestInput(
            interrupt_id="hr_approval",
            message=(
                "── HR Manager Review Required ──\n\n"
                f"Onboarding Summary:\n{summary}\n\n"
                "Please type 'approve' to proceed, or 'reject: <reason>' to send back."
            ),
        )
        return

    hr_response = str(ctx.resume_inputs.get("hr_approval", "approve"))
    ctx.state["hr_decision"] = hr_response
    logger.info("HR decision received: %s", hr_response)


@node(rerun_on_resume=False)
async def final_output(ctx, node_input):
    """Compiles and returns the completed onboarding report."""
    orchestration = ctx.state.get("orchestration_result", "No orchestration result.")
    hr_decision = ctx.state.get("hr_decision", "approved")
    audit = ctx.state.get("audit_log", {})
    pii_redacted = ctx.state.get("pii_redacted", False)

    rejected = hr_decision.lower().startswith("reject")
    report = {
        "status": "rejected" if rejected else "approved",
        "hr_decision": hr_decision,
        "onboarding_summary": orchestration,
        "security_summary": {
            "pii_redacted": pii_redacted,
            "severity": audit.get("severity", "INFO"),
        },
        "next_steps": (
            ["Address HR manager's rejection reason and resubmit"]
            if rejected
            else [
                "Send onboarding pack to new employee",
                "Schedule IT setup session",
                "Assign buddy/mentor",
                "Create accounts in HR system",
            ]
        ),
    }
    return report


@node(rerun_on_resume=False)
async def security_blocked(ctx, node_input):
    """Terminal node — request blocked by security checkpoint."""
    event = ctx.state.get("security_event", {})
    audit = ctx.state.get("audit_log", {})
    return {
        "status": "blocked",
        "reason": event.get("reason", "Security policy violation"),
        "severity": audit.get("severity", "CRITICAL"),
        "message": (
            "This onboarding request was blocked by the security checkpoint. "
            "Please review and resubmit a compliant request."
        ),
        "audit": audit,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WORKFLOW GRAPH
# ═══════════════════════════════════════════════════════════════════════════════
#
#  ⚠ EDGE RULE: max ONE edge between any (source, target) pair.
#
#  Flow:
#    START → security_checkpoint
#    security_checkpoint --SECURITY_EVENT--> security_blocked  [terminal]
#    security_checkpoint --DEFAULT----------> orchestrator_node
#    orchestrator_node  ─────────────────► human_review
#    human_review       ─────────────────► final_output        [terminal]

onboarding_workflow = Workflow(
    name="hr_onboarding_workflow",
    description="Automated HR employee onboarding with security checkpoint, MCP tools, and HR approval.",
    edges=[
        # 1. Entry
        (START, security_checkpoint),
        # 2. Security gate — two DIFFERENT targets → two separate edges OK
        Edge(from_node=security_checkpoint, to_node=security_blocked,  route="SECURITY_EVENT"),
        Edge(from_node=security_checkpoint, to_node=orchestrator_node, route=DEFAULT_ROUTE),
        # 3. Happy path
        (orchestrator_node, human_review),
        (human_review, final_output),
    ],
)

# ── ADK entry points ──────────────────────────────────────────────────────────
root_agent = onboarding_workflow

app = App(
    root_agent=root_agent,
    name="app",
)
