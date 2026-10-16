import tempfile
import threading
import unittest
from pathlib import Path

from MarketRadar.browser import BrowserCollector, BrowserError, safe_marketplace_url


BRIEF = {
    "keyword": "travel mug & flask", "marketplace": "amazon", "country": "US",
    "language": "English", "date_range": "today 12-m", "max_browser_steps": 2,
}


class FakeModel:
    def __init__(self, response):
        self.response = response

    def complete_json(self, system, prompt, **kwargs):
        return self.response


class FakeBrowser(BrowserCollector):
    """A CLI boundary double: real navigation must never run in unit tests."""
    def __init__(self, root, snapshots):
        super().__init__()
        self.root = Path(root)
        self.snapshots = snapshots
        self.commands = []
        self.current_url = "https://ads.google.com/aw/keywordplanner/home"

    def _cli(self, args, stop=None, timeout=45):
        self.commands.append(args)
        if args[0] == "tab-new":
            self.current_url = args[1]
        if args[0] == "tab-list":
            return "- 0: (current) research"
        source = "keyword_planner" if "ads.google.com" in self.current_url else (
            "google_trends" if "trends.google.com" in self.current_url else "marketplace"
        )
        snapshot = self.root / ".playwright-cli" / "page.yml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(self.snapshots.get(source, '- heading "No results"'), encoding="utf-8")
        return f"### Page\n- Page URL: {self.current_url}\n### Snapshot\n[Snapshot](.playwright-cli/page.yml)"


class BrowserTests(unittest.TestCase):
    def test_rejects_non_public_and_deceptive_marketplace_urls(self):
        for url in ["http://www.amazon.com/dp/123", "https://amazon.com.evil.test/dp/123",
                    "https://sellercentral.amazon.com/", "https://www.amazon.com/ap/signin",
                    "https://user:pass@www.ebay.com/itm/123", "https://127.0.0.1/",
                    "https://www.etsy.com/your/account", "https://www.amazon.com:9443/dp/123"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_marketplace_url(url)
        self.assertEqual(safe_marketplace_url("https://www.amazon.com/dp/B123?tag=tracking#top"),
                         "https://www.amazon.com/dp/B123")

    def test_source_links_encode_keyword_and_keep_trends_settings(self):
        collector = BrowserCollector()
        url = collector.source_url("google_trends", BRIEF)
        self.assertIn("q=travel+mug+%26+flask", url)
        self.assertIn("geo=US", url)
        self.assertIn("date=today+12-m", url)
        with self.assertRaises(ValueError):
            collector.source_url("arbitrary_command", BRIEF)

    def test_blocks_campaign_action_even_if_model_requests_it(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {"keyword_planner": '- button "Create campaign" [ref=e3]'})
            evidence = collector.collect(BRIEF, FakeModel({"action": "click", "ref": "e3"}))
        self.assertEqual(evidence[0]["status"], "failed")
        self.assertFalse(any(command[0] == "click" for command in collector.commands))

    def test_preserves_observed_keyword_ranges_and_drops_invented_metrics(self):
        snapshot = '- row "travel mug 1K – 10K High $0.50 – $1.25" [ref=e9]'
        response = {"action": "done", "data": {"keywords": [
            {"keyword": "travel mug", "volume": "1K – 10K", "competition": "High", "bid": "$0.50 – $1.25", "currency": "USD"},
            {"keyword": "travel mug", "volume": "9999999", "competition": "High"},
            {"keyword": "imaginary product", "volume": "1K – 10K"},
        ]}}
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {"keyword_planner": snapshot})
            evidence = collector.collect(BRIEF, FakeModel(response))
        rows = evidence[0]["data"]["keywords"]
        self.assertEqual(evidence[0]["status"], "live")
        self.assertEqual(rows[0]["volume"], "1K – 10K")
        self.assertIsNone(rows[1]["volume"])
        self.assertEqual(len(rows), 2)
        self.assertIn("captured_at", evidence[0])

    def test_keyword_metrics_cannot_be_borrowed_from_another_observed_row(self):
        snapshot = ('- row "travel mug 1K Low $0.50" [ref=e1]\n'
                    '- row "fishing rod 9K High $9.00" [ref=e2]')
        data = BrowserCollector._extract({"keywords": [{"keyword": "travel mug", "volume": "9K", "competition": "High", "bid": "$9.00"}]},
                                        "keyword_planner", snapshot, "https://ads.google.com/aw/keywordplanner/home")
        self.assertIsNone(data["keywords"][0]["volume"])
        self.assertIsNone(data["keywords"][0]["competition"])
        self.assertIsNone(data["keywords"][0]["bid"])

    def test_nested_keyword_cells_are_associated_with_their_parent_row(self):
        snapshot = ('- row [ref=e1]:\n  - cell "travel mug"\n  - cell "1K – 10K"\n  - cell "Low"\n'
                    '- row [ref=e2]:\n  - cell "fishing rod"\n  - cell "9K"\n  - cell "High"')
        data = BrowserCollector._extract({"keywords": [{"keyword": "travel mug", "volume": "1K – 10K", "competition": "Low"}]},
                                        "keyword_planner", snapshot, "https://ads.google.com/aw/keywordplanner/home")
        self.assertEqual(data["keywords"][0]["volume"], "1K – 10K")
        self.assertEqual(data["keywords"][0]["competition"], "Low")

    def test_snapshot_trend_values_must_belong_to_the_same_date_and_term(self):
        snapshot = '- row "2026-10-04 travel mug 20"\n- row "2026-10-11 tumbler 90"'
        data = BrowserCollector._extract({"trends": [{"date": "2026-10-04", "term": "travel mug", "value": "90"}]},
                                        "google_trends", snapshot, "https://trends.google.com/trends/explore")
        self.assertEqual(data["trends"], [])

    def test_model_notes_must_be_observed_source_text(self):
        data = BrowserCollector._extract({"notes": ["Demand doubled", "No data available"]}, "keyword_planner",
                                        '- heading "No data available"', "https://ads.google.com/aw/keywordplanner/home")
        self.assertEqual(data["notes"], ["No data available"])

    def test_rate_limit_is_explicit_and_does_not_use_a_model_call(self):
        class NoModel:
            def complete_json(self, *args, **kwargs):
                raise AssertionError("A rate-limited or empty source must not consume model quota.")

        for marker in ["Too Many Requests", "Error429", "Error 429", "HTTP 429"]:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as root:
                collector = FakeBrowser(root, {"google_trends": f'- heading "{marker}"'})
                result = collector._collect_source('google_trends', BRIEF,
                    collector.source_url('google_trends', BRIEF), NoModel(), None)
                self.assertEqual(result["status"], "rate_limited")

    def test_marketplace_price_cannot_be_borrowed_from_another_listing(self):
        snapshot = ('- listitem:\n  - heading "Travel Mug"\n  - link "Travel Mug":\n'
                    '    - /url: https://www.amazon.com/dp/B123\n  - generic "$12.00"\n'
                    '- listitem:\n  - heading "Fishing Rod"\n  - generic "$99.00"')
        data = BrowserCollector._extract({"competitors": [{"title": "Travel Mug", "url": "https://www.amazon.com/dp/B123", "price": "$99.00"}]},
                                        "marketplace", snapshot, "https://www.amazon.com/s?k=travel+mug")
        self.assertEqual(len(data["competitors"]), 1)
        self.assertIsNone(data["competitors"][0]["price"])

    def test_login_and_setup_are_reported_without_fabricated_success(self):
        for snapshot, expected in [('- heading "Sign in to Google Ads"', "login_required"),
                                   ('- heading "Set up your first campaign"', "account_setup_required"),
                                   ('- heading "Verify you are human"', "challenge")]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                collector = FakeBrowser(root, {"keyword_planner": snapshot})
                result = collector.collect(BRIEF, FakeModel({"action": "done", "status": "live", "data": {}}))
                self.assertEqual(result[0]["status"], expected)
                self.assertEqual(result[0]["data"]["keywords"], [])

    def test_rejects_model_code_and_unobserved_refs(self):
        for action in [{"action": "eval", "value": "document.cookie"}, {"action": "click", "ref": "e999"}]:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as root:
                collector = FakeBrowser(root, {"keyword_planner": '- button "Get results" [ref=e1]'})
                evidence = collector.collect(BRIEF, FakeModel(action))
                self.assertEqual(evidence[0]["status"], "failed")
                self.assertFalse(any(c[0] in ("eval", "run-code", "click") for c in collector.commands))

    def test_empty_extraction_is_never_marked_live(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {"keyword_planner": '- heading "Keyword Planner"'})
            result = collector.collect(BRIEF, FakeModel({"action": "done", "status": "live", "data": {}}))
        self.assertEqual(result[0]["status"], "no_results")

    def test_marketplace_pages_cannot_trigger_click_or_search_actions(self):
        collector = BrowserCollector()
        for action in [{"action": "click", "ref": "e1"},
                       {"action": "fill", "ref": "e1", "value": BRIEF["keyword"]}]:
            with self.subTest(action=action), self.assertRaises(BrowserError):
                collector._action_args(action, "marketplace", '- textbox "Search" [ref=e1]', BRIEF, None)

    def test_amazon_navigation_uses_one_planning_call_and_fixed_script(self):
        class CountingModel(FakeModel):
            calls = 0

            def complete_json(self, *args, **kwargs):
                self.calls += 1
                return self.response

        class ScriptBrowser(FakeBrowser):
            def _collect_source(self, source, *args, **kwargs):
                return {'id': source, 'source': source, 'status': 'no_results', 'data': {}}

            def _amazon_page(self, config, stop=None):
                self.commands.append(config)
                if config['action'] == 'search':
                    return {'status': 'live', 'notes': [], 'products': [
                        {'asin': 'B000000001', 'title': 'Alpha insulated travel cup black'},
                        {'asin': 'B000000002', 'title': 'Alpha insulated travel cup white'},
                        {'asin': 'B000000003', 'title': 'Beta vacuum travel mug'},
                        {'asin': 'B000000004', 'title': 'Gamma coffee flask cup'}]}
                return {'status': 'live', 'notes': [], 'products': [
                    {'asin': config['asin'], 'title': 'Observed product', 'features': ['Steel body'],
                     'reviews': [{'id': config['asin'] + '-review', 'body': 'Lid leaked.'}]}]}

        model = CountingModel({'searches': ['leakproof flask']})
        with tempfile.TemporaryDirectory() as root:
            browser = ScriptBrowser(root, {})
            result = browser.collect(BRIEF, model)
        self.assertEqual(len(result), 3)
        self.assertEqual(model.calls, 1)
        actions = [command for command in browser.commands if isinstance(command, dict)]
        self.assertEqual([action['action'] for action in actions], ['search', 'search', 'product', 'product', 'product'])
        self.assertEqual([action['asin'] for action in actions[2:]], ['B000000001', 'B000000003', 'B000000004'])
        self.assertEqual(result[-1]['status'], 'live')
        self.assertEqual(len(result[-1]['data']['products']), 3)

    def test_requested_settings_are_not_claimed_verified_for_planner(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {"keyword_planner": '- heading "Keyword Planner"'})
            result = collector.collect(BRIEF, FakeModel({"action": "done", "data": {}}))
        self.assertEqual(result[0]["settings_verified"], {"country": False, "language": False, "date_range": False})
        self.assertTrue(result[1]["settings_verified"]["country"])

    def test_trends_csv_preserves_comparison_metadata_zero_and_less_than_one(self):
        # Representative Google Trends UI CSV layout; these are test fixtures, never live fallback data.
        content = ('\ufeffCategory: All categories\n\nWeek,travel mug: (United States),tumbler: (United States)\n'
                   '2026-09-27,<1,0\n2026-10-04,100,42\n')
        data = BrowserCollector.parse_csv("google_trends", content)
        self.assertEqual(data["trends"][0], {"date": "2026-09-27", "term": "travel mug", "value": "<1", "series_label": "travel mug: (United States)"})
        self.assertEqual(data["trends"][1]["value"], "0")
        self.assertEqual(data["csv_context"]["comparison_terms"], ["travel mug: (United States)", "tumbler: (United States)"])
        self.assertEqual(data["csv_context"]["preamble"], ["Category: All categories"])

    def test_trends_csv_rejects_unrelated_downloads_and_does_not_invent_rows(self):
        with self.assertRaises(BrowserError):
            BrowserCollector.parse_csv("google_trends", "Country,Interest\nUnited States,100\n")
        data = BrowserCollector.parse_csv("google_trends", "Week,travel mug: (United States)\n2026-10-04,\n2026-10-11,999\n")
        self.assertEqual(data["trends"], [])

    def test_planner_export_preserves_volume_and_bid_ranges(self):
        content = ('Keyword Stats 2026-10-16\nKeyword,Currency,Avg. monthly searches,Competition,Top of page bid (low range),Top of page bid (high range)\n'
                   'travel mug,USD,1K – 10K,High,0.50,1.25\n')
        data = BrowserCollector.parse_csv("keyword_planner", content)
        self.assertEqual(data["keywords"][0]["volume"], "1K – 10K")
        self.assertEqual(data["keywords"][0]["bid"], "0.50 – 1.25")
        self.assertEqual(data["keywords"][0]["currency"], "USD")

    def test_download_is_limited_to_observed_time_chart_controls(self):
        snapshot = ('- heading "Interest over time"\n- button "Download CSV" [ref=e1]\n'
                    '- heading "Interest by subregion"\n- button "Download CSV" [ref=e2]\n')
        collector = BrowserCollector()
        self.assertTrue(collector._download_allowed("google_trends", snapshot, "e1"))
        self.assertFalse(collector._download_allowed("google_trends", snapshot, "e2"))
        self.assertFalse(collector._download_allowed("marketplace", snapshot, "e1"))
        self.assertFalse(collector._download_allowed("google_trends", snapshot, "e999"))

    def test_browser_download_parses_only_the_scoped_cli_csv_and_keeps_a_copy(self):
        class DownloadBrowser(FakeBrowser):
            def _cli(self, args, **kwargs):
                output = super()._cli(args, **kwargs)
                if args[0] == "click":
                    path = self.root / ".playwright-cli" / "multiTimeline.csv"
                    path.write_text("Week,travel mug: (United States)\n2026-10-04,0\n", encoding="utf-8")
                    output += '\n### Events\n- Downloaded file multiTimeline.csv to ".playwright-cli/multiTimeline.csv"'
                return output

        snapshot = '- heading "Interest over time"\n- button "Download CSV" [ref=e1]'
        with tempfile.TemporaryDirectory() as root:
            collector = DownloadBrowser(root, {"google_trends": snapshot})
            collector.current_url = collector.source_url("google_trends", BRIEF)
            data, artifact = collector._download_csv("google_trends", snapshot, "e1")
            self.assertEqual(data["trends"][0]["value"], "0")
            self.assertTrue((Path(root) / artifact["path"]).is_file())
            self.assertTrue(artifact["path"].startswith(".playwright-cli/marketradar-downloads/"))

    def test_download_pending_state_survives_a_snapshot_without_new_events(self):
        class PendingDownloadBrowser(FakeBrowser):
            snapshots_taken = 0

            def _cli(self, args, **kwargs):
                output = super()._cli(args, **kwargs)
                if args[0] == "click":
                    return output + '\n- Downloading file multiTimeline.csv ...'
                if args[0] == "snapshot":
                    self.snapshots_taken += 1
                    if self.snapshots_taken == 2:
                        path = self.root / ".playwright-cli" / "multiTimeline.csv"
                        path.write_text("Week,travel mug: (United States)\n2026-10-04,0\n", encoding="utf-8")
                        return output + '\n- Downloaded file multiTimeline.csv to ".playwright-cli/multiTimeline.csv"'
                return output

        snapshot = '- heading "Interest over time"\n- button "Download CSV" [ref=e1]'
        with tempfile.TemporaryDirectory() as root:
            collector = PendingDownloadBrowser(root, {"google_trends": snapshot})
            collector.current_url = collector.source_url("google_trends", BRIEF)
            data, _ = collector._download_csv("google_trends", snapshot, "e1")
            self.assertEqual(data["trends"][0]["value"], "0")
            self.assertEqual(collector.snapshots_taken, 2)

    def test_stop_interrupts_pending_download_before_another_browser_poll(self):
        stop = threading.Event()

        class StoppedDownloadBrowser(FakeBrowser):
            def _cli(self, args, **kwargs):
                output = super()._cli(args, **kwargs)
                if args[0] == "click":
                    stop.set()
                    return output + '\n- Downloading file multiTimeline.csv ...'
                return output

        snapshot = '- heading "Interest over time"\n- button "Download CSV" [ref=e1]'
        with tempfile.TemporaryDirectory() as root:
            collector = StoppedDownloadBrowser(root, {"google_trends": snapshot})
            with self.assertRaisesRegex(BrowserError, "Stopped"):
                collector._download_csv("google_trends", snapshot, "e1", stop=stop)
            self.assertFalse(any(command[0] == "snapshot" for command in collector.commands))

    def test_download_rejects_files_outside_browser_output_folder(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {})
            with self.assertRaises(BrowserError):
                collector._read_download(Path(root) / "private.csv")

    def test_collection_uses_real_browser_csv_when_chart_has_no_numeric_snapshot(self):
        class DownloadBrowser(FakeBrowser):
            def _cli(self, args, **kwargs):
                output = super()._cli(args, **kwargs)
                if args[0] == "click" and "trends.google.com" in self.current_url:
                    path = self.root / ".playwright-cli" / "multiTimeline.csv"
                    path.write_text("Week,travel mug: (United States)\n2026-10-04,<1\n", encoding="utf-8")
                    output += '\n- Downloaded file multiTimeline.csv to ".playwright-cli/multiTimeline.csv"'
                return output

        snapshot = '- heading "Interest over time"\n- button "Download CSV" [ref=e1]'
        with tempfile.TemporaryDirectory() as root:
            collector = DownloadBrowser(root, {"google_trends": snapshot})
            result = collector.collect(BRIEF, FakeModel({"action": "done", "data": {}}))
            self.assertEqual(result[1]["status"], "live")
            self.assertEqual(result[1]["data"]["trends"][0]["value"], "<1")
            self.assertIn("sha256", result[1]["download"])

    def test_location_filter_accepts_the_requested_country_name(self):
        collector = BrowserCollector()
        args, _ = collector._action_args({"action": "fill", "ref": "e1", "value": "United States"},
            "keyword_planner", '- textbox "Add a location" [ref=e1]', BRIEF, None)
        self.assertEqual(args, ["fill", "e1", "United States"])

    def test_stop_prevents_browser_commands(self):
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {})
            result = collector.collect(BRIEF, FakeModel({"action": "snapshot"}), stop=stop)
        self.assertEqual(collector.commands, [])
        self.assertTrue(all(row["status"] == "failed" for row in result))

    def test_snapshot_reader_rejects_paths_outside_cli_output_folder(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {})
            outside = Path(root) / "secret.yml"
            outside.write_text("private", encoding="utf-8")
            with self.assertRaises(BrowserError):
                collector._snapshot_text(f"[Snapshot]({outside})")

    def test_step_budget_prevents_unbounded_browser_loop(self):
        with tempfile.TemporaryDirectory() as root:
            collector = FakeBrowser(root, {"keyword_planner": '- heading "Keyword Planner"'})
            result = collector.collect(BRIEF, FakeModel({"action": "snapshot"}))
        snapshots = [c for c in collector.commands if c[0] == "snapshot"]
        self.assertLessEqual(len(snapshots), BRIEF["max_browser_steps"])
        self.assertEqual(result[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
