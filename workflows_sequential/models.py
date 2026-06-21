"""
Shared Pydantic data contracts for the AI Agent Readiness Auditor & Optimizer.

These are the ONLY types that should cross a node boundary in the ADK
Workflow graph. No untyped dicts between nodes — see SPEC.md §3.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

class TargetRef(BaseModel):
    """What we're auditing. Produced by the Intake Agent (§4.1)."""

    source_type: Literal["local_path", "url"]
    value: str  # absolute path or full URL
    resolved_at: str  # ISO 8601 string, e.g. datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

# The four known Lighthouse "Agentic Browsing" checks (Lighthouse 13.3+).
CHECK_IDS = (
    "llms-txt",                  # quality of an EXISTING llms.txt (Lighthouse-native)
    "llms-txt-exists",           # synthetic: does the file exist at all? (we check this ourselves)
    "webmcp-form-coverage",
    "webmcp-registered-tools",
    "webmcp-schema-validity",
    "agent-accessibility-tree",
    "cumulative-layout-shift",
)

CheckId = Literal[
    "llms-txt",
    "llms-txt-exists",
    "webmcp-form-coverage",
    "webmcp-registered-tools",
    "webmcp-schema-validity",
    "agent-accessibility-tree",
    "cumulative-layout-shift",
]


class LighthouseFinding(BaseModel):
    check_id: CheckId
    applicable: bool = True
    # When applicable=False (Lighthouse's "notApplicable" mode, e.g. WebMCP
    # checks on a page with no forms), `passed` is vacuously True — nothing
    # to fix — but `applicable=False` lets Diagnosis treat it as "not
    # relevant" rather than "good," which matters for accurate reporting.
    passed: bool
    raw_score: Optional[float] = None  # 0.0-1.0 where applicable, None for boolean/notApplicable checks
    details: str  # raw Lighthouse explanation, unmodified


class AuditResult(BaseModel):
    target: TargetRef
    run_at: str  # ISO 8601 string
    findings: list[LighthouseFinding]
    raw_json_path: str  # where the full Lighthouse JSON was saved

    def finding_for(self, check_id: CheckId) -> Optional[LighthouseFinding]:
        """Convenience lookup — used by Report node when diffing before/after."""
        return next((f for f in self.findings if f.check_id == check_id), None)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

RemediationType = Literal[
    "llms_txt",
    "aria_labels",
    "webmcp_suggestion_only",
    "not_auto_fixable",
]


class DiagnosisItem(BaseModel):
    check_id: CheckId
    severity: Literal["critical", "moderate", "info"]
    explanation: str  # why this matters for agentic browsing specifically
    remediation_type: RemediationType
    proposed_action: str  # human-readable description of the planned fix


class DiagnosisItems(BaseModel):
    """What the LLM actually produces — NOT a full DiagnosisResult.

    The LLM should never be asked to fabricate AuditResult data it wasn't
    given. We assemble the real DiagnosisResult ourselves in Python by
    combining the real AuditResult with these items.
    """
    items: list[DiagnosisItem]


class DiagnosisResult(BaseModel):
    audit: AuditResult
    items: list[DiagnosisItem]

    def item_for(self, check_id: CheckId) -> Optional[DiagnosisItem]:
        return next((i for i in self.items if i.check_id == check_id), None)


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------

class AriaLabelSuggestion(BaseModel):
    file_path: str = Field(description="Relative path of the HTML file to edit (e.g. 'index.html').")
    selector: str = Field(description="A CSS selector or description to locate the element.")
    element_snippet: str = Field(description="A unique snippet of the element HTML (like '<button class=\"menu-btn\">') to match.")
    aria_label: str = Field(description="The suggested aria-label value to insert.")

class RemediationDraft(BaseModel):
    llms_txt_content: Optional[str] = Field(default=None, description="Drafted content for llms.txt, or null if not needed.")
    aria_suggestions: list[AriaLabelSuggestion] = Field(default_factory=list, description="List of ARIA label suggestion objects.")
    webmcp_suggestion: Optional[str] = Field(default=None, description="Suggested WebMCP integration code snippet or instructions, or null if not needed.")


ActionTaken = Literal[
    "created",
    "modified",
    "skipped_already_present",
    "skipped_unsafe",
]


class RemediationAction(BaseModel):
    check_id: CheckId
    file_path: str
    action_taken: ActionTaken
    diff_summary: str


class RemediationResult(BaseModel):
    diagnosis: DiagnosisResult
    actions: list[RemediationAction]


# ---------------------------------------------------------------------------
# Benchmark / Report
# ---------------------------------------------------------------------------

Delta = Literal["fixed", "unchanged_pass", "unchanged_fail", "regressed"]


class BenchmarkComparison(BaseModel):
    check_id: CheckId
    before_passed: bool
    after_passed: bool
    delta: Delta


class FinalReport(BaseModel):
    target: TargetRef
    before: AuditResult
    after: AuditResult
    comparisons: list[BenchmarkComparison]
    summary_line: str
    report_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_delta(before_passed: bool, after_passed: bool) -> Delta:
    """Single source of truth for delta classification — used by Report node.

    Kept here (not duplicated in report.py) so the rule can't drift between
    where it's defined and where it's tested.
    """
    if before_passed and after_passed:
        return "unchanged_pass"
    if before_passed and not after_passed:
        return "regressed"
    if not before_passed and after_passed:
        return "fixed"
    return "unchanged_fail"
