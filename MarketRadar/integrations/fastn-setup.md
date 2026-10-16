# Connect MarketRadar to fastn MCP

MarketRadar already implements the MCP client. These account-specific resources must be created in your fastn workspace; they are not silently mocked or predeployed.

## 1. Prepare the workspace

Create your fastn account/workspace using the workshop instructions. Connect a dedicated Google Sheet with `Briefs`, `Evidence`, `Reports`, and `Actions` tabs. Use the account's supported builder to create and publish the workflows below. Inspect contracts and test them on your demo sheet before exposing them over MCP.

Workflows are proposed application resources, not built-in fastn API names. Your gateway may expose workflows directly or through a generic execution tool. MarketRadar's editable tool names and JSON templates support either arrangement, provided they match the actual discovered schema.

## 2. Read workflow

Intended input:

```json
{"brief": {"keyword": "travel mug", "marketplace": "amazon", "country": "US", "language": "English", "date_range": "today 12-m", "product_url": "", "competitor_urls": [], "max_browser_steps": 6}}
```

Return a canonical `brief` object and optional previous evidence/context. You can look up a stored brief by its keyword or configure a `brief_id` template. Return the effective product, region, dates and language so research uses the actual loaded brief. Keep this workflow read-only.

Suggested builder request:

> Build a read-only, on-demand research loader. Accept a brief object containing a product keyword, marketplace, country, language and date range. Find matching records in my connected research Sheet's Briefs tab and optionally load prior research. Return JSON with a brief object and previous_context. Return explicit missing-record information instead of invented data. Do not write or modify any records.

## 3. Store workflow

Accept a `payload` JSON object:

```json
{
  "run_id": "unique-client-run-id",
  "report_version": 1,
  "brief": {"keyword": "travel mug", "country": "US"},
  "evidence": [],
  "report": {"summary": "...", "observations": [], "limitations": [], "recommendations": []},
  "approved_actions": []
}
```

These are abbreviated contract examples, not measured research results. Actual evidence contains source IDs/URLs, timestamps, collection status, requested/verified filters, and extracted source data.

Suggested builder request:

> Build an on-demand research store accepting a payload object with run_id, report_version, brief, evidence, report and approved_actions. Write source evidence to Evidence, the report to Reports, and only the supplied approved_actions to Actions. Preserve structured JSON in JSON-text cells where needed. Upsert by run_id plus record ID plus report_version so retrying the same request creates no duplicates. Store a full snapshot_json of the complete payload in Reports to support exact readback. Return the run_id and created/updated record references. Do not perform listing edits or create advertising campaigns.

Deduplication is a requirement to implement and test in your workflow, not an automatic guarantee from fastn. MarketRadar also prevents concurrent/repeated known-success saves and blocks retries after uncertain outcomes.

## 4. Optional readback workflow (recommended)

Accept `{"run_id": "..."}`. Find that run's Reports row, decode `snapshot_json` and return the stored full payload. Return missing data explicitly. The client verifies the exact report, evidence, and approved actions, not just the ID. This readback must retrieve the persisted row rather than echo the request or cache the last input.

## 5. Configure the actual gateway

In fastn, use Connect to copy the gateway URL and obtain the supported credential. In MarketRadar -> Connections:

1. Save the gateway and credential locally.
2. Click Discover MCP tools. Inspect returned names and input schemas.
3. Map read, store, and optional verify tool names. If the gateway uses a generic tool, include its required workflow/action identifier inside your argument template exactly as its schema requires.
4. Test the configured read workflow. This button invokes only the loader you configured.
5. Run a live research brief, review it, then explicitly save. Inspect the destination rows and execution history.

For tools whose schemas match the example contracts, the argument templates are:

```json
{"brief": "{brief}"}
```

```json
{"payload": "{payload}"}
```

```json
{"run_id": "{run_id}"}
```

Exact placeholder strings are replaced with typed values. Supported context includes `{brief}`, `{brief_id}`, `{keyword}`, `{run_id}`, `{report_version}`, `{evidence}`, `{report}`, `{approved_actions}`, and `{payload}` where applicable. They are not string-interpolated fragments: `"prefix-{run_id}"` is a literal string. For a gateway wrapper, nest the typed placeholders within the wrapper's real schema.

The browser sources use Playwright CLI. You do not need a fastn Google Ads/Trends connector or a Google Ads API integration for collection. fastn owns the brief/results/task connections in this implementation.

Official references: [MCP gateway](https://docs.fastn.ai/build/mcp-gateway), [Google Sheets integration](https://fastn.ai/integrations/google-sheets).
