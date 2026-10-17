# MarketRadar

Human-guided product research: combine Amazon customer feedback with Google search demand, identify product gaps, and research suppliers after a person approves the direction.

[Live Alternate Clouds app](https://alternate-stormy-portal-764-docker-app.alternatefutures.ai/#research)

## What it does

Enter a product or niche. A DeepAgents orchestrator delegates Google Keyword Planner and Trends to one specialist, and Amazon features and reviews to another. The model chooses search terms; fixed Playwright CLI scripts collect Amazon and supplier pages. The report contains demand signals, complaints, requested features, possible market gaps, source citations and collection limitations.

The workflow pauses for human product approval. A person can approve or edit the requirements, reject the direction, or send feedback and a new keyword back to the agent. After approval, a supplier specialist reads public supplier profiles. The orchestrator proposes an inquiry and pauses again so a person can inspect and edit its exact recipient, subject and message.

Supplier audit badges are observations from source pages, not independent guarantees. Email delivery is optional; without configured email, approved inquiries remain drafts with manual contact links. No outreach runs without human approval.

## Architecture

```text
Dashboard -> server.py -> Radar.start() -> agents.run_research()
    -> DeepAgents orchestrator / LangGraph model-and-tool loop
         task("google-demand") -> Keyword Planner + Trends tools
         task("amazon-reviews") -> fixed Amazon Playwright script
         read_evidence() -> finalize_report() -> approve_product() pause
         task("supplier-scout") -> fixed supplier Playwright script
         contact_supplier() pause -> approved delivery or manual draft
```

LangChain defines the typed tools and model adapter. DeepAgents builds the orchestrator and its three specialists; LangGraph resumes human decisions with `Command(resume=...)`. The specialists run sequentially because they share one browser. The orchestrator must read canonical evidence before finalizing; report validation rejects unsupported citations. Unavailable sources remain disclosed.

| File | Responsibility |
| --- | --- |
| `server.py` | Dashboard API, access controls and research requests. |
| `core.py` | Background jobs, report validation, saved history and human decisions. |
| `agents.py` | DeepAgents orchestrator, specialists, tools and approval gates. |
| `browser.py` | Playwright CLI commands and bounded source collection. |
| `amazon_research.js` | Fixed Amazon search/product/review scraper. |
| `models.py` | LangChain chat adapter used by the agent loop. |
| `cloud_model.py` | Alternate Futures inference requests and model call limits. |
| `cloud_config.py` | Owner/demo authentication and deployment configuration. |
| `static/` | Dashboard HTML, CSS and JavaScript. |
| `deploy/` | Container, browser viewer, startup and deployment checks. |

The parent `models.py` exposes the provider factory. Evidence preparation and citation validation reuse the original `subagents/amazon_market_agent_v2.py` helpers.

## Run locally

From the repository root, install `MarketRadar/requirements.txt` in a Python environment and install Playwright CLI with `npm install --no-save --package-lock=false @playwright/cli@0.1.18`. Set `AF_API_KEY` and `AF_ORG_ID` privately in your environment, then run:

```powershell
.\MarketRadar\start.ps1 --port 8877
# Or: python -m MarketRadar --port 8877
```

Open http://127.0.0.1:8877. Local research uses the same Alternate Futures inference provider as the cloud deployment, defaulting to `gpt-5.6-luna`; `MARKETRADAR_CLOUD_MODEL` overrides the model. Attach a compatible Playwright CLI browser session from Connections. Complete source sign-ins manually. Sample mode is explicitly synthetic and makes no browser or model calls.

For the hosted app, use the owner's protected cloud browser from Connections. That browser has separate logins from local Chrome. Amazon reviews may require sign-in; Google Ads needs an account with Keyword Planner access. Verification pages require human action, and Trends may rate-limit cloud traffic. Missing access never becomes invented evidence.

## Storage and access

Run JSON files retain reports, observations and decision history. LangGraph's `SqliteSaver` retains messages and pending approvals. Local history defaults to `~/.marketradar/runs`, outside the source tree; `MARKETRADAR_DATA_DIR` can override it. In the cloud, these live under `/data/runs`; the browser profile lives under `/data/profile`. One persistent `/data` volume and one service replica are sufficient for this demo. Back up this volume before replacing a deployment because a new provider may not retain it.

The `owner` role runs live research, controls the browser and resumes agent approvals. The `demo` role explores saved reports, exports JSON and runs labeled samples. Passwords and inference credentials belong in runtime secrets, never Git. See [the deployment guide](deploy/README.md).

Local history and private operational backups stay outside the source folder under `~/.marketradar/`. `package_release.py` copies allowlisted source into `~/.marketradar/repository`; it is not part of the agent loop. Browser sessions, caches, environment files and private state are excluded from publication. Development tests are excluded from the running container.

Google Trends represents relative interest rather than sales. Keyword Planner competition describes advertising competition. Amazon purchase labels are recorded as displayed, and sampled reviews may combine variants or countries. Market gaps remain hypotheses for human validation.
