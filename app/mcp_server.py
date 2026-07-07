"""HR Onboarder — MCP Server (stdio transport).

Exposes 5 HR-domain tools used by the onboarding agents:

  1. get_employee_profile       — look up employee info from HR database
  2. get_required_documents     — fetch the required doc list for a role
  3. save_onboarding_record     — persist onboarding status to HR system
  4. get_policy                 — retrieve company policy text by name
  5. send_onboarding_email      — simulate sending welcome email to new hire

Run standalone:
    uv run python -m app.mcp_server

Or let the MCPToolset in agent.py auto-spawn it via stdio.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("hr-onboarder-tools")

# ── Simulated in-memory HR database ──────────────────────────────────────────

_EMPLOYEE_DB: dict[str, dict] = {
    "E001": {
        "employee_id": "E001",
        "name": "Alice Johnson",
        "role": "Software Engineer",
        "department": "Engineering",
        "manager": "Bob Smith",
        "start_date": "2026-08-01",
        "location": "New York",
        "email": "alice.johnson@company.com",
        "status": "pending_onboarding",
    },
    "E002": {
        "employee_id": "E002",
        "name": "Carlos Rivera",
        "role": "Product Manager",
        "department": "Product",
        "manager": "Diana Lee",
        "start_date": "2026-08-15",
        "location": "San Francisco",
        "email": "carlos.rivera@company.com",
        "status": "pending_onboarding",
    },
}

_ONBOARDING_RECORDS: dict[str, dict] = {}

_POLICIES: dict[str, str] = {
    "data_privacy": (
        "All employees must complete GDPR/CCPA training within 30 days of joining. "
        "Personal data must be handled in accordance with our Data Privacy Policy v3.2."
    ),
    "security": (
        "Employees must complete security awareness training within 7 days. "
        "MFA is mandatory on all company accounts. "
        "Laptops must use full-disk encryption."
    ),
    "code_of_conduct": (
        "All employees must read and sign the Code of Conduct. "
        "Zero tolerance for harassment, discrimination, or unethical behaviour."
    ),
    "remote_work": (
        "Remote work is permitted with manager approval. "
        "Employees must use VPN when accessing company systems remotely."
    ),
    "pto": (
        "Full-time employees accrue 15 days PTO per year. "
        "Unused PTO carries over up to 5 days per calendar year."
    ),
}

_REQUIRED_DOCS: dict[str, list[str]] = {
    "default": [
        "Government-issued photo ID",
        "W-4 / Tax withholding form",
        "Bank details for direct deposit",
        "Signed offer letter",
        "NDA / Confidentiality agreement",
        "Emergency contact form",
        "I-9 Employment eligibility (US)",
    ],
    "Engineering": [
        "Government-issued photo ID",
        "W-4 / Tax withholding form",
        "Bank details for direct deposit",
        "Signed offer letter",
        "NDA / Confidentiality agreement",
        "Emergency contact form",
        "I-9 Employment eligibility (US)",
        "IP assignment agreement",
        "GitHub username for access provisioning",
    ],
    "Finance": [
        "Government-issued photo ID",
        "W-4 / Tax withholding form",
        "Bank details for direct deposit",
        "Signed offer letter",
        "NDA / Confidentiality agreement",
        "Emergency contact form",
        "I-9 Employment eligibility (US)",
        "Background check consent form",
        "CPA/CFA certification copy (if applicable)",
    ],
}


# ═══════════════════════════════════════════════════════════════════════════════
# MCP TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_employee_profile(employee_id: str) -> str:
    """Look up an employee's profile from the HR database.

    Args:
        employee_id: The employee's unique ID (e.g. 'E001').

    Returns:
        JSON string with employee profile, or an error message if not found.
    """
    profile = _EMPLOYEE_DB.get(employee_id.upper())
    if not profile:
        # Try searching by name fragment
        matches = [
            emp for emp in _EMPLOYEE_DB.values()
            if employee_id.lower() in emp["name"].lower()
        ]
        if matches:
            return json.dumps({"found": True, "profiles": matches}, indent=2)
        return json.dumps({
            "found": False,
            "error": f"No employee found with ID or name containing '{employee_id}'",
            "available_ids": list(_EMPLOYEE_DB.keys()),
        })

    return json.dumps({"found": True, "profile": profile}, indent=2)


@mcp.tool()
def get_required_documents(department: str = "default") -> str:
    """Retrieve the list of required onboarding documents for a department.

    Args:
        department: The employee's department (e.g. 'Engineering', 'Finance').
                    Falls back to default list if department not found.

    Returns:
        JSON string with list of required documents.
    """
    docs = _REQUIRED_DOCS.get(department, _REQUIRED_DOCS["default"])
    return json.dumps({
        "department": department,
        "required_documents": docs,
        "total_count": len(docs),
    }, indent=2)


@mcp.tool()
def save_onboarding_record(
    employee_id: str,
    status: str,
    hr_decision: str,
    completion_percentage: float,
    notes: str = "",
) -> str:
    """Save or update an employee's onboarding record in the HR system.

    Args:
        employee_id: The employee's unique ID.
        status: Onboarding status ('pending', 'in_progress', 'completed', 'blocked').
        hr_decision: HR manager's decision ('approved', 'rejected', 'pending_review').
        completion_percentage: Overall completion % (0–100).
        notes: Optional free-text notes from HR.

    Returns:
        JSON string confirming the record was saved.
    """
    record = {
        "employee_id": employee_id,
        "status": status,
        "hr_decision": hr_decision,
        "completion_percentage": completion_percentage,
        "notes": notes,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    _ONBOARDING_RECORDS[employee_id] = record

    # Also update employee status in DB
    if employee_id in _EMPLOYEE_DB:
        _EMPLOYEE_DB[employee_id]["status"] = status

    logger.info("Saved onboarding record for %s: %s", employee_id, status)
    return json.dumps({
        "success": True,
        "message": f"Onboarding record saved for employee {employee_id}",
        "record": record,
    }, indent=2)


@mcp.tool()
def get_policy(policy_name: str) -> str:
    """Retrieve company HR policy text by name.

    Args:
        policy_name: Policy name — one of: 'data_privacy', 'security',
                     'code_of_conduct', 'remote_work', 'pto'.

    Returns:
        JSON string with policy name and text.
    """
    text = _POLICIES.get(policy_name.lower().replace(" ", "_"))
    if not text:
        return json.dumps({
            "found": False,
            "error": f"Policy '{policy_name}' not found.",
            "available_policies": list(_POLICIES.keys()),
        })

    return json.dumps({
        "found": True,
        "policy_name": policy_name,
        "policy_text": text,
    }, indent=2)


@mcp.tool()
def send_onboarding_email(
    employee_name: str,
    employee_email: str,
    role: str,
    start_date: str,
    manager_name: str = "Your Manager",
) -> str:
    """Simulate sending a welcome onboarding email to a new hire.

    Args:
        employee_name: Full name of the new employee.
        employee_email: New employee's email address.
        role: Their job title/role.
        start_date: Their first day (YYYY-MM-DD).
        manager_name: Name of their assigned manager.

    Returns:
        JSON string confirming the email was queued.
    """
    email_body = f"""
Subject: Welcome to the Company, {employee_name}! 🎉

Dear {employee_name},

We're thrilled to welcome you as our new {role}!

Your first day is {start_date}. Please arrive/log in by 9:00 AM.
Your manager {manager_name} will greet you and guide you through your first week.

Before you start, please complete your onboarding checklist in the HR portal.

Looking forward to having you on the team!

Best regards,
HR Onboarding Team
    """.strip()

    result = {
        "success": True,
        "email_queued": True,
        "recipient": employee_email,
        "subject": f"Welcome to the Company, {employee_name}!",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "preview": email_body[:200] + "...",
    }
    logger.info("Welcome email queued for %s <%s>", employee_name, employee_email)
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT (stdio transport for MCPToolset)
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="stdio")
