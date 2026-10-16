# MarketRadar

Product research using Google Keyword Planner, Google Trends, Amazon.com features/reviews, and a human-reviewed report. The local demo is at http://127.0.0.1:8877.

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
| `agents.py` | Defines the DeepAgents orchestrator, two specialists and their LangChain tools. Start here to understand the agent architecture. |
| `browser.py` | Model-free Playwright tools: Google observations/actions and scripted Amazon collection. |
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
         finalize_report(report) -> validate citations and end the graph
    -> save the report -> human review
```

`create_deep_agent()` builds the LangGraph graph and its model/tool loop. The main agent delegates with DeepAgents' `task` tool. Its Google specialist owns Keyword Planner and Trends; its Amazon specialist chooses searches and calls the fixed scraper. Each specialist has only its own tools. Requests run sequentially because the browser session is shared.

LangChain's `@tool` defines typed callable tools; LangGraph carries messages, tool calls and results between turns. `ResearchChatModel.bind_tools()` exposes tool schemas to the existing JSON provider and turns its response into a LangChain `AIMessage`. LangGraph executes those calls. The Codex subprocess itself has tools disabled. No handwritten LLM loop remains in `browser.py`.

The orchestrator reads canonical tool evidence before finalizing. Subagent summaries cannot replace source records. `finalize_report` validates the existing fixed report format against real evidence/review IDs. A validation error returns correction feedback, with at most three submissions; a successful submission ends the graph before another model call. Small LangChain middleware requires tool use until the assigned sources have been attempted. Built-in shell/filesystem tools and the general-purpose subagent are disabled; the graph uses an in-memory state backend.

Amazon uses a different approach: Python inserts validated query/ASIN data into the checked-in JavaScript template and runs it through Playwright CLI. The model never writes Amazon selectors or browser code. Collection is bounded to two searches, three products, and twelve sampled reviews per product; review bodies are limited to 1,200 characters.

All agents share one provider and a total cap of thirty model calls. Google gets at most six action decisions per source. The graph recursion limit is fifty. Cancellation reaches the provider and browser tools through the run's stop event. Sample mode bypasses the graph and model entirely. Dependencies are pinned in `requirements.txt` to the versions used by this refactor.

Framework references: [DeepAgents subagents](https://docs.langchain.com/oss/python/deepagents/subagents), [customization](https://docs.langchain.com/oss/python/deepagents/customization), [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview).

## Report and evidence

Every report contains summary, observations, products with demand signals, complaints, requested features, potential market gaps, follow-ups and limitations. Buyer-feedback findings cite actual collected review IDs. Gaps are hypotheses for human validation. JSON export includes the sampled product features and reviews.

Google Trends measures relative interest, not sales. Keyword Planner competition describes Google advertising. Amazon's bought-in-past-month labels are recorded as displayed. Reviews can pool variants/countries and are not a representative survey. Google country/language/date selections do not change Amazon delivery settings or filter its reviews. Unverified filters and missing sources remain disclosed.

The DeepAgents live run on 16 October 2026 completed with all three sources live: four Keyword Planner rows, 53 weekly Trends points, three Amazon products and 36 sampled reviews. It used fifteen model turns and produced a validated report with two follow-ups. Source-filter limitations remain disclosed. All 111 tests pass; the graph fixture also verifies delegation, real tool execution, citation correction and termination without another model call.

## Supporting files

- `.data/` holds private local history and settings; `.release/` is the separate publishing checkout with its Git history. Neither is application code to explain in the demo or include in a container.
- `cloud_config.py` handles owner/demo access. `deploy/` contains the container, protected cloud browser viewer and deployment instructions. Keep these for Alternate Clouds deployment; see [the deployment guide](deploy/README.md).
- `fastn.py` and `settings.py` support the existing optional fastn connection. It is off by default, does not participate in the current demo, and is documented in [the integration guide](integrations/fastn-setup.md). Recording a human review writes only local history; the separate copy button performs an optional fastn write.
- `package_release.py` copies allowlisted source files into `.release/`; it is a publishing utility, not part of the agent loop.
- `tests/` contains development checks and is excluded from the production image.

Local model selection defaults to `gpt-6-luna`; `MARKETRADAR_CODEX_MODEL` can override it. Cloud inference uses `AF_API_KEY`, `AF_ORG_ID` and optional `MARKETRADAR_CLOUD_MODEL`. Local Codex sign-in is never copied to the cloud. Cloud Google sign-in and authenticated inference still require live verification.
