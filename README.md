# MarketRadar

Product research using Google Keyword Planner, Google Trends, Amazon.com features/reviews, and a human-reviewed report. The local demo is at http://127.0.0.1:8877.

Cloud demo: [MarketRadar](https://alternate-stormy-portal-764-docker-app.alternatefutures.ai). Owner/viewer credentials are in the private local `.data/cloud-login.txt`; credentials and browser profiles are excluded from Git and container images.

## Start

Run from the parent project directory:

```powershell
.\MarketRadar\start.ps1
# Equivalent:
.\.venv\Scripts\python.exe -m MarketRadar --port 8877
```

The local app uses the existing Python environment, Node.js, Playwright CLI installation and signed-in Codex CLI. Attach Chrome from Connections and complete Google sign-in manually. Sample mode uses labeled synthetic evidence and makes no browser or model calls.

## Read the code in this order

| File | Responsibility |
| --- | --- |
| `__main__.py` | Starts `server.main()`. |
| `server.py` | Serves the dashboard/API; a research POST calls `Radar.start()`. |
| `core.py` | Manages background jobs, report validation, history and human review. |
| `agents.py` | Defines the DeepAgents orchestrator, three specialists and their LangChain tools. Start here to understand the agent architecture. |
| `browser.py` | Model-free Playwright tools: Google observations/actions and scripted Amazon/supplier collection. |
| `amazon_research.js` | Fixed Playwright script: search Amazon, read products, features and embedded reviews. No LLM calls. |
| `models.py` | Local Codex subscription provider plus `ResearchChatModel`, the LangChain model adapter. |
| `cloud_model.py` | Cloud `CloudModel.complete_json()` provider adapter. |
| `static/` | HTML, CSS and JavaScript for forms, progress, reports and history. |

The parent `models.py` contains `get_research_model()`, which selects the local or cloud provider. Evidence preparation and citation validation reuse helpers from the original `subagents/amazon_market_agent_v2.py`. The orchestrator now produces findings and follow-ups in one validated report.

## Main workflow and LLM calls

```text
Dashboard -> server.py -> Radar.start() -> background thread -> Radar._execute()
    -> agents.run_research() creates a DeepAgents orchestrator
         task("google-demand") -> open_google / google_step tools
         task("amazon-reviews") -> research_amazon tool -> fixed Playwright script
         read_evidence() -> canonical source records and reviews
         finalize_report(report) -> validate citations and save the report
         approve_product(...) -> pause for human approval or research feedback
         task("supplier-scout") -> find_suppliers -> fixed Playwright script
         contact_supplier(...) -> pause for exact-message approval or feedback
    -> approved email delivery or manual contact draft -> save history
```

`create_deep_agent()` builds the LangGraph model/tool loop. The main agent delegates with `task`: Google owns Keyword Planner and Trends, Amazon chooses searches and calls the fixed scraper, and supplier-scout reads public supplier profiles after product approval. Each specialist has only its own tools. Requests run sequentially because the browser session is shared.

## Human and agent workflow

Live research pauses at `approve_product` using DeepAgents' `interrupt_on`. The dashboard can approve/edit the selected product and requirements, reject it, or send feedback and a new keyword back into research. A revision keeps the old report/evidence in `report_history` and creates a new report version. Completed live reports also offer **Review this research with the agent**, reusing the original capture dates until a revision is requested.

After product approval, supplier-scout runs a fixed Playwright script over public Made-in-China supplier profiles. Displayed audit/license badges are source observations, not our guarantee of identity, certificates, quality or suitability. The main agent proposes one inquiry and pauses again at `contact_supplier`. The human can edit its exact subject/body, request another draft, or stop. Recipients come from observed supplier evidence and cannot be replaced by the model.

`Radar.review()` validates the pending interrupt ID and report version, records the human decision, and resumes with LangGraph `Command(resume=...)`. `SqliteSaver` stores messages and pending approvals in `.data/checkpoints.sqlite`; existing JSON files retain readable reports, source records and decision history. Waiting approvals survive a server restart. One persistent `/data` volume is sufficient for the single-service demo; a separate database server is unnecessary. A new provider without the original volume does not retain the workspace.

Email delivery is optional: set `MARKETRADAR_SMTP_HOST`, `MARKETRADAR_SMTP_PORT` (default 587 with STARTTLS), `MARKETRADAR_SMTP_FROM`, and optional `MARKETRADAR_SMTP_USER` / `MARKETRADAR_SMTP_PASSWORD` through runtime secrets. Without email configuration or an observed email, the approved draft offers a manual email/contact-page handoff. Sending/unknown outcomes are recorded before allowing further activity; there is no automatic resend. No supplier messages were sent during implementation checks.

LangChain's `@tool` defines typed callable tools; LangGraph carries messages, tool calls and results between turns. `ResearchChatModel.bind_tools()` exposes tool schemas to the existing JSON provider and turns its response into a LangChain `AIMessage`. LangGraph executes those calls. The Codex subprocess itself has tools disabled. No handwritten LLM loop remains in `browser.py`.

The orchestrator reads canonical tool evidence before finalizing. Subagent summaries cannot replace source records. `finalize_report` validates the fixed report format against real evidence/review IDs. A validation error returns correction feedback, with at most three submissions; a successful submission advances to human product review. LangChain middleware exposes only the next valid orchestrator action and blocks delegation to the wrong specialist. Unavailable sources count as completed attempts and remain disclosed. Built-in shell/filesystem tools and the general-purpose subagent are disabled; StateBackend keeps agent files in graph state, while SqliteSaver persists checkpoints.

Amazon uses a different approach: Python inserts validated query/ASIN data into the checked-in JavaScript template and runs it through Playwright CLI. The model never writes Amazon selectors or browser code. Collection is bounded to two searches, three products, and twelve sampled reviews per product; review bodies are limited to 1,200 characters.

All agents share one provider and a cap of thirty model calls per execution or human-approved continuation. Google gets at most six action decisions per source. The graph recursion limit is fifty. Cancellation reaches the provider and browser tools through the run's stop event. Sample mode bypasses the graph and model entirely. Dependencies are pinned in `requirements.txt` to the versions used by this refactor.

Framework references: [DeepAgents subagents](https://docs.langchain.com/oss/python/deepagents/subagents), [customization](https://docs.langchain.com/oss/python/deepagents/customization), [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview).

## Report and evidence

Every report contains summary, observations, products with demand signals, complaints, requested features, potential market gaps, follow-ups and limitations. Buyer-feedback findings cite actual collected review IDs. Gaps are hypotheses for human validation. JSON export includes the sampled product features and reviews.

Google Trends measures relative interest, not sales. Keyword Planner competition describes Google advertising. Amazon's bought-in-past-month labels are recorded as displayed. Reviews can pool variants/countries and are not a representative survey. Google country/language/date selections do not change Amazon delivery settings or filter its reviews. Unverified filters and missing sources remain disclosed.

The earlier DeepAgents live run on 16 October 2026 completed with all three sources live: four Keyword Planner rows, 53 weekly Trends points, three Amazon products and 36 sampled reviews. Source-filter limitations remain disclosed. Checks cover native graph pause/resume with a reopened SQLite connection, feedback-driven recollection, exact message edits and stale approval rejection. Supplier collection read three real profiles and two displayed audit badges. This saved research and its human product approval were transferred privately to the cloud, retaining original capture dates; the cloud agent generated a fresh inquiry and paused for message approval. No supplier was contacted.

The deployed image `a0967ed` passed public health, owner/viewer access boundaries, the owner-only live VNC WebSocket, and real cloud inference. A fresh cloud run for water bottle covers collected three Amazon products and reached native product review. It disclosed missing reviews, Google Ads sign-in and a Trends rate limit. The labeled synthetic walkthrough also passed; it is separate from real evidence. Configured email delivery remains untested.

## Supporting files

- `.data/` holds private local history and settings; `.release/` is the separate publishing checkout with its Git history. Neither is application code to explain in the demo or include in a container.
- `cloud_config.py` handles owner/demo access. `deploy/` contains the container, protected cloud browser viewer and deployment instructions. Keep these for Alternate Clouds deployment; see [the deployment guide](MarketRadar/deploy/README.md).
- `fastn.py` and `settings.py` support the existing optional fastn connection. It is off by default, does not participate in the current demo, and is documented in [the integration guide](MarketRadar/integrations/fastn-setup.md). Recording a human review writes only local history; the separate copy button performs an optional fastn write.
- `package_release.py` copies allowlisted source files into `.release/`; it is a publishing utility, not part of the agent loop.
- `tests/` contains development checks and is excluded from the production image.

Local model selection defaults to `gpt-6-luna`; `MARKETRADAR_CODEX_MODEL` can override it. Cloud inference uses `AF_API_KEY`, `AF_ORG_ID` and optional `MARKETRADAR_CLOUD_MODEL`; authenticated inference has succeeded inside the deployment. Local Codex sign-in is never copied to the cloud. Sign in to Google Ads manually through the owner's cloud browser before new Keyword Planner collection.
