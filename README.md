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
| `core.py` | Runs the workflow in `Radar._execute()`, validates reports, saves history and human review. |
| `browser.py` | Collects Google evidence and plans/runs the Amazon collection. |
| `amazon_research.js` | Fixed Playwright script: search Amazon, read products, features and embedded reviews. No LLM calls. |
| `models.py` | Local `CodexModel.complete_json()` provider adapter. |
| `cloud_model.py` | Cloud `CloudModel.complete_json()` provider adapter. |
| `static/` | HTML, CSS and JavaScript for forms, progress, reports and history. |

The parent `models.py` contains `get_research_model()`, which selects the local or cloud adapter. The two original files in the parent `subagents/` folder supply `analyze_market_evidence()` and `suggest_marketplace_opportunities()`. Their older DeepAgents runners are not used by this dashboard.

## Main workflow and LLM calls

```text
Dashboard -> server.py -> Radar.start() -> background thread -> Radar._execute()
    1. Create the selected model adapter.
    2. BrowserCollector.collect():
         Google Planner/Trends: observe -> model decision -> validate -> browser action.
         Amazon: one model call chooses an optional alternative search.
         Fixed Amazon script collects products, features and reviews.
    3. Original research agent: one model call produces the structured findings.
    4. Original listing agent: one model call suggests up to three follow-ups.
    5. Validate source/review citations, save the report, await human review.
```

This is a staged Python workflow. The repeating agent decision loop is the Google collector's `for step in range(budget)` in `browser.py`. It sends the current page snapshot to `model.complete_json()`, validates the returned action, runs Playwright CLI, then observes the updated page. A source stops on extraction, access problems, cancellation or its step limit.

Amazon uses a different approach: Python inserts validated query/ASIN data into the checked-in JavaScript template and runs it through Playwright CLI. The model never writes Amazon selectors or browser code. Collection is bounded to two searches, three products, and twelve sampled reviews per product; review bodies are limited to 1,200 characters.

The analysis and opportunity stages share the same model object. Local calls invoke the official Codex CLI with tools disabled and parse its JSON response. Cloud calls use Alternate Clouds inference. A live run allows at most twenty model calls in total; Google gets at most six decision steps per source. Sample mode bypasses the model entirely.

## Report and evidence

Every report contains summary, observations, products with demand signals, complaints, requested features, potential market gaps, follow-ups and limitations. Buyer-feedback findings cite actual collected review IDs. Gaps are hypotheses for human validation. JSON export includes the sampled product features and reviews.

Google Trends measures relative interest, not sales. Keyword Planner competition describes Google advertising. Amazon's bought-in-past-month labels are recorded as displayed. Reviews can pool variants/countries and are not a representative survey. Google country/language/date selections do not change Amazon delivery settings or filter its reviews. Unverified filters and missing sources remain disclosed.

The latest local Amazon workflow completed on 16 October 2026 with four Keyword Planner rows, 53 weekly Trends points, three Amazon products and 36 sampled reviews. It returned three products with demand signals, five complaints, two explicit feature requests, three gap hypotheses and three follow-ups.

## Supporting files

- `.data/` holds private local history and settings; `.release/` is the separate publishing checkout with its Git history. Neither is application code to explain in the demo or include in a container.
- `cloud_config.py` handles owner/demo access. `deploy/` contains the container, protected cloud browser viewer and deployment instructions. Keep these for Alternate Clouds deployment; see [the deployment guide](MarketRadar/deploy/README.md).
- `fastn.py` and `settings.py` support the existing optional fastn connection. It is off by default, does not participate in the current demo, and is documented in [the integration guide](MarketRadar/integrations/fastn-setup.md). Recording a human review writes only local history; the separate copy button performs an optional fastn write.
- `package_release.py` copies allowlisted source files into `.release/`; it is a publishing utility, not part of the agent loop.
- `tests/` contains development checks and is excluded from the production image.

Local model selection defaults to `gpt-6-luna`; `MARKETRADAR_CODEX_MODEL` can override it. Cloud inference uses `AF_API_KEY`, `AF_ORG_ID` and optional `MARKETRADAR_CLOUD_MODEL`. Local Codex sign-in is never copied to the cloud. Cloud Google sign-in and authenticated inference still require live verification.
