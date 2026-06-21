"""
AI Agent Readiness Auditor & Optimizer — root agent graph.

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
                "--only-categories=agentic-browsing",
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
        cat = lh_data.get("categories", {}).get("agentic-browsing", {})
        for ref in cat.get("auditRefs", []):
            audit = lh_data.get("audits", {}).get(ref["id"], {})
            mode = audit.get("scoreDisplayMode")
            applicable = mode != "notApplicable"
            score = audit.get("score")
            passed = True if not applicable else bool(score)
            details_parts = [audit.get("title", "")]
            audit_details = audit.get("details", {})
            if isinstance(audit_details, dict) and audit_details.get("items"):
                msgs = [i.get("message", "") for i in audit_details["items"] if i.get("message")]
                if msgs:
                    details_parts.append("; ".join(msgs))
            findings.append({
                "check_id": ref["id"],
                "applicable": applicable,
                "passed": passed,
                "raw_score": score if isinstance(score, (int, float)) else None,
                "details": " — ".join(p for p in details_parts if p),
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
        - "cumulative-layout-shift" -> remediation_type="not_auto_fixable"
        - "webmcp-form-coverage", "webmcp-registered-tools",
          "webmcp-schema-validity" -> remediation_type="webmcp_suggestion_only"
        - "llms-txt" (quality) or "llms-txt-exists" (presence) failures
          -> remediation_type="llms_txt"
        - "agent-accessibility-tree" failures -> remediation_type="aria_labels"
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
    model="gemini-2.5-flash",
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

remediation_draft_agent = LlmAgent(
    name="remediation_draft_agent",
    model="gemini-2.5-flash",
    instruction=_remediation_draft_instruction,
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


# ---------------------------------------------------------------------------
# Graph wiring — STUB SHAPE ONLY (Benchmark/Report not yet
# rewritten against the confirmed API — see "Next steps" after smoke test)
# ---------------------------------------------------------------------------

root_agent = SequentialAgent(
    name="root_agent",
    sub_agents=[
        IntakeAgent(name="intake_agent", description="Parses user input into a TargetRef."),
        AuditAgent(name="audit_agent", description="Runs Lighthouse CLI against the target."),
        diagnosis_agent,
        remediation_draft_agent,
        RemediationExecuteAgent(name="remediation_execute_agent", description="Executes drafted file changes."),
    ],
)

from google.adk.apps import App  # noqa: E402

app = App(root_agent=root_agent, name="workflows_sequential")
