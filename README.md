# Lighthouse Agentic Hub: The AI-Readiness Web Auditor & Auto-Optimizer

Audit any website for AI agent readiness, accessibility, and performance — then auto-fix what it can.

## What It Does
Lighthouse Agentic Hub is an autonomous development tool designed to audit websites and local codebases against Google Lighthouse standards and AI agent accessibility. It automatically diagnoses failures (such as missing instructions files or invalid navigation hierarchies) and writes safe, local fixes directly to the source code. For complex accessibility or performance issues, the tool provides structured diagnostic insights and step-by-step manual remediation instructions for developers.

## What It Checks
The auditor evaluates target sites across three distinct categories:
* **Agentic Browsing (4 Pillars)**:
  * **AI Navigation Structure**: Checks if interactive elements have clear accessible names for screen readers and AI agents (`agent-accessibility-tree`).
  * **WebMCP Integration**: Verifies forms are mapped to web actions and AI tools (`webmcp-form-coverage`, `webmcp-registered-tools`, `webmcp-schema-validity`).
  * **llms.txt Presence & Quality**: Evaluates whether instructions for LLMs exist and meet standard formatting and detail requirements (`llms-txt-exists`, `llms-txt`).
  * **Layout Stability**: Measures Cumulative Layout Shift (`cumulative-layout-shift`) to ensure page elements remain stable for AI browsers.
* **Accessibility (WCAG AA Compliance - Diagnosis Only)**:
  * Low contrast text, missing form labels, missing alternative image texts, tap target sizing, focus traps, and screen reader landmark errors.
* **Performance (Core Web Vitals - Diagnosis Only)**:
  * Page paint and interaction speeds (Largest Contentful Paint, First Contentful Paint, Cumulative Layout Shift, Total Blocking Time, Speed Index, Time to Interactive).

## What Gets Auto-Fixed
The system performs automated file-writing changes exclusively on local codebases. Public URLs are always treated as read-only.

| Check Category / ID | Description | Auto-Fixed? | Action Taken / Logic |
| :--- | :--- | :---: | :--- |
| **llms-txt-exists** | Missing `llms.txt` file at root | **Yes** | Created using custom AI-guided drafting skill. |
| **llms-txt** (Quality) | Quality of instructions file | **Yes** | Overwritten and updated with detailed context. |
| **agent-accessibility-tree** | Elements lacking accessible labels | **Yes** | BeautifulSoup4 injects missing `aria-label` attributes. |
| **cumulative-layout-shift** | Missing image dimensions | **No** *(Diagnosis)* | Too risky to auto-edit CSS; lists manual image fixes. |
| **webmcp-*** | Missing WebMCP action tools | **No** *(Diagnosis)* | Suggests standard integration code templates. |
| **Accessibility Audits** | WCAG AA color/label issues | **No** *(Diagnosis)* | Lists manual HTML/CSS fixes, prioritized. |
| **Performance (Vitals)** | Core Web Vitals issues | **No** *(Diagnosis)* | Lists performance optimization recommendations. |

## Architecture
The workflow pipeline executes as a deterministic sequence of 7 ADK agents. Data contracts are Pydantic models passed through invocation state.

```ascii
             [ INPUT ]
    (--path <dir>  OR  --url <url>)
                 │
                 ▼
        ┌──────────────────┐
        │   Intake Agent   │  ──► Resolves inputs into a TargetRef
        └──────────────────┘      [State Key: target]
                 │
                 ▼
        ┌──────────────────┐
        │   Audit Agent    │  ──► Runs Lighthouse CLI pre-remediation
        └──────────────────┘      [State Key: audit_result] [Tool: Lighthouse CLI]
                 │
                 ▼
        ┌──────────────────┐
        │ Diagnosis Agent  │  ──► Evaluates findings & maps fix actions
        └──────────────────┘      [State Key: diagnosis_items] [Tool: Gemini 3.1 Flash Lite]
                 │
                 ▼
        ┌──────────────────┐
        │ Remediation Draft│  ──► Generates ARIA labels & llms.txt content
        └──────────────────┘      [State Key: remediation_draft] [Tool: Gemini + SKILL]
                 │
                 ▼
        ┌──────────────────┐
        │Remediation Exec  │  ──► Applies edits to local HTML files
        └──────────────────┘      [State Key: remediation_result] [Tool: BeautifulSoup4]
                 │
                 ▼
        ┌──────────────────┐
        │ Benchmark Agent  │  ──► Runs Lighthouse CLI post-remediation
        └──────────────────┘      [State Key: after_audit_result] [Tool: Lighthouse CLI]
                 │
                 ▼
        ┌──────────────────┐
        │   Report Agent   │  ──► Compares results & outputs visual reports
        └──────────────────┘      [State Key: final_report]
                 │
                 ▼
            [ OUTPUTS ]
    (report.html, report.md, llms.txt)
```

## Prerequisites
* **Python**: 3.12+
* **Node.js**: 18+ and `npm`
* **Google AI Studio API Key**: Required for the Gemini model API calls (`GEMINI_API_KEY`)
* **uv**: Fast Python package manager (run `uv tool install google-agents-cli`)
* **Lighthouse CLI**: Globally installed: `npm install -g lighthouse`
* **Chromium / Chrome**: Installed and available in your PATH for Lighthouse headless browsing
* **Docker** *(Optional)*: For isolated container runs

## Setup
Follow these steps to set up the auditor on your local machine:

1. **Clone the Repository**:
   ```bash
   git clone <repo-url>
   cd ai-readiness-v2
   ```

2. **Install Dependencies**:
   Configure Python dependencies and Virtual Environment with `uv`:
   ```bash
   uv sync
   ```

3. **Configure Environment Variables**:
   Copy the example environment file and insert your API key:
   ```bash
   cp workflows_sequential/.env.example workflows_sequential/.env
   # Open workflows_sequential/.env and fill in GEMINI_API_KEY
   ```

4. **Install Lighthouse CLI**:
   Ensure Node.js is installed, then run:
   ```bash
   npm install -g lighthouse
   ```

5. **Verify Installation**:
   Ensure `lighthouse` and `chrome` are accessible:
   ```bash
   lighthouse --version
   ```

## How to Run

Launch the interactive CLI dashboard:
```bash
uv run agents-cli playground
```

### Local Path Audit & Remediation
To audit and automatically fix a local web project directory:
```bash
--path sandbox/luminary-site
```
*This will run audits, draft files, inject missing ARIA attributes, and write the modifications to disk.*

### Live URL Audit (Read-Only)
To audit a public web page without making any changes to files:
```bash
--url https://google.com
```
*This performs read-only audits and outputs detailed diagnostic reports without file system modifications.*

## Demo Site
We have prepared a sample website inside `sandbox/luminary-site/` (a high-fidelity product portal for the fictional hardware vendor **Luminary**). It contains intentional accessibility and instructions gaps to showcase the auditor's capabilities.

To reset the sandbox site back to its baseline broken state (e.g. before recording a demo or running a new audit):
```bash
git checkout sandbox/luminary-site/index.html
rm -f sandbox/luminary-site/llms.txt
```

## Project Structure
* `workflows_sequential/agent.py`: Root workflow sequential agent wiring, custom agent classes (`IntakeAgent`, `AuditAgent`, `BenchmarkAgent`, `RemediationExecuteAgent`, `ReportAgent`), and report HTML template generation.
* `workflows_sequential/models.py`: Shared Pydantic data models passed between agents.
* `workflows_sequential/skills/llms-txt-drafting/SKILL.md`: Standalone skill guidelines used by the LLM agent to format valid `llms.txt` files.
* `pyproject.toml`: Project dependencies and ruff lint configurations.
* `SPEC.md`: Software specifications and ADK version constraints.
* `docs/architecture.png`: High-resolution architecture layout.

## Security Rules

This project implements multiple layers of security:

**Pipeline Security (agent.py):**
- Read-Only URL Targets: Live URLs are strictly read-only. 
  RemediationExecuteAgent enforces this in code — if the 
  target is a URL, every remediation action is forced to 
  skipped_unsafe regardless of what the LLM drafted
- No Destructive Overwrites: The tool only inserts missing 
  ARIA attributes and creates missing llms.txt files. It 
  never overwrites existing non-empty files
- No Secrets Committed: The .env file containing API keys 
  is explicitly added to .gitignore and never staged or pushed

**MCP Server Security (mcp_server.py):**
- Path Traversal Guard: All local path targets are resolved 
  to absolute paths and verified to reside within the 
  workspace root. Attempts to audit outside paths 
  (e.g. /etc, ../../) are immediately rejected with a 
  security error — verified with automated tests
- URL Scheme Validation: Only http:// and https:// schemes 
  are accepted. file://, ftp://, and other schemes are rejected
- Command Injection Prevention: All subprocesses use 
  shell=False with clean argument lists — shell metacharacters 
  in inputs cannot execute arbitrary commands
- Read-Only Enforcement: The MCP server only exposes the 
  audit_web_readiness tool. No file writing or remediation 
  capabilities are exposed through the MCP interface

## Connect via MCP

Any MCP-compatible IDE (Claude Desktop, Cursor, Windsurf) 
can connect to the Lighthouse Agentic Hub auditor directly.

Add this to your MCP client configuration:

```json
{
  "mcpServers": {
    "lighthouse-agentic-hub": {
      "command": "uv",
      "args": ["run", "python", "-m", 
               "workflows_sequential.mcp_server"],
      "cwd": "/path/to/ai-readiness-v2"
    }
  }
}
```

Replace /path/to/ai-readiness-v2 with your local clone path.

Once connected, the tool audit_web_readiness is available 
to any agent or LLM in your IDE:
- audit_web_readiness("sandbox/luminary-site") — local audit
- audit_web_readiness("https://yoursite.com") — live audit

## Course Concepts Demonstrated
This project demonstrates several advanced patterns from the Google Agent Development Kit (ADK):

| Concept | Implementation Location | Purpose |
| :--- | :--- | :--- |
| **SequentialAgent** | [workflows_sequential/agent.py](file:///path/to/ai-readiness-v2/workflows_sequential/agent.py#L2081-L2092) | Chains multiple custom Python and LLM agents in a strict execution graph. |
| **LlmAgent** | [workflows_sequential/agent.py](file:///path/to/ai-readiness-v2/workflows_sequential/agent.py#L425-L432) | Defines LLM steps utilizing system instructions and Pydantic structured output mapping. |
| **BaseAgent subclassing**| [workflows_sequential/agent.py](file:///path/to/ai-readiness-v2/workflows_sequential/agent.py#L95-L101) | Subclasses `BaseAgent` to build custom deterministic agent nodes. |
| **Shared State Delta** | [workflows_sequential/agent.py](file:///path/to/ai-readiness-v2/workflows_sequential/agent.py#L82-L88) | Passes serializable data contracts between agents using SQLite-backed session states. |
| **SkillToolset** | [workflows_sequential/agent.py](file:///path/to/ai-readiness-v2/workflows_sequential/agent.py#L484-L508) | Attaches external skill guideline packages to the LLM agent tool calls. |

## License
This project is licensed under the MIT License.
