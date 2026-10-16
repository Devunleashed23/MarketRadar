# MarketRadar

A product research workspace combining **Playwright CLI**, the original Amazon research/listing agents, and a saved human review. Research covers Google Keyword Planner, Google Trends, and public Amazon, eBay or Etsy pages. **fastn MCP is optional.**

Local inference uses your signed-in Codex subscription. Cloud inference explicitly uses Alternate Clouds credentials and organization credits; your local Codex login is never copied into a deployment. The cloud service includes its own Chromium browser and an owner-only browser viewer for manual sign-in.

## Start

From the parent project directory in PowerShell:

```powershell
.\MarketRadar\start.ps1
```

Open <http://127.0.0.1:8877>. An equivalent command is:

```powershell
.\.venv\Scripts\python.exe -m MarketRadar --port 8877
```

The launcher uses the existing project virtual environment and Node/Playwright CLI installation. To prepare a different Python environment, install `MarketRadar/requirements.txt`. If the parent JavaScript dependencies are missing, run `npm install` in the parent project. The app deliberately uses Playwright CLI, not Playwright MCP.

## What works without setup

Choose **Explore a sample** to exercise the complete local interface and history. The sample is synthetic, uses no model allowance or external connections, and cannot be saved to fastn. It does not demonstrate live research.

The Codex CLI on this computer was already authenticated with ChatGPT. One real JSON smoke call succeeded during implementation. That is a provider connection check, not a completed market-research run.

## Required for live research

1. **Codex:** keep the official CLI signed in using ChatGPT. Check with `codex login status`; run `codex login` if needed. MarketRadar's root `models.py` factory selects the subscription adapter. It does not read OAuth tokens or fall back to an API key. Calls consume your shared Codex allowance. A run permits at most 20 turns, including browser decisions, and stops/cancels subprocesses within configured time bounds. Even small turns carry model/system overhead, so use sample mode when editing the UI.
2. **Browser:** install/enable the Playwright extension in Chrome, then use Connections -> Attach browser. Open Keyword Planner and complete any Google login, account setup or verification manually. No passwords or browser authentication state are copied into MarketRadar. Existing browser connection tokens can be supplied in the environment; no token is embedded in the new source.
3. **Google Keyword Planner:** the browser account needs access to the tool. Google's current help documents completed billing setup as a prerequisite for basic features. The agent only researches: it does not change billing, create campaigns, or add keywords to campaigns. Browser automation does not bypass access requirements.
4. **Optional fastn:** connect the read/store workflows described in [the integration guide](integrations/fastn-setup.md) if you want workspace enrichment and an external copy. Use the actual gateway and tool schemas from your account. No fastn workspace is needed for research, local review or JSON export.

The fastn option is off by default. The current Agentika handbook requires Alternate Clouds deployment and evaluates MCP where appropriate; it does not mandate fastn.

## Research workflow

```text
User chooses product, marketplace and research filters
    -> optional fastn MCP: load previous context
    -> Playwright CLI: Keyword Planner, Trends, selected marketplace pages
    -> preserve ranges, source settings and collection evidence
    -> original amazon_market_agent_v2.py: grounded analysis
    -> original listing_agent_v2.py: up to three source-linked follow-ups
    -> human accepts/rejects follow-ups and records a rationale
    -> save decision history and export report
    -> optional fastn MCP: store reviewed report
    -> optional readback verifies the exact stored contents
```

The browser loop uses observed page refs, bounded steps, and allowlisted research actions. Only one run owns the shared `marketradar` session at a time. Login, account setup, CAPTCHA, empty results and extraction failures have visible statuses. Source settings distinguish requested filters from verified filters. Unverified settings must be resolved before treating results as geographically comparable.

Trends is relative search interest, not sales. Planner volume ranges stay ranges; advertiser competition is not marketplace seller competition. Marketplace listings are a small observed sample. Recommendations cite source IDs; model interpretation is not an independently verified market fact. Never use a synthetic sample or source failure as evidence of demand.

## fastn connection behavior

The app uses the real MCP Python SDK and Streamable HTTP. It discovers tools and their input schemas. No invented fastn tool names are hardcoded. Map your actual workflow/tool names and JSON arguments in Connections. Whole-value placeholders preserve JSON object/array types; see the integration guide.

Discovery and the configured read test do not invoke the store workflow. Configure the loader as a read-only action. **Record review** only persists human decisions; it never executes suggested actions. A separate **Copy reviewed report to fastn** performs the optional external write and requires the same recorded action selection. Save results distinguish verified readback, successful-but-unverified writes, and uncertain outcomes. An uncertain write is not automatically retried. Your store workflow must deduplicate by run ID/version.

The app shows a successful write as **unverified** until the optional verify tool retrieves matching run ID, report, evidence and approved actions. A matching identifier alone is insufficient.

## Files and configuration

- `models.py`: local subscription adapter; `cloud_model.py`: Alternate Clouds inference adapter. The **parent** `models.py` selects one explicitly and retains the separate legacy Google factory.
- `browser.py`: Playwright CLI collector and source validation.
- `fastn.py`: real MCP discovery, calls, argument templates, credential redaction and readback checks.
- `core.py`: run state, calls to the two original agents, report validation, human review and persistence.
- `server.py`, `cloud_config.py`, `static/`: local or authenticated cloud API and responsive interface.
- `deploy/`: container, browser viewer, launch checks and deployment instructions.
- `.data/`: ignored local settings, run history and QA screenshots. The local gateway credential is stored here if entered in the form; keep this directory private. It is never returned in API responses.

Optional environment configuration:

```powershell
$env:MARKETRADAR_CODEX_MODEL = 'gpt-6-luna'
$env:MARKETRADAR_FASTN_URL = 'https://mcp.fastn.dev'
# MARKETRADAR_FASTN_API_KEY can supply the credential instead of the form.
# Use the actual gateway from your account; the example is not a promise about your tenant.
```

Environment credential overrides take precedence over the saved form value. No global Codex configuration is changed. Local mode binds only to localhost. Cloud mode requires an explicit HTTPS public URL and separate owner/demo passwords; manual browser control and integration configuration are restricted to the owner. This is one shared research workspace, with one active browser job, rather than a multi-tenant service.

## Cloud deployment and demo

See [deployment instructions](deploy/README.md). Judges open the HTTPS dashboard and use the dedicated demo account. Playwright CLI and Chromium run inside the service; no application or browser extension is required on the judges' computers. The owner opens the protected browser viewer to sign in to Google manually before the demo. Cloud sessions do not inherit the laptop's Google login.

The container must pass its build and runtime smoke checks before it is called deployable. Google account access, a successful real research run, persistent-volume behavior and authenticated Alternate Clouds inference also need live verification. Sample mode stays visibly synthetic and is useful for explaining the interface, not proving live demand.

## Tests and verification

From the parent directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s MarketRadar\tests -v
node --check MarketRadar\static\app.js
```

With MarketRadar running, the browser smoke test uses an isolated browser for this local UI only:

```powershell
node node_modules/@playwright/cli/playwright-cli.js -s=marketradar-qa open http://127.0.0.1:8877 --browser=chrome
node node_modules/@playwright/cli/playwright-cli.js -s=marketradar-qa run-code --filename=MarketRadar/tests/browser_smoke.js
```

The local collector attaches to the user's existing browser. A complete live run on 16 October 2026 collected four Keyword Planner rows, 53 weekly Trends points and three eBay listings, then produced three source-linked follow-ups through the original agents. No source data was synthesized. Planner retained the account's existing research filters; requested filters that were not confirmed remain explicitly unverified. Cloud inference, cloud Google sign-in and external fastn workflows still require account-specific verification. The real localhost MCP fixture proves protocol handling, not a live fastn connection.

Sources: [official Codex noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode), [Codex authentication](https://learn.chatgpt.com/docs/auth), [fastn MCP gateway](https://docs.fastn.ai/build/mcp-gateway), [Google Keyword Planner](https://support.google.com/google-ads/answer/7337243?hl=en), [Google Trends data](https://support.google.com/trends/answer/4365533?hl=en).
