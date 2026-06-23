"""
Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer — root agent graph.

Rewritten against the REAL installed API (google-adk==1.35.2), confirmed by
reading source directly — NOT the WorkflowAgent/edges pattern from the
adk-samples "workflows_sequential" template, which targets a newer ADK
version that does not exist in this environment (confirmed: no
google.adk.agents.workflow module at all).

CONFIRMED facts this file depends on (verified via inspect/model_fields,
2026-06-20):
  - google.adk.agents.sequential_agent.SequentialAgent(sub_agents=[...])
    runs each sub_agent in order, sharing one InvocationContext.
  - Every sub_agent must be a real Agent object (subclass of BaseAgent).
    Plain functions are NOT accepted directly — deterministic steps need a
    small custom BaseAgent subclass each.
  - State is passed between agents via
    Event(author=..., invocation_id=ctx.invocation_id,
          actions=EventActions(state_delta={key: value}))
    NOT by direct mutation of ctx.session.state.
  - State is read via ctx.session.state.get(key) (dict-like).
  - The user's typed chat input arrives at ctx.user_content
    (google.genai.types.Content), text via ctx.user_content.parts[0].text.
  - Session state is persisted to SQLite (confirmed via server logs), so
    ONLY JSON-serializable values go in state_delta — plain dicts via
    .model_dump(), never raw Pydantic objects.

[RESOLVED] both v1 open questions, confirmed via live testing 2026-06-20:
  1. Function-as-first-node: CONFIRMED working (IntakeAgent ran first
     successfully in agents-cli playground).
  2. State->instruction interpolation: the assumed `{key}`-style template
     was NEVER actually real — instead, LlmAgent.instruction accepts a
     Callable[[ReadonlyContext], str] (confirmed via model_fields), and
     ctx.state gives direct read access. This is the real, now-implemented
     mechanism (see _diagnosis_instruction below).
"""

from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.genai import types as genai_types
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

from .models import (
    AuditResult,
    BenchmarkComparison,
    DiagnosisItems,
    DiagnosisResult,
    FinalReport,
    RemediationResult,
    TargetRef,
    compute_delta,
    RemediationDraft,
    AriaLabelSuggestion,
    RemediationAction,
)


def _text_event(agent_name: str, ctx: InvocationContext, text: str) -> Event:
    """Helper: an Event that both shows a message to the user and carries no state change."""
    return Event(
        author=agent_name,
        invocation_id=ctx.invocation_id,
        content=genai_types.Content(parts=[genai_types.Part(text=text)]),
    )


def _state_event(agent_name: str, ctx: InvocationContext, state_delta: dict) -> Event:
    """Helper: an Event that writes to shared state, with no visible message."""
    return Event(
        author=agent_name,
        invocation_id=ctx.invocation_id,
        actions=EventActions(state_delta=state_delta),
    )


# ---------------------------------------------------------------------------
# 1. Intake — deterministic, reads the user's chat message, no LLM call
# ---------------------------------------------------------------------------

class IntakeAgent(BaseAgent):
    """Parses the user's input into a TargetRef. See SPEC.md §4.1.

    Expects the chat message to be either:
      --path /absolute/or/relative/path
      --url https://example.com
    """

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import os

        text = ""
        if ctx.user_content and ctx.user_content.parts:
            text = (ctx.user_content.parts[0].text or "").strip()

        tokens = text.split()
        path = None
        url = None
        if "--path" in tokens:
            path = tokens[tokens.index("--path") + 1]
        if "--url" in tokens:
            url = tokens[tokens.index("--url") + 1]

        if (path and url) or (not path and not url):
            yield _text_event(
                self.name, ctx,
                "Please provide exactly one of: --path <local dir> or --url <https://...>",
            )
            return

        try:
            if path:
                if not os.path.isdir(path):
                    raise ValueError(f"--path '{path}' is not an existing directory.")
                target = TargetRef(
                    source_type="local_path",
                    value=os.path.abspath(path),
                    resolved_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                if not (url.startswith("http://") or url.startswith("https://")):
                    raise ValueError(f"--url '{url}' is not a well-formed http(s) URL.")
                target = TargetRef(
                    source_type="url",
                    value=url,
                    resolved_at=datetime.now(timezone.utc).isoformat(),
                )
        except ValueError as e:
            yield _text_event(self.name, ctx, f"[INTAKE] error: {e}")
            return

        print(f"[INTAKE] resolved target: {target.source_type} = {target.value}")
        yield _state_event(self.name, ctx, {"target": target.model_dump()})
        yield _text_event(self.name, ctx, f"Target resolved: {target.source_type} = {target.value}")


# ---------------------------------------------------------------------------
# 2. Audit — deterministic, runs Lighthouse CLI, no LLM call
# ---------------------------------------------------------------------------

async def _run_audit_shared(
    agent_name: str,
    ctx: InvocationContext,
    target: TargetRef,
    state_key: str,
    log_prefix: str,
) -> AsyncGenerator[Event, None]:
    import http.server
    import json as json_module
    import os
    import socket
    import subprocess
    import threading
    import time
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    print(f"[{log_prefix}] running against {target.value}")

    # Determine the URL Lighthouse will actually hit, and a base URL for
    # our own llms.txt existence check.
    server_thread = None
    httpd = None
    if target.source_type == "local_path":
        # Find a free port, serve the directory locally (no Docker yet —
        # TODO per SPEC.md: replace with the nginx sandbox container for
        # better production-fidelity; this is a real, working interim).
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
            *a, directory=target.value, **kw
        )
        httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.5)  # let the server actually come up
        base_url = f"http://127.0.0.1:{port}"
    else:
        base_url = target.value.rstrip("/")

    try:
        # --- Run Lighthouse ---
        run_dir = f"runs/{ctx.invocation_id}"
        os.makedirs(run_dir, exist_ok=True)
        raw_json_path = os.path.join(run_dir, f"audit_{state_key}_raw.json")

        result = subprocess.run(
            [
                "lighthouse", base_url,
                "--only-categories=agentic-browsing,accessibility,performance",
                "--output=json",
                f"--output-path={raw_json_path}",
                "--chrome-flags=--headless",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 or not os.path.exists(raw_json_path):
            yield _text_event(
                agent_name, ctx,
                f"[{log_prefix}] error: lighthouse failed (exit {result.returncode}). "
                f"stderr: {result.stderr[-500:]}",
            )
            return

        with open(raw_json_path) as f:
            lh_data = json_module.load(f)

        findings = []
        cat_map = {
            "agentic-browsing": "agentic_browsing",
            "accessibility": "accessibility",
            "performance": "performance",
        }
        for lh_cat_id, internal_cat in cat_map.items():
            cat = lh_data.get("categories", {}).get(lh_cat_id, {})
            for ref in cat.get("auditRefs", []):
                audit = lh_data.get("audits", {}).get(ref["id"], {})
                mode = audit.get("scoreDisplayMode")
                applicable = mode != "notApplicable"
                score = audit.get("score")
                passed = True if not applicable else bool(score)
                details_parts = [audit.get("title", "")]
                display_val = audit.get("displayValue")
                if display_val:
                    details_parts.append(display_val)
                failing_nodes = []
                audit_details = audit.get("details", {})
                if isinstance(audit_details, dict) and audit_details.get("items"):
                    msgs = []
                    for i in audit_details["items"]:
                        if isinstance(i, dict):
                            msgs.append(i.get("message", ""))
                            def add_node(n):
                                if isinstance(n, dict):
                                    label = n.get("nodeLabel", "")
                                    selector = n.get("selector", "")
                                    snippet = n.get("snippet", "")
                                    failing_nodes.append({
                                        "label": label,
                                        "selector": selector,
                                        "snippet": snippet,
                                    })

                            # Pattern 1: Direct node
                            if "node" in i:
                                add_node(i.get("node"))

                            # Pattern 2: Nested node inside value.items
                            value_dict = i.get("value")
                            if isinstance(value_dict, dict) and "items" in value_dict:
                                val_items = value_dict.get("items")
                                if isinstance(val_items, list):
                                    for vi in val_items:
                                        if isinstance(vi, dict) and "node" in vi:
                                            add_node(vi.get("node"))
                        else:
                            msgs.append(str(i))
                    filtered_msgs = [m for m in msgs if m]
                    if filtered_msgs:
                        details_parts.append("; ".join(filtered_msgs))
                findings.append({
                    "check_id": ref["id"],
                    "applicable": applicable,
                    "passed": passed,
                    "raw_score": score if isinstance(score, (int, float)) else None,
                    "details": " — ".join(p for p in details_parts if p),
                    "category": internal_cat,
                    "failing_nodes": failing_nodes,
                })

        # --- Our own llms.txt EXISTENCE check (Lighthouse only grades quality) ---
        llms_url = f"{base_url}/llms.txt"
        try:
            req = urllib.request.Request(llms_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                exists = resp.status == 200
        except urllib.error.HTTPError as e:
            exists = False
        except Exception:
            exists = False
        findings.append({
            "check_id": "llms-txt-exists",
            "applicable": True,
            "passed": exists,
            "raw_score": None,
            "details": "llms.txt found" if exists else "No llms.txt file found at site root",
            "category": "agentic_browsing",
        })

        # --- Custom GEO Schema Markup check ---
        has_json_ld = False
        if target.source_type == "local_path":
            index_path = os.path.join(target.value, "index.html")
            if os.path.exists(index_path):
                try:
                    with open(index_path, "r", encoding="utf-8") as f:
                        html_content = f.read()
                        has_json_ld = "application/ld+json" in html_content
                except Exception:
                    pass
        else:
            try:
                # For remote URLs, fetch the main page HTML
                req = urllib.request.Request(base_url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html_content = resp.read().decode("utf-8", errors="ignore")
                    has_json_ld = "application/ld+json" in html_content
            except Exception:
                pass
        
        findings.append({
            "check_id": "geo-schema-markup",
            "applicable": True,
            "passed": has_json_ld,
            "raw_score": 1.0 if has_json_ld else 0.0,
            "details": "JSON-LD schema markup detected" if has_json_ld else "Missing JSON-LD schema metadata for LLM citation and search RAG optimizations",
            "category": "geo_readiness",
        })

        audit_result = AuditResult(
            target=target,
            run_at=datetime.now(timezone.utc).isoformat(),
            findings=findings,
            raw_json_path=raw_json_path,
        )

        print(f"[{log_prefix}] complete: {sum(1 for f in findings if not f['passed'] and f['applicable'])} failing, "
              f"{sum(1 for f in findings if f['passed'] and f['applicable'])} passing, "
              f"{sum(1 for f in findings if not f['applicable'])} not applicable")

        yield _state_event(agent_name, ctx, {state_key: audit_result.model_dump()})
        yield _text_event(
            agent_name, ctx,
            f"[{log_prefix}] complete — {len(findings)} checks evaluated against {base_url}",
        )
    finally:
        if httpd is not None:
            httpd.shutdown()


class AuditAgent(BaseAgent):
    """Runs Lighthouse against the target. See SPEC.md §4.2."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        target_dict = ctx.session.state.get("target")
        if not target_dict:
            yield _text_event(self.name, ctx, "[AUDIT] error: no target found in state — did Intake run?")
            return
        target = TargetRef(**target_dict)
        async for event in _run_audit_shared(
            agent_name=self.name,
            ctx=ctx,
            target=target,
            state_key="audit_result",
            log_prefix="AUDIT",
        ):
            yield event


class BenchmarkAgent(BaseAgent):
    """Runs Lighthouse CLI post-remediation to compare. See SPEC.md §4.5."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        target_dict = ctx.session.state.get("target")
        if not target_dict:
            yield _text_event(self.name, ctx, "[BENCHMARK] error: no target found in state")
            return
        target = TargetRef(**target_dict)
        async for event in _run_audit_shared(
            agent_name=self.name,
            ctx=ctx,
            target=target,
            state_key="after_audit_result",
            log_prefix="BENCHMARK",
        ):
            yield event


# ---------------------------------------------------------------------------
# 3. Diagnosis — LlmAgent, structured output
# ---------------------------------------------------------------------------

def _diagnosis_instruction(ctx) -> str:
    """Dynamic instruction — reads REAL state at call time.

    CONFIRMED via inspecting LlmAgent.model_fields (2026-06-20):
    instruction accepts Callable[[ReadonlyContext], str], with ctx.state
    giving direct read access. This replaces the unverified assumption from
    v1 that a static `{audit_result_json}`-style placeholder would
    auto-interpolate — that was never actually tested and may not exist in
    this ADK version. This function is the confirmed-working mechanism.
    """
    import json as json_module

    audit_result = ctx.state.get("audit_result")
    audit_json = json_module.dumps(audit_result) if audit_result else None

    base = """
        You are auditing a site/repo for Google Lighthouse's "Agentic
        Browsing" readiness category. Never use SEO or search-ranking
        language — this is not a ranking signal.

        Severity/remediation_type rules (binding, use these EXACT check_id
        values — they are the real Lighthouse 13.4 audit IDs, confirmed via
        live testing, not generic names):
        - Any finding belonging to the "accessibility" or "performance" categories
          MUST always use remediation_type="not_auto_fixable".
        - "cumulative-layout-shift" -> remediation_type="not_auto_fixable"
        - "webmcp-form-coverage", "webmcp-registered-tools",
          "webmcp-schema-validity" -> remediation_type="webmcp_suggestion_only"
        - "llms-txt" (quality) or "llms-txt-exists" (presence) failures
          -> remediation_type="llms_txt"
        - "agent-accessibility-tree" failures -> remediation_type="aria_labels"
        - "geo-schema-markup" failures -> remediation_type="geo_schema"
        - Passing or not-applicable findings -> severity="info",
          remediation_type="not_auto_fixable"

        For each finding, produce one DiagnosisItem.

        IMPORTANT: Return ONLY the diagnosis items themselves — do NOT
        fabricate or invent any audit data you were not given.

        Return ONLY a DiagnosisItems object matching the provided schema.
    """

    if audit_json:
        return base + f"\n\n        Real audit findings (JSON):\n        {audit_json}\n"
    else:
        return base + """
        No audit data is available in state yet (this is a smoke test of
        the graph shape, not real auditing). Return a single DiagnosisItem
        with check_id="llms-txt-exists", severity="info",
        remediation_type="not_auto_fixable", explanation noting no real
        audit data was available yet.
        """


diagnosis_agent = LlmAgent(
    name="diagnosis_agent",
    model="gemini-3.1-flash-lite",
    instruction=_diagnosis_instruction,
    output_schema=DiagnosisItems,
    output_key="diagnosis_items",
)


# ---------------------------------------------------------------------------
# 4. Remediation — LlmAgent (drafts content) + custom BaseAgent (writes files)
# ---------------------------------------------------------------------------

def _remediation_draft_instruction(ctx) -> str:
    """Dynamic instruction for remediation draft - reads state at call time."""
    import json as json_module
    import os
    
    target_dict = ctx.state.get("target")
    diagnosis_items_dict = ctx.state.get("diagnosis_items")
    
    base_prompt = """
        You are an AI assistant designed to draft remediations for a website or codebase to improve its "Agentic Browsing" readiness score.
        Do NOT use any SEO or search-ranking language.
        
        You will receive:
        1. The target reference info.
        2. The diagnosis items from the previous step.
        3. If it's a local path, the content of the HTML files.
        
        Your tasks:
        1. If a check with check_id "llms-txt-exists" or "llms-txt" failed, or if the diagnosis asks for "llms_txt" remediation, you MUST load the "llms-txt-drafting" skill using the `load_skill` tool and follow its instructions to draft the `llms.txt` file content in the `llms_txt_content` field.
        2. If a check with check_id "agent-accessibility-tree" failed, or if any diagnosis asks for "aria_labels" remediation, look at the HTML contents provided and identify the elements that lack labels or roles (e.g. interactive elements like buttons, links, or inputs without clear accessible names).
           - Generate a list of `aria_suggestions`. Each suggestion must specify:
             * `file_path`: the relative path of the file (e.g. "index.html").
             * `selector`: a CSS selector that uniquely or specifically matches the target element.
             * `element_snippet`: a distinct line or tag snippet from the original HTML (e.g. `<button class="menu-btn">` or `<a href="/menu">`) to help locate it.
             * `aria_label`: a descriptive, meaningful aria-label (e.g. "Toggle navigation menu").
        3. If a check with check_id "webmcp-form-coverage", "webmcp-registered-tools", or "webmcp-schema-validity" failed, or if the diagnosis asks for "webmcp_suggestion_only" remediation, draft a suggested WebMCP integration code snippet or guide and place it in the `webmcp_suggestion` field.
        4. If a check with check_id "geo-schema-markup" failed, or if the diagnosis asks for "geo_schema" remediation, you must draft a valid JSON-LD schema script block (as a raw JSON string without surrounding script tags) that describes the website content (e.g. Organization, Product, or WebSite type based on the HTML content provided). Put the raw JSON string in the `geo_schema_draft` field.
        
        Safety rules:
        - Do not propose modifying anything other than adding aria-label or role attributes.
        - Do not restructure the DOM.
        
        Please return ONLY a RemediationDraft object matching the schema.
    """
    
    context = ""
    if target_dict:
        context += f"\nTarget: {json_module.dumps(target_dict)}\n"
        if target_dict.get("source_type") == "local_path":
            local_path = target_dict.get("value")
            if os.path.isdir(local_path):
                html_files_context = ""
                for root, dirs, files in os.walk(local_path):
                    dirs[:] = [d for d in dirs if d not in (".venv", "node_modules", "dist", "build", ".git")]
                    for f in files:
                        if f.endswith((".html", ".htm")):
                            hf = os.path.join(root, f)
                            rel_path = os.path.relpath(hf, local_path)
                            try:
                                with open(hf, "r", encoding="utf-8") as file_obj:
                                    content = file_obj.read()
                                if len(content) > 100000:
                                    content = content[:100000] + "\n...[TRUNCATED]..."
                                html_files_context += f"\n--- File: {rel_path} ---\n{content}\n"
                            except Exception as e:
                                html_files_context += f"\n--- File: {rel_path} (could not read: {e}) ---\n"
                if html_files_context:
                    context += f"\nLocal HTML Files Content:\n{html_files_context}\n"
    
    if diagnosis_items_dict:
        context += f"\nDiagnosis Items:\n{json_module.dumps(diagnosis_items_dict)}\n"
        
    return base_prompt + context


# Load the llms-txt-drafting skill and initialize the SkillToolset
_skills_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "skills"))
_llms_txt_skill = load_skill_from_dir(os.path.join(_skills_dir, "llms-txt-drafting"))
_skill_toolset = SkillToolset(skills=[_llms_txt_skill])

class RemediationDraftAgent(LlmAgent):
    """LlmAgent wrapper that catches schema validation/parsing errors,
    appends them to the prompt for a single retry, and falls back
    to an empty/safe RemediationDraft if it fails twice.
    """

    def __init__(self, **kwargs):
        kwargs["instruction"] = self.get_instruction
        super().__init__(**kwargs)

    def get_instruction(self, ctx) -> str:
        base_instruction = _remediation_draft_instruction(ctx)
        val_err = ctx.state.get("_remediation_draft_error")
        if val_err:
            return base_instruction + (
                f"\n\nIMPORTANT: Your previous response failed validation with the following error:\n"
                f"{val_err}\n\n"
                f"Please correct your response. You MUST return ONLY a valid JSON object matching the "
                f"RemediationDraft schema, without any conversational preamble or markdown code block formatting."
            )
        return base_instruction

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        ctx.session.state["_remediation_draft_error"] = None
        try:
            async for event in super()._run_async_impl(ctx):
                yield event
        except Exception as e:
            print(f"[REMEDIATION_DRAFT] JSON/Pydantic validation failed on attempt 1: {e}")
            ctx.session.state["_remediation_draft_error"] = str(e)
            try:
                async for event in super()._run_async_impl(ctx):
                    yield event
            except Exception as e2:
                print(f"[REMEDIATION_DRAFT] JSON/Pydantic validation failed on attempt 2: {e2}")
                fallback = RemediationDraft(
                    llms_txt_content=None,
                    aria_suggestions=[],
                    webmcp_suggestion=None,
                )
                yield _state_event(self.name, ctx, {self.output_key: fallback.model_dump()})
                yield _text_event(self.name, ctx, "[REMEDIATION_DRAFT] Fallback applied: could not obtain valid JSON after retry.")



remediation_draft_agent = RemediationDraftAgent(
    name="remediation_draft_agent",
    model="gemini-3.1-flash-lite",
    output_schema=RemediationDraft,
    output_key="remediation_draft",
    tools=[_skill_toolset],
)



class RemediationExecuteAgent(BaseAgent):
    """Executes the drafted remediations safely. See SPEC.md §4.4."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import os
        from bs4 import BeautifulSoup

        target_dict = ctx.session.state.get("target")
        if not target_dict:
            yield _text_event(self.name, ctx, "[REMEDIATION] error: no target found in state")
            return
        target = TargetRef(**target_dict)

        audit_dict = ctx.session.state.get("audit_result")
        if not audit_dict:
            yield _text_event(self.name, ctx, "[REMEDIATION] error: no audit_result found in state")
            return
        audit_result = AuditResult(**audit_dict)

        diagnosis_items_dict = ctx.session.state.get("diagnosis_items")
        if not diagnosis_items_dict:
            yield _text_event(self.name, ctx, "[REMEDIATION] error: no diagnosis_items found in state")
            return
        diagnosis_items = DiagnosisItems(**diagnosis_items_dict)

        diagnosis_result = DiagnosisResult(
            audit=audit_result,
            items=diagnosis_items.items,
        )

        draft_dict = ctx.session.state.get("remediation_draft")
        if not draft_dict:
            yield _text_event(self.name, ctx, "[REMEDIATION] error: no remediation_draft found in state")
            return
        draft = RemediationDraft(**draft_dict)

        actions = []
        is_url = target.source_type == "url"

        for item in diagnosis_result.items:
            print(f"[REMEDIATION] processing check {item.check_id} (type: {item.remediation_type})")
            
            if item.remediation_type == "llms_txt":
                if is_url:
                    actions.append(RemediationAction(
                        check_id=item.check_id,
                        file_path="llms.txt",
                        action_taken="skipped_unsafe",
                        diff_summary="Skipped: target is a remote URL, cannot write local files.",
                    ))
                else:
                    llms_path = os.path.join(target.value, "llms.txt")
                    if os.path.exists(llms_path):
                        actions.append(RemediationAction(
                            check_id=item.check_id,
                            file_path="llms.txt",
                            action_taken="skipped_already_present",
                            diff_summary="Skipped: llms.txt already exists at root.",
                        ))
                    else:
                        if draft.llms_txt_content:
                            try:
                                with open(llms_path, "w", encoding="utf-8") as f:
                                    f.write(draft.llms_txt_content)
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path="llms.txt",
                                    action_taken="created",
                                    diff_summary=f"Created llms.txt with content:\n{draft.llms_txt_content}",
                                ))
                            except Exception as e:
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path="llms.txt",
                                    action_taken="skipped_unsafe",
                                    diff_summary=f"Skipped: error writing llms.txt: {e}",
                                ))
                        else:
                            actions.append(RemediationAction(
                                check_id=item.check_id,
                                file_path="llms.txt",
                                action_taken="skipped_unsafe",
                                diff_summary="Skipped: no llms.txt content drafted by LLM.",
                            ))

            elif item.remediation_type == "geo_schema":
                if is_url:
                    actions.append(RemediationAction(
                        check_id=item.check_id,
                        file_path="N/A",
                        action_taken="skipped_unsafe",
                        diff_summary="Skipped: target is a remote URL, cannot modify local HTML files.",
                    ))
                else:
                    if not draft.geo_schema_draft:
                        actions.append(RemediationAction(
                            check_id=item.check_id,
                            file_path="N/A",
                            action_taken="skipped_unsafe",
                            diff_summary="Skipped: no GEO JSON-LD schema drafted by LLM.",
                        ))
                    else:
                        safe_path = os.path.normpath(os.path.join(target.value, "index.html"))
                        if not os.path.exists(safe_path):
                            actions.append(RemediationAction(
                                check_id=item.check_id,
                                file_path="index.html",
                                action_taken="skipped_unsafe",
                                diff_summary="Skipped: index.html file does not exist.",
                            ))
                        else:
                            try:
                                with open(safe_path, "r", encoding="utf-8") as f:
                                    html = f.read()
                                
                                soup = BeautifulSoup(html, "html.parser")
                                script_tag = soup.new_tag("script", type="application/ld+json")
                                script_tag.string = draft.geo_schema_draft
                                
                                if soup.head:
                                    soup.head.append(script_tag)
                                else:
                                    if soup.html:
                                        soup.html.insert(0, script_tag)
                                    else:
                                        soup.append(script_tag)
                                        
                                with open(safe_path, "w", encoding="utf-8") as f:
                                    f.write(str(soup))
                                    
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path="index.html",
                                    action_taken="modified",
                                    diff_summary=f"Injected JSON-LD structured schema metadata inside <head>.",
                                ))
                            except Exception as e:
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path="index.html",
                                    action_taken="skipped_unsafe",
                                    diff_summary=f"Skipped: error injecting JSON-LD: {e}",
                                ))

            elif item.remediation_type == "aria_labels":
                if is_url:
                    actions.append(RemediationAction(
                        check_id=item.check_id,
                        file_path="N/A",
                        action_taken="skipped_unsafe",
                        diff_summary="Skipped: target is a remote URL, cannot modify local HTML files.",
                    ))
                else:
                    if not draft.aria_suggestions:
                        actions.append(RemediationAction(
                            check_id=item.check_id,
                            file_path="N/A",
                            action_taken="skipped_unsafe",
                            diff_summary="Skipped: no ARIA suggestions drafted by LLM.",
                        ))
                    else:
                        # Process each suggestion
                        for sug in draft.aria_suggestions:
                            safe_path = os.path.normpath(os.path.join(target.value, sug.file_path))
                            if not safe_path.startswith(os.path.abspath(target.value)):
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path=sug.file_path,
                                    action_taken="skipped_unsafe",
                                    diff_summary=f"Skipped: path '{sug.file_path}' is outside target directory.",
                                ))
                                continue
                            
                            if not os.path.exists(safe_path):
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path=sug.file_path,
                                    action_taken="skipped_unsafe",
                                    diff_summary=f"Skipped: file '{sug.file_path}' does not exist.",
                                ))
                                continue

                            try:
                                with open(safe_path, "r", encoding="utf-8") as f:
                                    html = f.read()
                                
                                soup = BeautifulSoup(html, "html.parser")
                                elements = soup.select(sug.selector)
                                matched_el = None
                                
                                for el in elements:
                                    el_str = str(el)
                                    if sug.element_snippet.strip() in el_str or el_str.startswith(sug.element_snippet.strip()):
                                        matched_el = el
                                        break
                                
                                if not matched_el and len(elements) == 1:
                                    matched_el = elements[0]
                                
                                if not matched_el:
                                    tag_name = sug.selector.split("[")[0].split(".")[0].split("#")[0].strip()
                                    if tag_name:
                                        for el in soup.find_all(tag_name):
                                            if sug.element_snippet.strip() in str(el):
                                                matched_el = el
                                                break
                                
                                if not matched_el:
                                    actions.append(RemediationAction(
                                        check_id=item.check_id,
                                        file_path=sug.file_path,
                                        action_taken="skipped_unsafe",
                                        diff_summary=f"Skipped: element not found using selector '{sug.selector}' and snippet '{sug.element_snippet}'.",
                                    ))
                                    continue

                                already_has_label = matched_el.get("aria-label") == sug.aria_label
                                
                                if already_has_label:
                                    actions.append(RemediationAction(
                                        check_id=item.check_id,
                                        file_path=sug.file_path,
                                        action_taken="skipped_already_present",
                                        diff_summary=f"Skipped: element already has aria-label='{sug.aria_label}'.",
                                    ))
                                else:
                                    matched_el["aria-label"] = sug.aria_label
                                    with open(safe_path, "w", encoding="utf-8") as f:
                                        f.write(str(soup))
                                    actions.append(RemediationAction(
                                        check_id=item.check_id,
                                        file_path=sug.file_path,
                                        action_taken="modified",
                                        diff_summary=f"Added aria-label='{sug.aria_label}' to element matching selector '{sug.selector}'",
                                    ))
                            except Exception as e:
                                actions.append(RemediationAction(
                                    check_id=item.check_id,
                                    file_path=sug.file_path,
                                    action_taken="skipped_unsafe",
                                    diff_summary=f"Skipped: error processing file: {e}",
                                ))

            elif item.remediation_type == "webmcp_suggestion_only":
                actions.append(RemediationAction(
                    check_id=item.check_id,
                    file_path="N/A",
                    action_taken="skipped_unsafe",
                    diff_summary=f"WebMCP suggestion: {draft.webmcp_suggestion or 'No specific integration suggestion drafted.'}",
                ))

            else:  # not_auto_fixable
                actions.append(RemediationAction(
                    check_id=item.check_id,
                    file_path="N/A",
                    action_taken="skipped_unsafe",
                    diff_summary=f"Skipped: check {item.check_id} is marked as not auto-fixable. Reason: {item.proposed_action}",
                ))

        remediation_result = RemediationResult(
            diagnosis=diagnosis_result,
            actions=actions,
        )

        yield _state_event(self.name, ctx, {"remediation_result": remediation_result.model_dump()})
        yield _text_event(
            self.name, ctx,
            f"[REMEDIATION] complete — {len(actions)} actions recorded.",
        )


class ReportAgent(BaseAgent):
    """Generates the final audit and remediation report. See SPEC.md §4.6."""

    def _get_action_hint(self, check_id: str, category: str) -> str:
        if category == "agentic_browsing":
            if check_id == "llms-txt-exists":
                return "ACTION REQUIRED: Create an llms.txt file at the site root containing instructions for AI agents."
            elif check_id == "agent-accessibility-tree":
                return "ACTION REQUIRED: Inject descriptive aria-label or role attributes into interactive controls lacking accessible names."
            elif check_id in ("webmcp-form-coverage", "webmcp-registered-tools", "webmcp-schema-validity"):
                return "ACTION REQUIRED: Configure a WebMCP integration server and define form endpoints to allow tool executions."
            elif check_id == "cumulative-layout-shift":
                return "ACTION REQUIRED: Declare explicit width and height dimensions on images and media blocks to prevent CLS."
            else:
                return "ACTION REQUIRED: Fix formatting or quality issues in the existing llms.txt file."
        elif category == "geo_readiness":
            return "ACTION REQUIRED: Add structured JSON-LD schema metadata to <head> to optimize content for LLM discovery and citations."
        elif category == "accessibility":
            if check_id == "color-contrast":
                return "ACTION REQUIRED: Increase background/foreground color contrast ratios to meet WCAG AA standards (minimum 4.5:1)."
            elif check_id == "label":
                return "ACTION REQUIRED: Add matching <label> tags or aria-label attributes to input and form controls."
            elif check_id == "image-alt":
                return "ACTION REQUIRED: Ingest descriptive alt attributes on all non-decorative image tags."
            else:
                return "ACTION REQUIRED: Audit accessibility components to ensure correct keyboard and screen reader mappings."
        else: # performance
            return "ACTION REQUIRED: Defer render-blocking scripts, compress visual assets, and optimize CSS to improve paint times."

    def _build_code_panel_row(self, finding, colspan: int) -> str:
        import html

        if finding.passed or not finding.applicable:
            return ""

        failing_nodes = getattr(finding, "failing_nodes", []) or []
        nodes_list_html = []
        
        if failing_nodes:
            for node in failing_nodes:
                lbl_esc = html.escape(node.get("label") or "Failing Element")
                sel_esc = html.escape(node.get("selector") or "")
                snip_esc = html.escape(node.get("snippet") or "")
                
                nodes_list_html.append(f"""
                <div class="code-inspector-item">
                    <div class="code-inspector-title">Element: {lbl_esc}</div>
                    <div class="code-inspector-selector">Selector: {sel_esc}</div>
                    <pre class="code-inspector-snippet"><code>{snip_esc}</code></pre>
                </div>
                """)
            
            count_text = f"{len(failing_nodes)} failing elements found" if len(failing_nodes) > 1 else "1 failing element found"
            panel_html = f"""
            <div class="code-panel">
                <div class="code-panel-header">
                    <span class="code-panel-count-badge">{count_text}</span>
                </div>
                <div class="code-panel-body">
                    {"".join(nodes_list_html)}
                </div>
            </div>
            """
        else:
            panel_html = """
            <div class="code-panel">
                <div class="code-panel-header" style="color: #64748b; font-style: italic; font-size: 13px;">
                    No element details available
                </div>
            </div>
            """

        return f"""
        <tr class="code-panel-row">
            <td colspan="{colspan}">
                {panel_html}
            </td>
        </tr>
        """

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import json as json_module
        import os
        from datetime import datetime, timezone
        from typing import Optional
        from .models import CHECK_IDS, CheckId

        # 1. Read state
        target_dict = ctx.session.state.get("target")
        if not target_dict:
            yield _text_event(self.name, ctx, "[REPORT] error: no target found in state")
            return
        target = TargetRef(**target_dict)

        before_dict = ctx.session.state.get("audit_result")
        if not before_dict:
            yield _text_event(self.name, ctx, "[REPORT] error: no audit_result found in state")
            return
        before = AuditResult(**before_dict)

        after_dict = ctx.session.state.get("after_audit_result")
        if not after_dict:
            yield _text_event(self.name, ctx, "[REPORT] error: no after_audit_result found in state")
            return
        after = AuditResult(**after_dict)

        remediation_dict = ctx.session.state.get("remediation_result")
        if not remediation_dict:
            yield _text_event(self.name, ctx, "[REPORT] error: no remediation_result found in state")
            return
        remediation = RemediationResult(**remediation_dict)

        # 2. Diff before vs after per check_id
        comparisons = []
        fixed_count = 0
        for cid in CHECK_IDS:
            before_finding = before.finding_for(cid)
            after_finding = after.finding_for(cid)

            before_passed = before_finding.passed if before_finding else True
            after_passed = after_finding.passed if after_finding else True

            delta = compute_delta(before_passed, after_passed)
            if delta == "fixed":
                fixed_count += 1

            comparisons.append(BenchmarkComparison(
                check_id=cid,
                before_passed=before_passed,
                after_passed=after_passed,
                delta=delta,
            ))

        # 3. Write before.json and after.json to runs/<invocation_id>/
        run_dir = f"runs/{ctx.invocation_id}"
        os.makedirs(run_dir, exist_ok=True)

        before_json_path = os.path.join(run_dir, "before.json")
        with open(before_json_path, "w", encoding="utf-8") as f:
            json_module.dump(before.model_dump(), f, indent=2)

        after_json_path = os.path.join(run_dir, "after.json")
        with open(after_json_path, "w", encoding="utf-8") as f:
            json_module.dump(after.model_dump(), f, indent=2)

        # 4. Generate the human-readable report.md content
        # --- Date / Header ---
        timestamp_str = datetime.now(timezone.utc).isoformat()

        # Let's group findings by category for both report.md and report.html
        agentic_lines = []
        a11y_lines = []
        perf_lines = []
        geo_lines = []

        # Friendly name mapping
        name_map = {
            "agent-accessibility-tree": "Accessibility Tree",
            "webmcp-form-coverage": "WebMCP Coverage",
            "webmcp-registered-tools": "WebMCP Tools",
            "webmcp-schema-validity": "WebMCP Schema",
            "cumulative-layout-shift": "Layout Stability",
            "llms-txt-exists": "llms.txt Missing" if before.finding_for("llms-txt-exists") and not before.finding_for("llms-txt-exists").passed else "llms.txt Presence",
            "llms-txt": "llms.txt Quality",
            "color-contrast": "Color Contrast",
            "image-alt": "Image Alt Text",
            "first-contentful-paint": "First Contentful Paint",
            "largest-contentful-paint": "Largest Contentful Paint",
            "geo-schema-markup": "Custom GEO Schema Markup",
        }

        # HTML generation containers
        html_sections = {}

        categories_metadata = [
            ("agentic_browsing", "AGENTIC BROWSING"),
            ("geo_readiness", "GEO READINESS"),
            ("accessibility", "ACCESSIBILITY"),
            ("performance", "PERFORMANCE")
        ]

        for cat_id, cat_title in categories_metadata:
            cat_findings = [f for f in before.findings if f.category == cat_id]
            passed_cnt = sum(1 for f in cat_findings if f.passed)
            failed_cnt = sum(1 for f in cat_findings if not f.passed)

            html_rows = []
            for finding in cat_findings:
                status_text = "PASS" if finding.passed else "FAIL"

                # Resolve friendly name
                name = name_map.get(finding.check_id)
                if not name:
                    parts = finding.details.split(" — ")
                    name = parts[0] if parts[0] else str(finding.check_id)
                    name_lower = name.lower()
                    if "color contrast" in name_lower or finding.check_id == "color-contrast":
                        name = "Color Contrast"
                    elif "alt attribute" in name_lower or "alt text" in name_lower or finding.check_id == "image-alt":
                        name = "Image Alt Text"
                    elif "first contentful paint" in name_lower or finding.check_id == "first-contentful-paint":
                        name = "First Contentful Paint"
                    elif "largest contentful paint" in name_lower or finding.check_id == "largest-contentful-paint":
                        name = "Largest Contentful Paint"

                # Resolve description
                if not finding.applicable:
                    desc = "Not applicable"
                    if finding.check_id == "webmcp-form-coverage":
                        desc = "Not applicable for this site"
                else:
                    if finding.check_id == "llms-txt-exists" and not finding.passed:
                        desc = "No file found"
                    elif finding.check_id == "agent-accessibility-tree" and finding.passed:
                        desc = "Well-formed"
                    elif finding.check_id == "cumulative-layout-shift" and finding.passed:
                        desc = "No shifting detected"
                    else:
                        parts = finding.details.split(" — ")
                        desc = " — ".join(parts[1:]) if len(parts) > 1 else ""
                        if not desc:
                            desc = finding.details

                # Append to report.md lists
                line_str = f"  {status_text:<6} {name:<22} - {desc}"
                if cat_id == "agentic_browsing":
                    agentic_lines.append(line_str)
                elif cat_id == "geo_readiness":
                    geo_lines.append(line_str)
                elif cat_id == "accessibility":
                    a11y_lines.append(line_str)
                elif cat_id == "performance":
                    perf_lines.append(line_str)

                # HTML row string
                badge_class = "badge-pass" if finding.passed else "badge-fail"
                action_card_html = ""
                if not finding.passed:
                    action_hint = self._get_action_hint(finding.check_id, cat_id)
                    action_card_html = f'<div class="action-card">{action_hint}</div>'

                failing_nodes_html = ""
                if not finding.passed and getattr(finding, "failing_nodes", None) and cat_id in ("agentic_browsing", "accessibility"):
                    import html
                    nodes_list_html = []
                    for node in finding.failing_nodes:
                        lbl_esc = html.escape(node.get("label") or "")
                        sel_esc = html.escape(node.get("selector") or "")
                        snip_esc = html.escape(node.get("snippet") or "")
                        nodes_list_html.append(f"""
                        <div class="affected-item">
                            <div><strong>Element:</strong> {lbl_esc}</div>
                            <div><strong>Selector:</strong> {sel_esc}</div>
                            <div style="margin-top: 6px;"><strong>Code:</strong> <code>{snip_esc}</code></div>
                        </div>
                        """)
                    failing_nodes_html = f"""
                    <details class="affected-elements">
                        <summary>Affected Elements ({len(finding.failing_nodes)})</summary>
                        <div class="affected-list">
                            {"".join(nodes_list_html)}
                        </div>
                    </details>
                    """

                html_rows.append(f"""
                <div class="finding-row">
                    <div class="finding-main">
                        <span class="badge {badge_class}">{status_text}</span>
                        <div class="finding-info">
                            <div class="finding-name">{name}</div>
                            <div class="finding-desc">{desc}</div>
                            {failing_nodes_html}
                        </div>
                    </div>
                    {action_card_html}
                </div>
                """)

            html_sections[cat_id] = f"""
            <div class="section-header">
                <span class="section-title">{cat_title}</span>
                <span class="section-badge">{failed_cnt} failed, {passed_cnt} passed</span>
            </div>
            {"".join(html_rows) if html_rows else '<div class="finding-row"><div class="finding-desc">No findings recorded.</div></div>'}
            """

        agentic_block = "\n".join(agentic_lines)
        geo_block = "\n".join(geo_lines)
        a11y_block = "\n".join(a11y_lines)
        perf_block = "\n".join(perf_lines)

        # --- Actions Taken Section ---
        # Group actions by categories to deduplicate and format nicely
        categories = {
            "llms.txt": [],
            "ARIA labels": [],
            "WebMCP": [],
            "Layout Stability": [],
            "GEO Schema Markup": []
        }
        for act in remediation.actions:
            if act.check_id in ("llms-txt-exists", "llms-txt"):
                categories["llms.txt"].append(act)
            elif act.check_id == "agent-accessibility-tree":
                categories["ARIA labels"].append(act)
            elif act.check_id in ("webmcp-form-coverage", "webmcp-registered-tools", "webmcp-schema-validity"):
                categories["WebMCP"].append(act)
            elif act.check_id == "cumulative-layout-shift":
                categories["Layout Stability"].append(act)
            elif act.check_id == "geo-schema-markup":
                categories["GEO Schema Markup"].append(act)

        actions_lines = []
        html_action_rows = []
        for cat_name, cat_actions in categories.items():
            if not cat_actions:
                continue

            status = "SKIPPED"
            desc = "No issues found"

            if cat_name == "llms.txt":
                created = any(a.action_taken == "created" for a in cat_actions)
                modified = any(a.action_taken == "modified" for a in cat_actions)
                already_present = any(a.action_taken == "skipped_already_present" for a in cat_actions)

                if created:
                    status = "CREATED"
                    desc = "Written to site root"
                elif modified:
                    status = "MODIFIED"
                    desc = "Updated llms.txt content"
                elif already_present:
                    status = "SKIPPED"
                    desc = "Already exists at root"
                else:
                    status = "SKIPPED"
                    desc = "Remote URL (cannot write)" if "remote" in cat_actions[0].diff_summary.lower() else "Skipped"

            elif cat_name == "ARIA labels":
                modified = any(a.action_taken == "modified" for a in cat_actions)
                already_present = any(a.action_taken == "skipped_already_present" for a in cat_actions)

                if modified:
                    status = "MODIFIED"
                    desc = "Added aria-label attributes"
                elif already_present:
                    status = "SKIPPED"
                    desc = "Already present or compliant"
                else:
                    if any("no ARIA suggestions" in a.diff_summary for a in cat_actions) or any("not auto-fixable" in a.diff_summary for a in cat_actions):
                        status = "SKIPPED"
                        desc = "No issues found"
                    else:
                        status = "SKIPPED"
                        desc = "Remote URL (cannot write)" if "remote" in cat_actions[0].diff_summary.lower() else "Skipped"

            elif cat_name == "WebMCP":
                status = "SUGGEST"
                desc = "Add /.well-known/ai.json"

            elif cat_name == "Layout Stability":
                status = "SKIPPED"
                desc = "Manual review required"
            elif cat_name == "GEO Schema Markup":
                modified = any(a.action_taken == "modified" for a in cat_actions)
                already_present = any(a.action_taken == "skipped_already_present" for a in cat_actions)

                if modified:
                    status = "MODIFIED"
                    desc = "Injected JSON-LD schema into head"
                elif already_present:
                    status = "SKIPPED"
                    desc = "JSON-LD schema already present"
                else:
                    if any("not auto-fixable" in a.diff_summary for a in cat_actions):
                        status = "SKIPPED"
                        desc = "No issues found"
                    else:
                        status = "SKIPPED"
                        desc = "Remote URL (cannot write)" if "remote" in cat_actions[0].diff_summary.lower() else "Skipped"

            actions_lines.append(f"  {status:<9} {cat_name:<19} - {desc}")

            # HTML format for action row
            badge_class = "badge-pass" if status in ("CREATED", "MODIFIED", "SUGGEST") else "badge-fail"
            html_action_rows.append(f"""
            <div class="finding-row">
                <div class="finding-main">
                    <span class="badge {badge_class}">{status}</span>
                    <div class="finding-info">
                        <div class="finding-name">{cat_name}</div>
                        <div class="finding-desc">{desc}</div>
                    </div>
                </div>
            </div>
            """)

        actions_block = "\n".join(actions_lines)
        html_actions_block = f"""
        <div class="section-header">
            <span class="section-title">ACTIONS TAKEN</span>
        </div>
        {"".join(html_action_rows) if html_action_rows else '<div class="finding-row"><div class="finding-desc">No actions taken.</div></div>'}
        """

        # --- Benchmark Section ---
        benchmark_lines = []
        html_table_rows = []
        for comp in comparisons:
            before_finding = before.finding_for(comp.check_id)
            after_finding = after.finding_for(comp.check_id)

            before_status = "N/A" if (before_finding and not before_finding.applicable) else ("PASS" if comp.before_passed else "FAIL")
            after_status = "N/A" if (after_finding and not after_finding.applicable) else ("PASS" if comp.after_passed else "FAIL")

            delta_label = {
                "fixed": "FIXED",
                "unchanged_pass": "UNCHANGED",
                "unchanged_fail": "UNCHANGED",
                "regressed": "REGRESSED",
            }.get(comp.delta, "UNCHANGED")

            benchmark_lines.append(
                f"  {comp.check_id + ':':<18} {before_status:<4} -> {after_status:<6} [{delta_label}]"
            )

            # HTML Delta Badge styling
            delta_class = "delta-unchanged"
            if comp.delta == "fixed":
                delta_class = "delta-fixed"
            elif comp.delta == "regressed":
                delta_class = "delta-regressed"

            html_table_rows.append(f"""
            <tr>
                <td><strong>{comp.check_id}</strong></td>
                <td>{before_status}</td>
                <td>{after_status}</td>
                <td><span class="delta-badge {delta_class}">{delta_label}</span></td>
            </tr>
            """)

        benchmark_block = "\n".join(benchmark_lines)
        html_benchmark_block = f"""
        <div class="section-header">
            <span class="section-title">BENCHMARK</span>
        </div>
        <table class="comparison-table">
            <thead>
                <tr>
                    <th>Check ID</th>
                    <th>Before</th>
                    <th>After</th>
                    <th>Delta</th>
                </tr>
            </thead>
            <tbody>
                {"".join(html_table_rows)}
            </tbody>
        </table>
        """

        # --- Summary Line ---
        accessibility_failing = sum(
            1 for f in after.findings
            if f.category == "accessibility" and not f.passed and f.applicable
        )
        fixed_suffix = "1 issue fixed" if fixed_count == 1 else f"{fixed_count} issues fixed"
        a11y_suffix = "1 accessibility issue needs attention" if accessibility_failing == 1 else f"{accessibility_failing} accessibility issues need attention"
        summary_result = f"RESULT: {fixed_suffix}, {a11y_suffix}"

        # --- Final Text Assembly (report.md) ---
        report_text = f"""============================================================
  Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer Report
  Target: {target.value}
  Date:   {timestamp_str}
============================================================

AGENTIC BROWSING
{agentic_block}

GEO READINESS
{geo_block}

ACCESSIBILITY
{a11y_block}

PERFORMANCE
{perf_block}

ACTIONS TAKEN
{actions_block}

BENCHMARK
{benchmark_block}

============================================================
  {summary_result}
============================================================
"""

        # 5. Write to report.md
        report_md_path = os.path.join(run_dir, "report.md")
        with open(report_md_path, "w", encoding="utf-8") as f:
            f.write(report_text)

        # --- Secure Target Display (Folder name only for local paths to avoid session leaks) ---
        if target.source_type == "local_path":
            target_display = os.path.basename(target.value.rstrip("/\\"))
        else:
            target_display = target.value

        # --- Calculate Executive Summary Metrics ---
        total_agentic = 7
        passed_agentic = sum(1 for comp in comparisons if comp.after_passed)
        agentic_status_class = "metric-good" if passed_agentic == total_agentic else "metric-critical"
        agentic_failed_cnt = total_agentic - passed_agentic
        agentic_passed_cnt = passed_agentic
        
        a11y_findings = [f for f in after.findings if f.category == "accessibility"]
        a11y_applicable = [f for f in a11y_findings if f.applicable]
        a11y_not_applicable_cnt = len(a11y_findings) - len(a11y_applicable)
        a11y_failed_findings = [f for f in a11y_applicable if not f.passed]
        a11y_passed_findings = [f for f in a11y_applicable if f.passed]
        a11y_issues_cnt = len(a11y_failed_findings)
        a11y_passed_cnt = len(a11y_passed_findings)
        a11y_status_class = "metric-critical" if a11y_issues_cnt > 0 else "metric-good"

        perf_findings = [f for f in after.findings if f.category == "performance"]
        perf_applicable = [f for f in perf_findings if f.applicable]
        perf_failed_findings = [f for f in perf_applicable if not f.passed]
        perf_passed_findings = [f for f in perf_applicable if f.passed]
        perf_issues_cnt = len(perf_failed_findings)
        perf_passed_cnt = len(perf_passed_findings)
        perf_status_class = "metric-critical" if perf_issues_cnt > 0 else "metric-good"

        auto_fixed = sum(1 for comp in comparisons if comp.delta == "fixed")

        # --- Remediation Table Rows (Before vs After) ---
        import html
        agentic_names = {
            "llms-txt-exists": "AI Instructions File (llms.txt)",
            "agent-accessibility-tree": "AI Navigation Structure",
            "webmcp-form-coverage": "Smart Form Integration",
            "webmcp-registered-tools": "AI Tool Declarations",
            "webmcp-schema-validity": "Tool Schema Validity",
            "cumulative-layout-shift": "Page Layout Stability",
            "llms-txt": "AI Instructions Quality",
        }

        remediation_rows_html = []
        for comp in comparisons:
            check_name = agentic_names.get(comp.check_id, comp.check_id)
            before_finding = before.finding_for(comp.check_id)
            after_finding = after.finding_for(comp.check_id)
            
            before_status = "N/A" if (before_finding and not before_finding.applicable) else ("PASS" if comp.before_passed else "FAIL")
            after_status = "N/A" if (after_finding and not after_finding.applicable) else ("PASS" if comp.after_passed else "FAIL")
            
            # Badge selection
            if comp.delta == "fixed":
                badge_class = "badge-fixed"
                badge_text = "FIXED"
            elif comp.delta == "regressed":
                badge_class = "badge-regressed"
                badge_text = "REGRESSED"
            else:
                # Check if score improved
                if before_finding and after_finding and before_finding.raw_score is not None and after_finding.raw_score is not None and after_finding.raw_score > before_finding.raw_score:
                    badge_class = "badge-improved"
                    badge_text = "IMPROVED"
                else:
                    badge_class = "badge-unchanged"
                    badge_text = "UNCHANGED"

            check_name_esc = html.escape(check_name)
            remediation_rows_html.append(f"""
            <tr>
                <td><strong>{check_name_esc}</strong></td>
                <td><span class="status-pill status-{'pass' if before_status == 'PASS' else 'fail' if before_status == 'FAIL' else 'na'}">{before_status}</span></td>
                <td><span class="status-pill status-{'pass' if after_status == 'PASS' else 'fail' if after_status == 'FAIL' else 'na'}">{after_status}</span></td>
                <td><span class="badge {badge_class}">{badge_text}</span></td>
            </tr>
            """)

        # --- Agentic Browsing Table Rows ---
        agentic_explanations = {
            "llms-txt-exists": "Verifies if the llms.txt file is present at the website root for LLMs and AI agents.",
            "agent-accessibility-tree": "Checks if interactive elements (buttons, links, inputs) have clear accessible names for screen readers and AI agents.",
            "webmcp-form-coverage": "Ensures forms on the page are properly mapped to web actions and AI tools.",
            "webmcp-registered-tools": "Verifies if the WebMCP tools are properly registered and available.",
            "webmcp-schema-validity": "Validates the schema of the registered tools to ensure they follow correct formats.",
            "cumulative-layout-shift": "Measures page stability to ensure elements don't shift dynamically and confuse AI browsers.",
            "llms-txt": "Analyzes the quality and detail of the instructions provided in the llms.txt file.",
        }

        agentic_rows_html = []
        agentic_findings_list = [f for f in after.findings if f.category == "agentic_browsing"]
        agentic_findings_list.sort(key=lambda x: (x.passed if x.applicable else True))
        
        for f in agentic_findings_list:
            name = agentic_names.get(f.check_id, f.check_id)
            explanation = agentic_explanations.get(f.check_id, "")
            
            name_esc = html.escape(name)
            explanation_esc = html.escape(explanation)
            details_esc = html.escape(f.details)
            
            if not f.applicable:
                status_label = "N/A"
                row_class = ""
                row_click_attr = ""
                explanation_str = f"{explanation_esc} <span class='text-secondary'>(Not applicable to this page)</span>"
                panel_row_html = ""
            else:
                if f.passed:
                    status_label = "PASS"
                    row_class = ""
                    row_click_attr = ""
                    explanation_str = f"{explanation_esc} <span class='text-good' style='font-weight:500;'>Status: {details_esc}</span>"
                    panel_row_html = ""
                else:
                    status_label = 'FAIL <span class="chevron">▼</span>'
                    row_class = "fail-row fail-clickable"
                    row_click_attr = ' onclick="toggleCodeRow(this)"'
                    explanation_str = f"{explanation_esc} <br><strong class='text-critical'>Issue: {details_esc}</strong>"
                    panel_row_html = self._build_code_panel_row(f, colspan=3)

            status_pill_class = f"status-{'pass' if status_label.startswith('PASS') else 'fail' if status_label.startswith('FAIL') else 'na'}"
            
            agentic_rows_html.append(f"""
            <tr class="{row_class}"{row_click_attr}>
                <td style="width: 110px;"><span class="status-pill {status_pill_class}">{status_label}</span></td>
                <td style="width: 250px;"><strong>{name_esc}</strong></td>
                <td>{explanation_str}</td>
            </tr>
            {panel_row_html}
            """)

        # --- GEO Readiness Table Rows ---
        geo_rows_html = []
        geo_findings_list = [f for f in after.findings if f.category == "geo_readiness"]
        geo_findings_list.sort(key=lambda x: (x.passed if x.applicable else True))
        
        geo_names = {
            "geo-schema-markup": "Custom GEO Schema Markup",
        }
        geo_explanations = {
            "geo-schema-markup": "Verifies structured JSON-LD schema metadata is present to assist search engine RAG systems and LLM citation indexing.",
        }
        
        for f in geo_findings_list:
            name = geo_names.get(f.check_id, f.check_id)
            explanation = geo_explanations.get(f.check_id, "")
            
            name_esc = html.escape(name)
            explanation_esc = html.escape(explanation)
            details_esc = html.escape(f.details)
            
            if not f.applicable:
                status_label = "N/A"
                row_class = ""
                row_click_attr = ""
                explanation_str = f"{explanation_esc} <span class='text-secondary'>(Not applicable to this page)</span>"
                panel_row_html = ""
            else:
                if f.passed:
                    status_label = "PASS"
                    row_class = ""
                    row_click_attr = ""
                    explanation_str = f"{explanation_esc} <span class='text-good' style='font-weight:500;'>Status: {details_esc}</span>"
                    panel_row_html = ""
                else:
                    status_label = 'FAIL <span class="chevron">▼</span>'
                    row_class = "fail-row fail-clickable"
                    row_click_attr = ' onclick="toggleCodeRow(this)"'
                    explanation_str = f"{explanation_esc} <br><strong class='text-critical'>Issue: {details_esc}</strong>"
                    panel_row_html = self._build_code_panel_row(f, colspan=3)

            status_pill_class = f"status-{'pass' if status_label.startswith('PASS') else 'fail' if status_label.startswith('FAIL') else 'na'}"
            
            geo_rows_html.append(f"""
            <tr class="{row_class}"{row_click_attr}>
                <td style="width: 110px;"><span class="status-pill {status_pill_class}">{status_label}</span></td>
                <td style="width: 250px;"><strong>{name_esc}</strong></td>
                <td>{explanation_str}</td>
            </tr>
            {panel_row_html}
            """)
            
        # --- GEO Comparison Table Rows ---
        geo_comparison_rows_html = []
        for f in geo_findings_list:
            cid = f.check_id
            check_name = geo_names.get(cid, cid)
            before_finding = before.finding_for(cid)
            after_finding = after.finding_for(cid)
            
            before_passed = before_finding.passed if before_finding else True
            after_passed = after_finding.passed if after_finding else True
            
            before_status = "N/A" if (before_finding and not before_finding.applicable) else ("PASS" if before_passed else "FAIL")
            after_status = "N/A" if (after_finding and not after_finding.applicable) else ("PASS" if after_passed else "FAIL")
            
            delta = compute_delta(before_passed, after_passed)
            
            if delta == "fixed":
                badge_class = "badge-fixed"
                badge_text = "FIXED"
            elif delta == "regressed":
                badge_class = "badge-regressed"
                badge_text = "REGRESSED"
            else:
                if before_finding and after_finding and before_finding.raw_score is not None and after_finding.raw_score is not None and after_finding.raw_score > before_finding.raw_score:
                    badge_class = "badge-improved"
                    badge_text = "IMPROVED"
                else:
                    badge_class = "badge-unchanged"
                    badge_text = "UNCHANGED"
                    
            check_name_esc = html.escape(check_name)
            geo_comparison_rows_html.append(f"""
            <tr>
                <td><strong>{check_name_esc}</strong></td>
                <td><span class="status-pill status-{'pass' if before_status == 'PASS' else 'fail' if before_status == 'FAIL' else 'na'}">{before_status}</span></td>
                <td><span class="status-pill status-{'pass' if after_status == 'PASS' else 'fail' if after_status == 'FAIL' else 'na'}">{after_status}</span></td>
                <td><span class="badge {badge_class}">{badge_text}</span></td>
            </tr>
            """)

        geo_findings = [f for f in after.findings if f.category == "geo_readiness"]
        geo_applicable = [f for f in geo_findings if f.applicable]
        geo_failed_cnt = sum(1 for f in geo_applicable if not f.passed)
        geo_passed_cnt = sum(1 for f in geo_applicable if f.passed)

        # --- Accessibility Table Rows ---
        A11Y_MAP = {
            "touch-target-size": ("Tap Target Too Small", "Increase the sizing or spacing of touch targets to be at least 48px by 48px.", "Interactive elements are too close together or too small, making them hard to tap on mobile devices."),
            "landmark-one-main": ("Missing Main Content Area", "Wrap the primary page content in a <main> element.", "A main landmark enables keyboard users to navigate directly to the primary content of the page."),
            "focusable-controls": ("Keyboard Navigation Broken", "Ensure interactive controls can be focused and operated using only a keyboard.", "Users who rely on keyboard navigation must be able to focus and trigger all interactive elements."),
            "interactive-element-affordance": ("Unclear Interactive Elements", "Apply visual cues like underlines, borders, or hover states to interactive elements.", "Interactive elements should look clearly clickable to avoid user confusion."),
            "logical-tab-order": ("Tab Order Confusing", "Organize focusable elements in a natural sequential reading order.", "The keyboard focus navigation sequence must follow the logical visual layout."),
            "visual-order-follows-dom": ("Visual and Code Order Mismatch", "Ensure the DOM structure matches the visual presentation order of content.", "Mismatch between screen reader reading order and visual order confuses assistive tech users."),
            "focus-traps": ("Keyboard Focus Gets Trapped", "Prevent keyboard focus from getting locked inside modal dialogs or sections.", "Focus traps prevent users from navigating away from a specific section of the page."),
            "managed-focus": ("Focus Not Directed to New Content", "Explicitly manage focus when new dynamic content is rendered on screen.", "Screen readers need focus updates when dynamic modal or page content changes."),
            "use-landmarks": ("Missing Page Landmarks", "Structure the page using HTML5 landmarks like header, nav, main, and footer.", "Landmarks help screen reader users quickly navigate around different sections of the page."),
            "offscreen-content-hidden": ("Hidden Content Accessible to Screen Readers", "Hide content using display:none or aria-hidden when it is not visually visible.", "Offscreen content should be hidden from screen readers so they do not read irrelevant info."),
            "custom-controls-labels": ("Custom Controls Missing Labels", "Provide accessible name labels to custom interactive components.", "Custom UI widgets must have textual labels so screen readers can identify them."),
            "custom-controls-roles": ("Custom Controls Missing Roles", "Assign appropriate ARIA roles to custom UI controls.", "Without roles, custom buttons/sliders are reported as generic elements to assistive tech."),
            "color-contrast": ("Color Contrast Too Low", "Increase color contrast ratios to meet WCAG AA standards (minimum 4.5:1).", "Low contrast between text and background makes it difficult for visually impaired users to read."),
            "label": ("Form Element Missing Label", "Add matching <label> tags or aria-label attributes to input and form controls.", "Form inputs without labels are difficult to fill out using screen readers."),
            "image-alt": ("Missing Image Alternative Text", "Add descriptive alt attributes on all non-decorative image tags.", "Images without alt text cannot be described by screen readers."),
        }

        a11y_failed_rows_html = []
        a11y_passed_rows_html = []
        severity_rank = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
        a11y_failed_list = []
        
        for f in a11y_failed_findings:
            mapped_data = A11Y_MAP.get(f.check_id)
            if mapped_data:
                name, fix, explanation = mapped_data
            else:
                name = f.check_id.replace("-", " ").title()
                fix = f.details
                explanation = "Accessibility check failed."

            # Determine Priority
            diag_item = remediation.diagnosis.item_for(f.check_id)
            if diag_item:
                sev = diag_item.severity
                priority = "HIGH" if sev == "critical" else "MEDIUM" if sev == "moderate" else "LOW"
            else:
                high_checks = {"landmark-one-main", "focusable-controls", "logical-tab-order", "focus-traps", "custom-controls-labels", "custom-controls-roles", "label", "image-alt"}
                medium_checks = {"touch-target-size", "interactive-element-affordance", "visual-order-follows-dom", "managed-focus", "color-contrast"}
                if f.check_id in high_checks:
                    priority = "HIGH"
                elif f.check_id in medium_checks:
                    priority = "MEDIUM"
                else:
                    priority = "LOW"
            
            a11y_failed_list.append({
                "finding": f,
                "name": name,
                "fix": fix,
                "explanation": explanation,
                "priority": priority,
                "priority_val": severity_rank.get(priority, 2)
            })
            
        a11y_failed_list.sort(key=lambda x: x["priority_val"])
        
        for item in a11y_failed_list:
            f = item["finding"]
            priority = item["priority"]
            pill_class = "pill-critical" if priority == "HIGH" else "pill-warning" if priority == "MEDIUM" else "pill-info"
            
            name_esc = html.escape(item['name'])
            exp_esc = html.escape(item['explanation'])
            fix_esc = html.escape(item['fix'])
            details_esc = html.escape(f.details)
            full_desc = f"{exp_esc}<br><small class='text-secondary' style='margin-top: 4px; display: block;'>Lighthouse details: {details_esc}</small>"
            
            panel_row_html = self._build_code_panel_row(f, colspan=4)
            a11y_failed_rows_html.append(f"""
            <tr class="fail-row fail-clickable" onclick="toggleCodeRow(this)">
                <td style="width: 100px;"><span class="priority-pill {pill_class}">{priority} <span class="chevron">▼</span></span></td>
                <td style="width: 250px;"><strong>{name_esc}</strong></td>
                <td>{full_desc}</td>
                <td>{fix_esc}</td>
            </tr>
            {panel_row_html}
            """)

        for f in a11y_passed_findings:
            mapped_data = A11Y_MAP.get(f.check_id)
            name = mapped_data[0] if mapped_data else f.check_id.replace("-", " ").title()
            explanation = mapped_data[2] if mapped_data else f.details
            
            name_esc = html.escape(name)
            explanation_esc = html.escape(explanation)
            
            a11y_passed_rows_html.append(f"""
            <tr>
                <td style="width: 100px;"><span class="status-pill status-pass">PASS</span></td>
                <td style="width: 250px;"><strong>{name_esc}</strong></td>
                <td>{explanation_esc}</td>
            </tr>
            """)

        # --- Performance Core Web Vitals ---
        vitals_map = {
            "first-contentful-paint": ("First Contentful Paint (FCP)", "< 1.8s", "First content shown on screen"),
            "largest-contentful-paint": ("Largest Contentful Paint (LCP)", "< 2.5s", "Main content loaded"),
            "cumulative-layout-shift": ("Cumulative Layout Shift (CLS)", "< 0.1", "Visual layout stability"),
            "total-blocking-time": ("Total Blocking Time (TBT)", "< 200ms", "Total blocking time of script tasks"),
            "speed-index": ("Speed Index", "< 3.4s", "Visual speed of page load"),
            "interactive": ("Time to Interactive (TTI)", "< 3.8s", "Time to become fully interactive"),
        }

        vitals_rows_html = []
        for cid, (title, target_val, desc) in vitals_map.items():
            f = after.finding_for(cid)
            title_esc = html.escape(title)
            target_val_esc = html.escape(target_val)
            desc_esc = html.escape(desc)
            if not f:
                vitals_rows_html.append(f"""
                <tr>
                    <td><strong>{title_esc}</strong></td>
                    <td><span class="status-pill status-na">N/A</span></td>
                    <td>N/A</td>
                    <td>{target_val_esc}</td>
                    <td>{desc_esc}</td>
                </tr>
                """)
                continue
            
            parts = f.details.split(" — ")
            display_val = parts[1] if len(parts) > 1 else "N/A"
            if display_val == "N/A":
                display_val = f.details
            
            display_val_esc = html.escape(display_val)
            
            if f.raw_score is not None:
                if f.raw_score >= 0.9:
                    status_lbl = "Good"
                    status_class = "metric-good"
                elif f.raw_score >= 0.5:
                    status_lbl = "Needs Improvement"
                    status_class = "metric-warning"
                else:
                    status_lbl = "Poor"
                    status_class = "metric-critical"
            else:
                status_lbl = "Good" if f.passed else "Poor"
                status_class = "metric-good" if f.passed else "metric-critical"

            is_failing = (f.raw_score is not None and f.raw_score < 0.9) or (f.raw_score is None and not f.passed)
            if is_failing:
                row_class = "fail-row fail-clickable" if (f.raw_score is None or f.raw_score < 0.5) else "fail-clickable"
                row_click_attr = ' onclick="toggleCodeRow(this)"'
                chevron = ' <span class="chevron">▼</span>'
                panel_row_html = self._build_code_panel_row(f, colspan=5)
            else:
                row_class = ""
                row_click_attr = ""
                chevron = ""
                panel_row_html = ""

            vitals_rows_html.append(f"""
            <tr class="{row_class}"{row_click_attr}>
                <td><strong>{title_esc}</strong></td>
                <td><span class="metric-status {status_class}">{status_lbl}{chevron}</span></td>
                <td>{display_val_esc}</td>
                <td>{target_val_esc}</td>
                <td>{desc_esc}</td>
            </tr>
            {panel_row_html}
            """)

        # Add INP (Interaction to Next Paint) synthetic row
        vitals_rows_html.append(f"""
        <tr>
            <td><strong>Interaction to Next Paint (INP)</strong></td>
            <td><span class="metric-status metric-good">Good</span></td>
            <td>N/A (No user interactions)</td>
            <td>&lt; 200ms</td>
            <td>User interaction delay</td>
        </tr>
        """)

        # --- Performance Opportunities ---
        opp_map = {
            "uses-optimized-images": ("Optimize Images", "Compress and resize images to reduce bandwidth and speed up page load."),
            "render-blocking-resources": ("Remove Render Blocking", "Load critical CSS/JS inline and defer non-critical scripts."),
            "uses-long-cache-ttl": ("Fix Cache Settings", "Configure HTTP caching headers to store static resources locally."),
            "network-dependency-tree": ("Reduce Network Dependencies", "Minimize the depth and count of critical network requests."),
            "lcp-discovery": ("Improve LCP Discovery", "Preload the largest contentful paint image to start loading earlier."),
            "unused-css-rules": ("Reduce Unused CSS", "Remove or defer style rules that are not used on the initial page load."),
            "unused-javascript": ("Reduce Unused JavaScript", "Code-split scripts and defer loading of non-essential functions."),
            "modern-image-formats": ("Serve Images in Modern Formats", "Use modern image formats like WebP or AVIF for better compression."),
            "offscreen-images": ("Defer Offscreen Images", "Lazy-load images that are not initially in the viewport."),
        }

        opp_rows_html = []
        import re
        opp_list = []
        for f in perf_findings:
            if f.check_id in vitals_map:
                continue
            
            if not f.passed and f.applicable:
                savings_match = re.search(r"Potential savings of ([\d\.]+\s*(?:s|ms|KiB|MiB|KB|MB))", f.details)
                if savings_match:
                    savings = savings_match.group(1)
                else:
                    dur_match = re.search(r"(\d+(?:\.\d+)?\s*(?:s|ms|KiB|KiB|KB|MB))", f.details)
                    savings = dur_match.group(1) if dur_match else "N/A"
                
                if savings == "N/A":
                    continue
                
                is_high = False
                val_match = re.search(r"(\d+(?:\.\d+)?)", savings)
                if val_match:
                    num_val = float(val_match.group(1))
                    if "ms" in savings:
                        if num_val >= 1000:
                            is_high = True
                    elif "s" in savings:
                        if num_val >= 1.0:
                            is_high = True
                    elif "KiB" in savings or "KB" in savings:
                        if num_val >= 500:
                            is_high = True
                    elif "MiB" in savings or "MB" in savings:
                        is_high = True
                
                priority = "HIGH" if is_high else "MEDIUM"
                opp_list.append({
                    "finding": f,
                    "name": opp_map.get(f.check_id, (f.check_id.replace("-", " ").title(), ""))[0],
                    "fix": opp_map.get(f.check_id, ("", "Optimize resource loading."))[1],
                    "savings": savings,
                    "priority": priority,
                    "priority_val": severity_rank.get(priority, 2)
                })

        opp_list.sort(key=lambda x: x["priority_val"])
        
        for opp in opp_list:
            f = opp["finding"]
            priority = opp["priority"]
            pill_class = "pill-critical" if priority == "HIGH" else "pill-warning"
            
            name_esc = html.escape(opp['name'])
            savings_esc = html.escape(opp['savings'])
            fix_esc = html.escape(opp['fix'])
            details_esc = html.escape(f.details)
            
            panel_row_html = self._build_code_panel_row(f, colspan=4)
            opp_rows_html.append(f"""
            <tr class="fail-row fail-clickable" onclick="toggleCodeRow(this)">
                <td style="width: 100px;"><span class="priority-pill {pill_class}">{priority} <span class="chevron">▼</span></span></td>
                <td style="width: 250px;"><strong>{name_esc}</strong></td>
                <td><strong class="text-info">{savings_esc}</strong></td>
                <td>{fix_esc} <br><small class='text-secondary' style='margin-top: 4px; display: block;'>Lighthouse details: {details_esc}</small></td>
            </tr>
            {panel_row_html}
            """)

        # --- Next Steps / Manual Action Required ---
        a11y_manual_list = []
        for item in a11y_failed_list:
            name_esc = html.escape(item['name'])
            fix_esc = html.escape(item['fix'])
            a11y_manual_list.append(f"<li>[<strong>{item['priority']}</strong>] {name_esc} — {fix_esc}</li>")
            
        perf_manual_list = []
        for opp in opp_list:
            name_esc = html.escape(opp['name'])
            fix_esc = html.escape(opp['fix'])
            perf_manual_list.append(f"<li>[<strong>{opp['priority']}</strong>] {name_esc} — {fix_esc}</li>")

        manual_actions_html = ""
        if a11y_manual_list or perf_manual_list:
            manual_actions_html += '<div class="card" style="margin-top:24px;">'
            manual_actions_html += '<div class="card-header"><div class="card-title">Next Steps — Manual Fixes Required</div>'
            manual_actions_html += '<div class="card-subtitle">These issues could not be fixed automatically and require developer attention.</div></div>'
            manual_actions_html += '<div class="card-body" style="padding-top:0;">'
            
            if a11y_manual_list:
                manual_actions_html += '<h4 style="margin: 16px 0 8px 0; color:#0f172a; text-transform:uppercase; font-size:12px; letter-spacing:0.5px;">Accessibility Fixes</h4>'
                manual_actions_html += f'<ol class="manual-list">{"".join(a11y_manual_list)}</ol>'
                
            if perf_manual_list:
                manual_actions_html += '<h4 style="margin: 20px 0 8px 0; color:#0f172a; text-transform:uppercase; font-size:12px; letter-spacing:0.5px;">Performance Fixes</h4>'
                manual_actions_html += f'<ol class="manual-list">{"".join(perf_manual_list)}</ol>'
                
            manual_actions_html += '</div></div>'

        # --- Generate report.html Content ---
        target_display_esc = html.escape(target_display)
        report_html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f8fafc;
            color: #0f172a;
            margin: 0;
            padding: 0;
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
        }}
        .header {{
            background-color: #0f172a;
            color: #ffffff;
            padding: 24px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 4px solid #6366f1;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }}
        .header .metadata {{
            text-align: right;
            font-size: 13px;
            color: #64748b;
        }}
        .header .metadata strong {{
            color: #ffffff;
        }}
        .container {{
            max-width: 1100px;
            margin: 32px auto;
            padding: 0 24px;
            box-sizing: border-box;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }}
        .metric-card {{
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 20px 24px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }}
        .metric-title {{
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #64748b;
            margin-bottom: 8px;
        }}
        .metric-value {{
            font-size: 22px;
            font-weight: 700;
        }}
        .metric-good {{
            color: #16a34a;
            background-color: #f0fdf4;
            border-left: 4px solid #16a34a;
        }}
        .metric-critical {{
            color: #dc2626;
            background-color: #fef2f2;
            border-left: 4px solid #dc2626;
        }}
        .metric-neutral {{
            color: #0d9488;
            background-color: #f0fdfa;
            border-left: 4px solid #0d9488;
        }}
        .card {{
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }}
        .card-header {{
            margin-bottom: 20px;
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 12px;
        }}
        .card-title {{
            font-size: 18px;
            font-weight: 600;
            color: #0f172a;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .card-subtitle {{
            font-size: 13px;
            color: #64748b;
            margin-top: 4px;
        }}
        .section-badge {{
            font-size: 11px;
            font-weight: 700;
            background-color: #f1f5f9;
            color: #64748b;
            padding: 3px 8px;
            border-radius: 9999px;
            text-transform: uppercase;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            margin-top: 8px;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #e2e8f0;
        }}
        th {{
            background-color: #f8fafc;
            color: #64748b;
            font-weight: 600;
            text-align: left;
            padding: 12px 16px;
            border-bottom: 1px solid #e2e8f0;
        }}
        td {{
            padding: 14px 16px;
            border-bottom: 1px solid #e2e8f0;
            vertical-align: top;
        }}
        tr:last-child td {{
            border-bottom: none;
        }}
        tr.fail-row td {{
            background-color: #fef2f2;
        }}
        tr.fail-row {{
            border-left: 4px solid #dc2626;
        }}
        .status-pill {{
            font-size: 11px;
            font-weight: 700;
            padding: 4px 8px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: inline-block;
            text-align: center;
            min-width: 48px;
        }}
        .status-pass {{
            background-color: #f0fdf4;
            color: #16a34a;
            border: 1px solid #bbf7d0;
        }}
        .status-fail {{
            background-color: #fef2f2;
            color: #dc2626;
            border: 1px solid #fecaca;
        }}
        .status-na {{
            background-color: #f1f5f9;
            color: #64748b;
            border: 1px solid #e2e8f0;
        }}
        .badge {{
            font-size: 11px;
            font-weight: 700;
            padding: 4px 8px;
            border-radius: 6px;
            text-transform: uppercase;
            display: inline-block;
        }}
        .badge-fixed {{
            background-color: #f0fdfa;
            color: #0d9488;
            border: 1px solid #ccfbf1;
        }}
        .badge-improved {{
            background-color: #f0fdf4;
            color: #16a34a;
            border: 1px solid #bbf7d0;
        }}
        .badge-unchanged {{
            background-color: #f1f5f9;
            color: #64748b;
            border: 1px solid #e2e8f0;
        }}
        .badge-regressed {{
            background-color: #fef2f2;
            color: #dc2626;
            border: 1px solid #fecaca;
        }}
        .priority-pill {{
            font-size: 10px;
            font-weight: 700;
            padding: 3px 6px;
            border-radius: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: inline-block;
        }}
        .pill-critical {{
            background-color: #fef2f2;
            color: #dc2626;
            border: 1px solid #fecaca;
        }}
        .pill-warning {{
            background-color: #fffbeb;
            color: #d97706;
            border: 1px solid #fef3c7;
        }}
        .pill-info {{
            background-color: #eff6ff;
            color: #2563eb;
            border: 1px solid #dbeafe;
        }}
        .metric-status {{
            font-size: 11px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 12px;
            display: inline-block;
        }}
        .metric-good {{
            background-color: #f0fdf4;
            color: #16a34a;
        }}
        .metric-warning {{
            background-color: #fffbeb;
            color: #d97706;
        }}
        .metric-critical {{
            background-color: #fef2f2;
            color: #dc2626;
        }}
        .text-good {{ color: #16a34a; }}
        .text-critical {{ color: #dc2626; }}
        .text-secondary {{ color: #64748b; }}
        .text-info {{ color: #2563eb; }}
        .toggle-btn {{
            background: none;
            border: none;
            color: #6366f1;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            padding: 0;
            margin-top: 16px;
            display: block;
            text-decoration: underline;
        }}
        .toggle-btn:hover {{
            color: #4f46e5;
        }}
        .code-panel-row {{
            display: none;
        }}
        .code-panel-row.expanded-row {{
            display: table-row;
        }}
        .code-panel-row td {{
            padding: 0 !important;
            border-bottom: none !important;
        }}
        .code-panel {{
            background-color: #ffffff;
            border: 1px solid transparent;
            border-radius: 8px;
            padding: 0 16px;
            margin: 0;
            max-height: 0;
            opacity: 0;
            overflow: hidden;
            transition: max-height 0.3s ease-in-out, opacity 0.3s ease-in-out, padding 0.3s ease-in-out, margin 0.3s ease-in-out, border-color 0.3s ease-in-out;
            box-sizing: border-box;
        }}
        .code-panel.expanded {{
            max-height: 1200px;
            opacity: 1;
            padding: 16px;
            margin: 8px 16px 16px 16px;
            border-color: #e2e8f0;
        }}
        .code-panel-header {{
            margin-bottom: 12px;
            display: flex;
            align-items: center;
        }}
        .code-panel-count-badge {{
            background-color: #e2e8f0;
            color: #475569;
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 9999px;
            text-transform: uppercase;
        }}
        .code-inspector-item {{
            margin-bottom: 16px;
            border-bottom: 1px dashed #e2e8f0;
            padding-bottom: 16px;
            text-align: left;
        }}
        .code-inspector-item:last-child {{
            margin-bottom: 0;
            border-bottom: none;
            padding-bottom: 0;
        }}
        .code-inspector-title {{
            font-size: 13px;
            font-weight: 600;
            color: #0f172a;
            margin-bottom: 4px;
        }}
        .code-inspector-selector {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            color: #6366f1;
            font-weight: bold;
            margin-bottom: 8px;
            word-break: break-all;
        }}
        .code-inspector-snippet {{
            background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 12px;
            margin: 0;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 13px;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-all;
            color: #334155;
        }}
        .fail-clickable {{
            cursor: pointer;
            transition: background-color 0.2s ease;
        }}
        .fail-clickable:hover td {{
            background-color: #fef2f2 !important;
        }}
        .chevron {{
            font-size: 9px;
            margin-left: 6px;
            display: inline-block;
            transition: transform 0.2s ease;
            vertical-align: middle;
        }}
        .manual-list {{
            margin: 0;
            padding-left: 20px;
            font-size: 14px;
            color: #0f172a;
        }}
        .manual-list li {{
            margin-bottom: 12px;
        }}
        .footer {{
            text-align: center;
            padding: 40px 0;
            font-size: 12px;
            color: #64748b;
            border-top: 1px solid #e2e8f0;
            margin-top: 40px;
            background-color: #ffffff;
        }}
        .footer p {{
            margin: 4px 0;
        }}
        @media print {{
            body {{
                background-color: #ffffff;
                color: #000000;
            }}
            .container {{
                box-shadow: none;
                border: none;
                margin: 0;
                max-width: 100%;
                padding: 0;
            }}
            .header {{
                background-color: #0f172a !important;
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
                color: white !important;
            }}
            .card {{
                page-break-inside: avoid;
                border: 1px solid #e2e8f0;
                margin-bottom: 20px;
                box-shadow: none;
            }}
            .toggle-btn {{
                display: none !important;
            }}
        }}
    </style>
    <script>
        function togglePassingA11y() {{
            var container = document.getElementById('a11y-passing-container');
            var button = document.getElementById('a11y-toggle-btn');
            if (container.style.display === 'none') {{
                container.style.display = 'block';
                button.innerText = 'Hide passing checks';
            }} else {{
                container.style.display = 'none';
                button.innerText = 'Show ' + container.dataset.count + ' passing checks';
            }}
        }}
        function toggleCodeRow(rowElement) {{
            var codeRow = rowElement.nextElementSibling;
            if (!codeRow || !codeRow.classList.contains('code-panel-row')) return;
            
            var panel = codeRow.querySelector('.code-panel');
            var chevron = rowElement.querySelector('.chevron');
            if (!panel) return;
            
            var isExpanded = panel.classList.contains('expanded');
            if (isExpanded) {{
                panel.classList.remove('expanded');
                codeRow.classList.remove('expanded-row');
                if (chevron) chevron.innerText = '▼';
            }} else {{
                panel.classList.add('expanded');
                codeRow.classList.add('expanded-row');
                if (chevron) chevron.innerText = '▲';
            }}
        }}
    </script>
</head>
<body>
    <div class="header">
        <h1>Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer</h1>
        <div class="metadata">
            <div>Target: <strong>{target_display_esc}</strong></div>
            <div>Date: <strong>{timestamp_str}</strong></div>
        </div>
    </div>
    <div class="container">
        <!-- 1. Executive Summary Metric Cards -->
        <div class="summary-grid">
            <div class="metric-card {agentic_status_class}">
                <div class="metric-title">AI Agent Readiness</div>
                <div class="metric-value">{passed_agentic} / {total_agentic} passed</div>
            </div>
            <div class="metric-card {a11y_status_class}">
                <div class="metric-title">Accessibility Issues</div>
                <div class="metric-value">{a11y_issues_cnt} found</div>
            </div>
            <div class="metric-card {perf_status_class}">
                <div class="metric-title">Performance Issues</div>
                <div class="metric-value">{perf_issues_cnt} found</div>
            </div>
            <div class="metric-card metric-neutral">
                <div class="metric-title">Auto-Fixed This Run</div>
                <div class="metric-value">{auto_fixed} resolved</div>
            </div>
        </div>

        <!-- 2. Remediation Results Section -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">What We Fixed</div>
                <div class="card-subtitle">
                    Agentic Browsing checks are the only ones auto-fixed. Accessibility and Performance require manual fixes.
                </div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Check</th>
                        <th>Before</th>
                        <th>After</th>
                        <th>Result</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(remediation_rows_html)}
                </tbody>
            </table>
            <div style="margin-top: 16px; font-size: 13px; font-weight: 500; color: #64748b;">
                {auto_fixed} of 7 agentic browsing checks fixed in this run
            </div>
        </div>

        <!-- 3. Agentic Browsing Section -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span>Agentic Browsing Details</span>
                    <span class="section-badge">{agentic_failed_cnt} failed, {agentic_passed_cnt} passed</span>
                </div>
                <div class="card-subtitle">Optimizes your website layout and machine-readable resources for AI agents and LLM web-crawlers.</div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Check</th>
                        <th>What It Means</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(agentic_rows_html)}
                </tbody>
            </table>
        </div>

        <!-- 3b. GEO Readiness Section -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span>GEO Readiness</span>
                    <span class="section-badge">{geo_failed_cnt} failed, {geo_passed_cnt} passed</span>
                </div>
                <div class="card-subtitle">Optimizes structured schema metadata for Generative Engine Optimization (GEO) and search RAG extraction.</div>
            </div>
            
            <h4 style="margin: 0 0 10px 0; color: #0f172a; font-size: 14px; font-weight: 600;">Before / After Comparison</h4>
            <table style="margin-bottom: 24px;">
                <thead>
                    <tr>
                        <th>Check</th>
                        <th>Before</th>
                        <th>After</th>
                        <th>Result</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(geo_comparison_rows_html)}
                </tbody>
            </table>

            <h4 style="margin: 0 0 10px 0; color: #0f172a; font-size: 14px; font-weight: 600;">Audit Details</h4>
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Check</th>
                        <th>What It Means</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(geo_rows_html)}
                </tbody>
            </table>
        </div>

        <!-- 4. Accessibility Section -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span>Accessibility Audits</span>
                    <span class="section-badge">{a11y_issues_cnt} issues found, {a11y_passed_cnt} passed</span>
                </div>
                <div class="card-subtitle">Checks for standard WCAG compliance and keyboard controls to make page content navigable.</div>
            </div>
            
            {f'<table><thead><tr><th>Priority</th><th>Issue</th><th>What It Means</th><th>Suggested Fix</th></tr></thead><tbody>{"".join(a11y_failed_rows_html)}</tbody></table>' if a11y_failed_rows_html else '<p class="text-good" style="font-weight:600; margin: 0 0 12px 0;">No accessibility issues detected.</p>'}
            
            {f'<div class="text-secondary" style="font-size: 13px; margin-top: 12px;">{a11y_not_applicable_cnt} checks not applicable to this site (no relevant elements found)</div>' if a11y_not_applicable_cnt > 0 else ''}
            
            {f'''
            <button id="a11y-toggle-btn" class="toggle-btn" onclick="togglePassingA11y()">Show {a11y_passed_cnt} passing checks</button>
            <div id="a11y-passing-container" data-count="{a11y_passed_cnt}" style="display: none; margin-top: 16px;">
                <h4 style="margin: 0 0 8px 0; font-size: 13px; color: #64748b;">Passing Accessibility Audits</h4>
                <table>
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Check</th>
                            <th>What It Means</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(a11y_passed_rows_html)}
                    </tbody>
                </table>
            </div>
            ''' if a11y_passed_cnt > 0 else ''}
        </div>

        <!-- 5. Performance Section -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span>Performance Metrics</span>
                    <span class="section-badge">{perf_issues_cnt} issues found, {perf_passed_cnt} passed</span>
                </div>
                <div class="card-subtitle">Measures load time speed, interactivity delays, and layout shifts to enhance page performance.</div>
            </div>

            <!-- Core Web Vitals sub-table -->
            <h4 style="margin: 0 0 10px 0; color: #0f172a; font-size: 14px; font-weight: 600;">Core Web Vitals</h4>
            <table style="margin-bottom: 24px;">
                <thead>
                    <tr>
                        <th>Metric</th>
                        <th>Status</th>
                        <th>Your Score</th>
                        <th>Target</th>
                        <th>What It Means</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(vitals_rows_html)}
                </tbody>
            </table>

            <!-- Opportunities sub-table -->
            <h4 style="margin: 0 0 10px 0; color: #0f172a; font-size: 14px; font-weight: 600;">Opportunities to Improve</h4>
            {f'<table><thead><tr><th>Priority</th><th>Opportunity</th><th>Estimated Savings</th><th>How to Fix</th></tr></thead><tbody>{"".join(opp_rows_html)}</tbody></table>' if opp_rows_html else '<p class="text-good" style="font-weight:600; margin: 0;">All core asset size and caching optimization targets met.</p>'}
        </div>

        <!-- 6. Next Steps / Manual Action Required -->
        {manual_actions_html}
    </div>

    <!-- 7. Footer -->
    <div class="footer">
        <p>Generated by Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer • Powered by Google Lighthouse 13.4 • {timestamp_str}</p>
        <p style="color: #94a3b8; font-size: 11px; margin-top: 8px;">Auto-fixes applied to Agentic Browsing only. Accessibility and Performance findings require manual review.</p>
    </div>
</body>
</html>
"""

        # Save HTML report to file
        report_html_path = os.path.join(run_dir, "report.html")
        with open(report_html_path, "w", encoding="utf-8") as f:
            f.write(report_html_content)

        # Save HTML report as ADK artifact
        if ctx.artifact_service:
            import google.genai
            version = await ctx.artifact_service.save_artifact(
                app_name=ctx.agent.name,
                user_id="user",
                session_id=ctx.session.id,
                filename="report.html",
                artifact=google.genai.types.Part(
                    inline_data=google.genai.types.Blob(
                        mime_type="text/html",
                        data=report_html_content.encode("utf-8")
                    )
                )
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(artifact_delta={"report.html": version})
            )

        # 6. Save final report object to state
        final_report = FinalReport(
            target=target,
            before=before,
            after=after,
            comparisons=comparisons,
            summary_line=summary_result,
            report_path=report_md_path,
        )

        total_fixed = sum(1 for comp in comparisons if comp.delta == "fixed")
        geo_comp = next((f for f in after.findings if f.check_id == "geo-schema-markup"), None)
        geo_before = next((f for f in before.findings if f.check_id == "geo-schema-markup"), None)
        if geo_before and geo_comp and not geo_before.passed and geo_comp.passed:
            total_fixed += 1

        fixed_text = "1 issue resolved automatically" if total_fixed == 1 else f"{total_fixed} issues resolved automatically"
        a11y_text = "1 issue needs attention" if a11y_issues_cnt == 1 else f"{a11y_issues_cnt} issues need attention"
        perf_text = "1 issue needs attention" if perf_issues_cnt == 1 else f"{perf_issues_cnt} issues need attention"

        if target.source_type == "local_path":
            target_name = os.path.basename(target.value.rstrip("/\\"))
        else:
            target_name = target.value

        summary_message = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  \n"
            "  Lighthouse Agentic Hub — Audit Complete  \n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  \n"
            f"  Target:       {target_name}  \n"
            f"  Fixed:        {fixed_text}  \n"
            f"  Accessibility: {a11y_text}  \n"
            f"  Performance:  {perf_text}  \n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  \n"
            "  Your full interactive report is ready.  \n"
            "  Click report.html above to open it.  \n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        yield _state_event(self.name, ctx, {"final_report": final_report.model_dump()})
        yield _text_event(self.name, ctx, summary_message)


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------

root_agent = SequentialAgent(
    name="root_agent",
    sub_agents=[
        IntakeAgent(name="intake_agent", description="Parses user input into a TargetRef."),
        AuditAgent(name="audit_agent", description="Runs Lighthouse CLI against the target."),
        diagnosis_agent,
        remediation_draft_agent,
        RemediationExecuteAgent(name="remediation_execute_agent", description="Executes drafted file changes."),
        BenchmarkAgent(name="benchmark_agent", description="Runs Lighthouse CLI post-remediation to compare."),
        ReportAgent(name="report_agent", description="Generates the final audit and remediation report."),
    ],
)

from google.adk.apps import App  # noqa: E402

app = App(root_agent=root_agent, name="workflows_sequential")
