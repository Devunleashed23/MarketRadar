"""Playwright CLI collection: Google UI actions and scripted Amazon research."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import uuid4


class BrowserError(RuntimeError):
    pass


_PUBLIC_DOMAINS = {"amazon.com"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_FORBIDDEN = re.compile(
    r"campaign|billing|payment|budget|publish|purchase|checkout|cart|\bbuy\b|"
    r"place order|delete|remove|password|sign in|log in|sign up|account|"
    r"create (?!keyword)|save|add to|subscribe|start now", re.I
)
_PLANNER_BUTTONS = re.compile(
    r"keyword planner|discover new keywords|keyword ideas|get results|see results|"
    r"get search volume|start with keywords|search|filter|language|location|date|"
    r"month|year|cancel|close|done|accept all|reject all|essential|tools|planning", re.I
)
_TRENDS_BUTTONS = re.compile(
    r"interest over time|related quer|related topic|compare|table|list|view|explore|"
    r"search|filter|language|location|country|region|date|past |time|category|"
    r"download|cancel|close|done|accept all|reject all|essential", re.I
)
_FIELDS = re.compile(r"keyword|search|products|services|location|country|region|language|date|time|range|period", re.I)
_ACTIONS = {"click", "fill", "press", "select", "scroll", "snapshot", "download", "done", "needs_login"}
_STATUSES = {"live", "login_required", "account_setup_required", "browser_setup_required", "challenge", "rate_limited", "no_results", "failed"}
_COUNTRY_NAMES = {"US": ("United States", "United States of America"), "GB": ("United Kingdom",),
                  "PK": ("Pakistan",), "IN": ("India",), "CA": ("Canada",), "AU": ("Australia",),
                  "AE": ("United Arab Emirates",), "DE": ("Germany",), "FR": ("France",),
                  "SA": ("Saudi Arabia",), "JP": ("Japan",)}
_MAX_CSV_BYTES = 2_000_000


def safe_marketplace_url(raw: str) -> str:
    """Allow public product/search pages only; discard tracking parameters."""
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("Use a public Amazon.com product/search URL.")
    parts = urlsplit(raw.strip())
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    try:
        bad_port = parts.port not in (None, 443)
    except ValueError as exc:
        raise ValueError("Invalid marketplace URL.") from exc
    if parts.scheme != "https" or bare not in _PUBLIC_DOMAINS or parts.username or parts.password or bad_port:
        raise ValueError("Use a public HTTPS Amazon.com URL.")
    path = parts.path or "/"
    allowed = re.search(r"/(?:dp|gp/product)/[^/]+", path) or path == "/s"
    if not allowed or re.search(r"%(?:2f|5c)|\\", path, re.I):
        raise ValueError("Only public marketplace listing and search pages are supported.")
    # User-provided URLs are product pages; searches are generated internally.
    return urlunsplit(("https", host, path, "", ""))


def _stopped(stop) -> bool:
    return bool(stop and (stop.is_set() if hasattr(stop, "is_set") else stop()))


def _grounded(value, snapshot: str):
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    value = str(value).strip()[:300]
    normalize = lambda text: re.sub(r"\s+", "", str(text).replace("–", "-").replace("—", "-")).casefold()
    return value if value and normalize(value) in normalize(snapshot) else None


_NUMBER_SPAN = re.compile(r'(?<![\d.,])(?:[$€£₹¥]\s*)?\d[\d,.]*(?:\s*[KMBkmb%])?'
                          r'(?:\s*[-–—]\s*(?:[$€£₹¥]\s*)?\d[\d,.]*(?:\s*[KMBkmb%])?)?'
                          r'(?![\d.,KMBkmb%])')


def _grounded_metric(value, snapshot):
    """A metric must retain a whole observed numeral or range, including its units."""
    grounded = _grounded(value, snapshot)
    if grounded is None:
        return None
    normalize = lambda text: re.sub(r'\s+', '', text.replace('–', '-').replace('—', '-')).casefold()
    proposed = [normalize(match.group()) for match in _NUMBER_SPAN.finditer(grounded)]
    if not proposed:
        return grounded
    observed = [normalize(match.group()) for match in _NUMBER_SPAN.finditer(snapshot)]
    for number in proposed:
        # Currency symbols may be omitted when a separate currency field is used;
        # numeric digits, magnitude suffixes and both range endpoints must match.
        if not any(number == item or (number[0].isdigit() and number == re.sub(r'[$€£₹¥]', '', item)) for item in observed):
            return None
    return grounded


def _entity_blocks(snapshot: str, entity) -> list[str]:
    """Find the entity's nearest row/listitem, never the entire page/table."""
    if not isinstance(entity, str) or not entity.strip():
        return []
    lines = snapshot.splitlines()
    structured, fallback = [], []
    for index, line in enumerate(lines):
        if not _grounded(entity, line):
            continue
        parent = None
        ancestor_indent = len(line) - len(line.lstrip()) + 1
        for candidate in range(index, -1, -1):
            current = lines[candidate]
            if not current.strip():
                continue
            indent = len(current) - len(current.lstrip())
            if indent >= ancestor_indent:
                continue
            ancestor_indent = indent
            if re.match(r"\s*- (?:row|listitem)(?:\s|:|$)", current):
                parent = candidate
                break
        if parent is None:
            fallback.append(line)
            continue
        indent = len(lines[parent]) - len(lines[parent].lstrip())
        end = parent + 1
        while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
            end += 1
        block = "\n".join(lines[parent:end])
        if block not in structured:
            structured.append(block)
    return structured or fallback


def _entity_block(snapshot, entity, proposed_values=()):
    blocks = _entity_blocks(snapshot, entity)
    return max(blocks, key=lambda block: sum(_grounded(value, block) is not None for value in proposed_values), default="")


def _redact(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email redacted]", text)
    return re.sub(r"(?i)([?&](?:token|api_key|key|secret|code|authorization)=)[^\s&\)\"]+", r"\1[redacted]", text)


class BrowserCollector:
    def __init__(self, session="marketradar", emit=None):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", session):
            raise ValueError("Invalid browser session name.")
        self.session = session
        self.emit = emit
        self._google = None
        application = Path(__file__).resolve().parents[1]
        self.root = Path(os.getenv('MARKETRADAR_BROWSER_WORK_DIR', str(application)))
        self.cli = application / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
        self.mode = os.getenv('MARKETRADAR_BROWSER_MODE', 'extension')
        if self.mode not in ('extension', 'headed', 'headless'):
            raise ValueError('Choose extension, headed or headless browser mode.')
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(session, threading.RLock())

    def _event(self, source, status, message):
        if self.emit:
            self.emit({"type": "browser", "source": source, "status": status, "message": message})

    def _environment(self):
        # The browser subprocess must never inherit model or dashboard secrets.
        allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'DISPLAY',
                   'XAUTHORITY', 'TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'SystemRoot',
                   'WINDIR', 'COMSPEC', 'PATHEXT', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE',
                   'PLAYWRIGHT_BROWSERS_PATH', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME',
                   'XDG_RUNTIME_DIR', 'NODE_EXTRA_CA_CERTS'}
        if self.mode == 'extension':
            allowed.add('PLAYWRIGHT_MCP_EXTENSION_TOKEN')
        return {key: value for key, value in os.environ.items() if key in allowed}

    def _cli(self, args, stop=None, timeout=45):
        if _stopped(stop):
            raise BrowserError("Stopped by user.")
        node = shutil.which("node")
        if not node or not self.cli.is_file():
            raise BrowserError("The project's Playwright CLI or Node.js is not installed.")
        command = [node, str(self.cli), f"-s={self.session}", *args]
        process = subprocess.Popen(
            command, cwd=str(self.root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", shell=False,
            env=self._environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                output, _ = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if _stopped(stop) or time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise BrowserError("Stopped by user." if _stopped(stop) else "The browser command timed out.")
        if process.returncode or '### Error' in output:
            # CLI errors can contain extension URLs or account details. Keep them local.
            detail = re.search(r'### Error\s*\n(.*?)(?=\n### |\Z)', output, re.S)
            if detail:
                safe = re.sub(r'(?:https?|wss?)://\S+', '[url]', _redact(detail.group(1)))[:700]
                raise BrowserError('Playwright CLI: ' + safe)
            raise BrowserError("Playwright CLI could not complete this action. Check the visible browser tab.")
        return output[:2_000_000]

    def attach(self) -> dict:
        """Attach local Chrome, or launch the container's own persistent Chromium."""
        with self._lock:
            try:
                if self.mode == 'extension':
                    self._cli(["attach", "--extension=chrome"], timeout=60)
                else:
                    self.root.mkdir(parents=True, exist_ok=True)
                    profile = Path(os.getenv('MARKETRADAR_BROWSER_PROFILE', str(self.root / 'profile')))
                    profile.mkdir(parents=True, exist_ok=True)
                    config = self.root / 'managed-browser.json'
                    config.write_text(json.dumps({'browser': {
                        'browserName': 'chromium', 'isolated': False, 'userDataDir': str(profile),
                        'launchOptions': {'headless': self.mode == 'headless', 'channel': 'chromium', 'chromiumSandbox': True},
                        'contextOptions': {'viewport': {'width': 1365, 'height': 850}}
                    }}), encoding='utf-8')
                    self._cli(['open', 'about:blank', '--config', str(config)], timeout=60)
                return {"status": "connected", "connected": True, "session": self.session,
                        "mode": self.mode, "message": "Chrome is attached through the Playwright extension."
                        if self.mode == 'extension' else 'The managed cloud Chromium browser is running.'}
            except BrowserError as exc:
                return {"status": "attachment_required", "connected": False, "session": self.session,
                        "message": str(exc)}

    def status(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {'status': 'busy', 'connected': None, 'busy': True, 'session': self.session,
                    'mode': self.mode, 'message': 'An active research run owns the browser session.'}
        try:
            try:
                self._cli(["tab-list"], timeout=10)
                return {"status": "connected", "connected": True, "session": self.session}
            except BrowserError:
                return {"status": "attachment_required", "connected": False, "session": self.session,
                        "mode": self.mode, "message": "Attach Chrome using its Playwright extension. Sign in manually where needed."
                        if self.mode == 'extension' else 'Start the managed browser, then sign in through the owner browser viewer.'}
        finally:
            self._lock.release()

    def source_url(self, source: str, brief: dict) -> str:
        keyword = str(brief.get("keyword", "")).strip()[:200]
        if not keyword:
            raise ValueError("A research keyword is required.")
        if source == "keyword_planner":
            return "https://ads.google.com/aw/keywordplanner/home"
        if source == "google_trends":
            country = str(brief.get("country", "US")).upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError("Country must be a two-letter code.")
            date_range = str(brief.get("date_range", "today 12-m"))[:50]
            return "https://trends.google.com/trends/explore?" + urlencode({"q": keyword, "geo": country, "date": date_range})
        if source == "marketplace":
            urls = [brief.get("product_url"), *(brief.get("competitor_urls") or [])]
            if any(urls):
                return safe_marketplace_url(next(url for url in urls if url))
            return "https://www.amazon.com/s?" + urlencode({"k": keyword})
        raise ValueError("Unknown research source.")

    def open_source(self, source: str, brief: dict) -> dict:
        with self._lock:
            try:
                url = self.source_url(source, brief)
                self._cli(["tab-new", url])
                return {"status": "opened", "source": source, "url": url,
                        "message": "Source opened. Complete sign-in or account setup manually if requested."}
            except (BrowserError, ValueError) as exc:
                return {"status": "failed", "source": source, "message": str(exc)}

    def _snapshot_text(self, output: str) -> str:
        inline = re.search(r'### Snapshot\s*\n```(?:yaml|yml)\s*\n(.*?)\n```', output, re.S)
        if inline:
            snapshot = inline.group(1)
            if len(snapshot) > 2_000_000:
                raise BrowserError('Browser snapshot is too large.')
        else:
            matches = re.findall(r"\[Snapshot\]\(([^)]+\.(?:yml|yaml))\)", output)
            if not matches:
                raise BrowserError("Playwright CLI did not return a readable snapshot.")
            candidate = Path(matches[-1])
            path = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
            allowed = (self.root / ".playwright-cli").resolve()
            if not allowed.is_relative_to(self.root.resolve()) or not path.is_relative_to(allowed) or not path.is_file() or path.stat().st_size > 2_000_000:
                raise BrowserError("Browser snapshot path is outside the CLI output folder or too large.")
            snapshot = path.read_text(encoding="utf-8", errors="replace")
        snapshot = _redact(snapshot)
        # Long ad/tracking URLs can fill the context before any product appears.
        # Retain the observed destination and refs; navigation uses refs, not URLs.
        snapshot = re.sub(r'(https?://[^\s?"<>]+)\?[^\s"<>]+', r'\1', snapshot)
        main = re.search(r'^\s*- main(?:\s|:|$)', snapshot, re.M)
        if len(snapshot) > 60_000 and main:
            return snapshot[:5000] + '\n' + snapshot[main.start():main.start() + 55_000]
        return snapshot[:60_000]

    @staticmethod
    def _page_url(output: str, fallback: str) -> str:
        match = re.search(r"^- Page URL:\s*(https?://\S+)", output, re.M)
        return match.group(1) if match else fallback

    @staticmethod
    def _page_status(source, url, snapshot):
        text = snapshot.casefold()
        if re.search(r"too many requests|\b(?:error|http)\s*429\b", text):
            return "rate_limited"
        if any(marker in text for marker in ("captcha", "verify you are human", "unusual traffic", "robot check", "automated queries", "verify you're human")):
            return "challenge"
        if source == "keyword_planner":
            if re.search(r'(?:heading|dialog)[^\n]*turn off ad blockers[^\n]*\[ref=e\d+\]', text):
                return 'browser_setup_required'
            if urlsplit(url).hostname == "accounts.google.com" or any(marker in text for marker in ("sign in to google ads", "sign in to your google account", "sign in – google accounts", "sign in - google accounts")):
                return "login_required"
            if any(marker in text for marker in ("set up your first campaign", "create your first campaign", "complete your account setup", "add billing information", "create a google ads account", "finish setting up")):
                return "account_setup_required"
            if "/home" == urlsplit(url).path.rstrip("/") and "sign in" in text:
                return "login_required"
        if any(marker in text for marker in ("no results", "no data available", "not enough data", "don't have enough data")):
            return "no_results"
        return None

    @staticmethod
    def _allowed_location(source, url):
        host = (urlsplit(url).hostname or "").lower()
        if source == "keyword_planner":
            return host in {"ads.google.com", "accounts.google.com"}
        if source == "google_trends":
            return host == "trends.google.com"
        return False

    @staticmethod
    def _ref_line(snapshot, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"e\d+", ref):
            raise BrowserError("The model requested an invalid browser ref.")
        for line in snapshot.splitlines():
            if re.search(r"\[ref=" + re.escape(ref) + r"\]", line):
                return line
        raise BrowserError("The model requested a ref that was not observed in this snapshot.")

    def _download_allowed(self, source, snapshot, ref):
        try:
            line = self._ref_line(snapshot, ref)
        except BrowserError:
            return False
        if source not in {"google_trends", "keyword_planner"} or _FORBIDDEN.search(line):
            return False
        if not re.search(r'\b(button|link|menuitem)\b.*(?:download|export|csv)', line, re.I):
            return False
        if source == "keyword_planner":
            return bool(re.search(r"keyword ideas|avg\. monthly searches|average monthly searches", snapshot, re.I))
        # Trends has separate CSV buttons for time series, regions, and related queries.
        # Only the time-series chart belongs in the numeric trend data.
        preceding = snapshot[:snapshot.index(line) + len(line)]
        charts = re.findall(r"interest over time|interest by subregion|interest by region|related topics|related queries", preceding, re.I)
        return bool(charts and charts[-1].casefold() == "interest over time")

    def _find_download(self, source, snapshot):
        for ref in re.findall(r"\[ref=(e\d+)\]", snapshot):
            if self._download_allowed(source, snapshot, ref):
                return ref
        return None

    @staticmethod
    def _trends_table(snapshot, brief, url):
        """Read Google's accessible chart table when available; no pixel estimates."""
        term = str(brief.get('keyword', ''))
        if parse_qs(urlsplit(url).query).get('q') != [term] or not _grounded(term, snapshot):
            return []
        sections = re.split('interest over time', snapshot, maxsplit=1, flags=re.I)
        if len(sections) < 2:
            return []
        section = re.split(r'Interest by (?:subregion|region)|Related topics|Related queries', sections[1], maxsplit=1, flags=re.I)[0]
        rows = []
        for date, value in re.findall(r'- cell "([^"\n]+)"[^\n]*\n\s*- cell "([^"\n]+)"', section):
            date = re.sub('[\u202a-\u202e\u2066-\u2069]', '', date).strip()
            try:
                parsed_date = datetime.strptime(date, '%b %d, %Y').date().isoformat()
                number = int(value)
            except ValueError:
                continue
            if 0 <= number <= 100:
                rows.append({'date': parsed_date, 'value': number, 'term': term})
        return rows[:104]

    def _read_download(self, candidate):
        candidate = Path(candidate)
        path = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        allowed = (self.root / ".playwright-cli").resolve()
        if (not allowed.is_relative_to(self.root.resolve()) or not path.is_relative_to(allowed)
                or path.suffix.casefold() != ".csv" or not path.is_file()
                or path.stat().st_size > _MAX_CSV_BYTES):
            raise BrowserError("The downloaded CSV is outside the browser output folder or too large.")
        with path.open("rb") as stream:
            payload = stream.read(_MAX_CSV_BYTES + 1)
        if len(payload) > _MAX_CSV_BYTES:
            raise BrowserError("The downloaded CSV exceeds the research size limit.")
        return payload

    @staticmethod
    def parse_csv(source, text):
        """Parse browser-exported CSV only, retaining the export's exact values."""
        if not isinstance(text, str) or len(text) > _MAX_CSV_BYTES:
            raise BrowserError("The research CSV is invalid or too large.")
        text = text.lstrip("\ufeff")
        delimiter = "\t" if "\t" in text[:4000] else ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error as exc:
            raise BrowserError("The downloaded research CSV could not be parsed.") from exc
        data = {"keywords": [], "trends": [], "competitors": [], "notes": []}
        if source == "google_trends":
            index = next((i for i, row in enumerate(rows) if len(row) > 1 and row[0].strip().casefold() in
                          {"day", "week", "month", "year", "date", "time"}), None)
            if index is None:
                raise BrowserError("The download is not a Google Trends time-series CSV.")
            headers = [cell.strip()[:300] for cell in rows[index][:9]]
            data["csv_context"] = {"preamble": [", ".join(row)[:400] for row in rows[:index] if any(row)],
                                   "date_unit": headers[0], "comparison_terms": headers[1:]}
            for row in rows[index + 1:index + 1501]:
                if not row or not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", row[0].strip()):
                    continue
                for column, label in enumerate(headers[1:], 1):
                    value = row[column].strip() if column < len(row) else ""
                    if not re.fullmatch(r"<1|(?:100|[0-9]{1,2})(?:\.0+)?", value):
                        continue
                    term = re.sub(r":\s*\([^)]*\)\s*$", "", label).strip()
                    if term:
                        data["trends"].append({"date": row[0].strip(), "value": value, "term": term, "series_label": label})
                if len(data["trends"]) >= 1500:
                    data["notes"].append("CSV rows were limited to 1,500 observed data points.")
                    break
            data["notes"].append("Google Trends CSV values are relative interest within this export's comparison context; they are not search counts. Zero and <1 are preserved.")
        elif source == "keyword_planner":
            normal = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
            index = next((i for i, row in enumerate(rows) if "keyword" in [normal(cell) for cell in row]
                          and any(normal(cell) in {"avgmonthlysearches", "averagemonthlysearches"} for cell in row)), None)
            if index is None:
                raise BrowserError("The download is not a supported Keyword Planner keyword-ideas CSV.")
            headers = {normal(cell): i for i, cell in enumerate(rows[index])}
            def cell(row, *names):
                column = next((headers[name] for name in names if name in headers), None)
                value = row[column].strip()[:300] if column is not None and column < len(row) else ""
                return value if value and value not in {"--", "—", "N/A"} else None
            for row in rows[index + 1:index + 101]:
                keyword = cell(row, "keyword")
                if not keyword:
                    continue
                low, high = cell(row, "topofpagebidlowrange"), cell(row, "topofpagebidhighrange")
                data["keywords"].append({"keyword": keyword,
                    "volume": cell(row, "avgmonthlysearches", "averagemonthlysearches"),
                    "competition": cell(row, "competition"), "bid": f"{low} – {high}" if low and high else low or high,
                    "currency": cell(row, "currency", "currencycode")})
            data["csv_context"] = {"headers": rows[index], "preamble": [", ".join(row)[:400] for row in rows[:index] if any(row)]}
            data["notes"].append("Keyword Planner competition describes Google advertising competition, not marketplace seller competition.")
        else:
            raise BrowserError("CSV research exports are supported only for Trends and Keyword Planner.")
        return data

    def _download_csv(self, source, snapshot, ref, stop=None):
        if not self._download_allowed(source, snapshot, ref):
            raise BrowserError("Only an observed research-chart CSV download is permitted.")
        # The installed CLI auto-saves browser downloads. Read its explicit completion
        # event; never inspect Chrome's Downloads folder or call a hidden data API.
        output = self._cli(["click", ref], stop=stop, timeout=20)
        match = None
        pending = False
        deadline = time.monotonic() + 20
        # Recent CLI events are drained by each snapshot. Remember download-start
        # even if an intermediate snapshot has no new events.
        for attempt in range(5):
            if _stopped(stop):
                raise BrowserError("Stopped by user.")
            match = re.search(r'- Downloaded file [^\r\n]+ to "([^"\r\n]+)"', output)
            if match:
                break
            pending = pending or "Downloading file" in output
            if not pending or attempt == 4 or time.monotonic() >= deadline:
                raise BrowserError("The browser did not complete a CSV download. Open the export menu manually if needed.")
            output = self._cli(["snapshot"], stop=stop, timeout=max(1, min(10, deadline - time.monotonic())))
        payload = self._read_download(match.group(1))
        try:
            text = payload.decode("utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as exc:
            raise BrowserError("The research export is not a supported text CSV.") from exc
        data = self.parse_csv(source, text)
        folder = (self.root / ".playwright-cli" / "marketradar-downloads").resolve()
        if not folder.is_relative_to(self.root.resolve() / ".playwright-cli"):
            raise BrowserError("The research download folder is outside the workspace.")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{uuid4().hex}.csv"
        with destination.open("xb") as stream:
            stream.write(payload)
        artifact = {"path": destination.relative_to(self.root.resolve()).as_posix(), "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(), "raw_excerpt": _redact(text[:6000])}
        return data, artifact

    def _action_args(self, decision, source, snapshot, brief, focused):
        if source not in {"keyword_planner", "google_trends"}:
            raise BrowserError("Model browser actions are limited to Google research pages.")
        action = decision.get("action")
        if action not in _ACTIONS:
            raise BrowserError("The model proposed an unsupported browser action.")
        if action == "snapshot":
            return ["snapshot"], focused
        if action == "scroll":
            return ["mousewheel", "0", "600"], focused
        if action == "press":
            key = decision.get("key")
            if not focused or key not in {"Enter", "Escape", "ArrowDown", "ArrowUp"}:
                raise BrowserError("Keyboard actions require a previously validated research field.")
            focus_line = self._ref_line(snapshot, focused)
            if not re.search(r"textbox|combobox|searchbox", focus_line) or _FORBIDDEN.search(focus_line):
                raise BrowserError("The previously focused research field is no longer available.")
            return ["press", key], focused
        ref = decision.get("ref")
        line = self._ref_line(snapshot, ref)
        if source == 'keyword_planner' and action == 'click' and re.search(r'button "Discover new keywords"', line):
            # Google exposes an empty heading button beside the actual card.
            # Both appear in snapshots; the descriptive card is actionable.
            card = next((candidate for candidate in snapshot.splitlines()
                         if 'button "Discover new keywords Get keyword ideas' in candidate and '[ref=' in candidate), None)
            if card:
                ref = re.search(r'\[ref=(e\d+)\]', card).group(1)
                line = card
        label = re.sub(r"\[ref=e\d+\]", "", line)
        if _FORBIDDEN.search(label):
            raise BrowserError("Blocked an action outside research; account, campaign, and purchase changes require manual control.")
        if action == "click":
            allow = _PLANNER_BUTTONS if source == "keyword_planner" else _TRENDS_BUTTONS
            filter_aliases = [*_COUNTRY_NAMES.get(str(brief.get("country", "US")).upper(), ()), str(brief.get("language", "English"))]
            if not allow.search(label) and not any(f'"{alias}"'.casefold() in label.casefold() for alias in filter_aliases):
                raise BrowserError("Only recognized research and filter controls can be clicked.")
            if not re.search(r"\b(button|tab|combobox|textbox|menuitem)\b", label):
                raise BrowserError("The selected ref is not a research control.")
            is_field = _FIELDS.search(label) and re.search(r"textbox|combobox|searchbox", label)
            return ["click", ref], ref if is_field else None
        if action not in {"fill", "select"} or not _FIELDS.search(label) or not re.search(r"textbox|combobox|searchbox", label):
            raise BrowserError("Only observed keyword and research-filter fields may be filled.")
        value = decision.get("value")
        if not isinstance(value, str) or len(value) > 300 or value.startswith("--") or any(c in value for c in ("\n", "\r", "\x00")):
            raise BrowserError("Invalid research-field value.")
        allowed_values = {str(brief.get(key, "")) for key in ("keyword", "country", "language", "date_range")}
        if re.search(r"location|country|region", label, re.I):
            allowed_values.update(_COUNTRY_NAMES.get(str(brief.get("country", "US")).upper(), ()))
        if value not in allowed_values and not (action == "select" and _grounded(value, snapshot)):
            raise BrowserError("Research field values must come from the brief or an observed option.")
        return [action, ref, value], ref

    @staticmethod
    def _extract(data, source, snapshot):
        cleaned = {"keywords": [], "trends": [], "competitors": [], "notes": []}
        if not isinstance(data, dict):
            return cleaned
        if source == "keyword_planner":
            for row in (data.get("keywords") or [])[:50]:
                if not isinstance(row, dict):
                    continue
                block = _entity_block(snapshot, row.get("keyword"), [row.get(key) for key in ("volume", "competition", "bid")])
                if not block:
                    continue
                result = {"keyword": _grounded(row.get("keyword"), block)}
                metric_evidence = block.replace(str(row.get("keyword")), "")
                result.update({key: _grounded_metric(row.get(key), metric_evidence) for key in ("volume", "competition", "bid")})
                result["currency"] = _grounded(row.get("currency"), metric_evidence)
                cleaned["keywords"].append(result)
        elif source == "google_trends":
            for row in (data.get("trends") or [])[:104]:
                if not isinstance(row, dict):
                    continue
                block = _entity_block(snapshot, row.get("date"), [row.get("term"), row.get("value")])
                date = _grounded(row.get("date"), block)
                term = _grounded(row.get("term"), block)
                value_evidence = block.replace(str(row.get("date")), "").replace(str(row.get("term")), "")
                value = _grounded_metric(row.get("value"), value_evidence)
                if date and value is not None and term:
                    cleaned["trends"].append({"date": date, "value": value, "term": term})
        for note in (data.get("notes") or [])[:6]:
            if isinstance(note, str) and _grounded(note, snapshot):
                cleaned["notes"].append(_redact(note)[:400])
        return cleaned

    @staticmethod
    def _verified_settings(source, brief, url, snapshot):
        """Requested settings are not assumed to be applied in a signed-in UI."""
        verified = {"country": False, "language": False, "date_range": False}
        if source == "google_trends":
            query = parse_qs(urlsplit(url).query)
            verified["country"] = query.get("geo") == [str(brief.get("country", "US")).upper()]
            verified["date_range"] = query.get("date") == [str(brief.get("date_range", "today 12-m"))]
        # A language mention elsewhere on a page does not establish a filter.
        language = re.escape(str(brief.get("language", "English")))
        verified["language"] = bool(re.search(r'(?:combobox|button) "(?:Language[s]?:?\s*)?' + language + r'"', snapshot, re.I))
        return verified

    def _google_result(self):
        state = self._google
        result = {"source": state["source"], "url": state["evidence"]["url"],
                  "status": "running" if state["running"] else state["evidence"]["status"],
                  "steps_remaining": max(0, state["budget"] - state["steps"]),
                  "evidence": None if state["running"] else state["evidence"]}
        if state["running"]:
            result["snapshot"] = state["snapshot"]
        return result

    def _finish_google(self, status, note=None):
        state = self._google
        evidence = state["evidence"]
        evidence["status"] = status
        if note:
            evidence["data"]["notes"].append(note)
        if state["running"]:
            if not all(evidence["settings_verified"].values()):
                evidence["data"]["notes"].append("Settings show the requested research scope. Only settings_verified=true values were confirmed; other source filters remain unverified.")
            state["running"] = False
            self._event(state["source"], status, f"{state['source'].replace('_', ' ')}: {status}.")
        return self._google_result()

    def _observe_google(self, stop=None):
        """Refresh evidence and perform fixed UI extraction before asking an agent."""
        state = self._google
        source, brief, evidence = state["source"], state["brief"], state["evidence"]
        while True:
            if _stopped(stop):
                raise BrowserError("Stopped by user.")
            url = self._page_url(state["output"], state["url"])
            snapshot = self._snapshot_text(state["output"])
            state["snapshot"] = snapshot
            evidence["url"], evidence["raw_excerpt"] = _redact(url), snapshot[:12_000]
            evidence["settings_verified"] = self._verified_settings(source, brief, url, snapshot)
            if not self._allowed_location(source, url):
                raise BrowserError("The source navigated outside its allowed research pages.")
            if source == "google_trends":
                table = self._trends_table(snapshot, brief, url)
                if table:
                    evidence["data"]["trends"] = table
                    return self._finish_google("live", "Values read directly from the browser accessibility table for Interest over time.")
            status = self._page_status(source, url, snapshot)
            download = self._find_download(source, snapshot) if source == "google_trends" and not state["attempted_download"] else None
            # A missing related-topics panel does not invalidate the time chart.
            if status == "no_results" and download:
                status = None
            if status:
                notes = {"rate_limited": "The source rate-limited this request (HTTP 429). Collection stopped without retrying or bypassing the limit.",
                         "login_required": "Complete sign-in or account setup manually in Chrome, then rerun.",
                         "account_setup_required": "Complete sign-in or account setup manually in Chrome, then rerun.",
                         "browser_setup_required": "Google Ads reports an ad blocker. Pause it for ads.google.com manually and reload Keyword Planner."}
                return self._finish_google(status, notes.get(status, "The source did not provide usable research data."))
            if download:
                state["attempted_download"] = True
                self._event(source, "running", "Downloading the observed Interest over time CSV through the browser.")
                try:
                    data, artifact = self._download_csv(source, snapshot, download, stop=stop)
                    evidence["data"], evidence["download"] = data, artifact
                    evidence["raw_excerpt"] += "\nBrowser CSV export:\n" + artifact["raw_excerpt"]
                    return self._finish_google("live" if data["trends"] else "no_results")
                except BrowserError as exc:
                    if _stopped(stop):
                        raise
                    evidence["data"]["notes"].append(str(exc))
                    state["output"] = self._cli(["snapshot"], stop=stop)
                    continue
            if source == "keyword_planner" and not state["submitted"] and _grounded(brief["keyword"], snapshot):
                results = next((line for line in snapshot.splitlines()
                                if 'button "Get results"' in line and "[disabled]" not in line and "[ref=" in line), None)
                if results:
                    state["submitted"] = True
                    ref = re.search(r"\[ref=(e\d+)\]", results).group(1)
                    self._event(source, "running", "Getting keyword ideas for the observed seed.")
                    self._cli(["click", ref], stop=stop)
                    if stop is not None and stop.wait(3):
                        raise BrowserError("Stopped by user.")
                    if stop is None:
                        time.sleep(3)
                    state["output"] = self._cli(["snapshot"], stop=stop)
                    continue
            if state["steps"] >= state["budget"]:
                return self._finish_google("failed", "Browser step limit reached before usable evidence was extracted.")
            return self._google_result()

    def open_google(self, source, brief, stop=None):
        """Open one Google source; the DeepAgent decides subsequent tool actions."""
        with self._lock:
            if source not in {"keyword_planner", "google_trends"}:
                raise BrowserError("Choose Keyword Planner or Google Trends.")
            if self._google and self._google["running"]:
                raise BrowserError("Finish the active Google source before opening another.")
            url = self.source_url(source, brief)
            evidence = {"id": f"ev_{uuid4().hex[:12]}", "source": source, "status": "failed", "url": url,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "settings": {key: brief.get(key, default) for key, default in (("country", "US"), ("language", "English"), ("date_range", "today 12-m"))},
                "settings_verified": {"country": False, "language": False, "date_range": False},
                "data": {"keywords": [], "trends": [], "competitors": [], "notes": []}, "raw_excerpt": ""}
            self._google = {"source": source, "brief": brief, "url": url, "evidence": evidence,
                            "running": True, "steps": 0, "budget": max(1, min(int(brief.get("max_browser_steps", 6)), 6)),
                            "focused": None, "attempted_download": False, "submitted": False, "snapshot": ""}
            self._event(source, "running", f"Collecting {source.replace('_', ' ')} through Playwright CLI.")
            try:
                if _stopped(stop):
                    raise BrowserError("Stopped by user.")
                self._google["output"] = self._cli(["tab-new", url], stop=stop)
                # Google renders the shell before its widgets; do not spend agent
                # turns on eight identical loading snapshots.
                for _ in range(8):
                    initial = self._snapshot_text(self._google["output"])
                    status = self._page_status(source, self._page_url(self._google["output"], url), initial)
                    planner_ready = source == "keyword_planner" and re.search(
                        r"(?:button|heading)[^\n]*Discover new keywords[^\n]*\[ref=e\d+\]|Avg\. monthly searches", initial, re.I)
                    if (status not in (None, "no_results") or planner_ready or self._find_download(source, initial)
                            or (source == "google_trends" and self._trends_table(initial, brief, self._page_url(self._google["output"], url)))):
                        break
                    if stop is not None and stop.wait(2):
                        raise BrowserError("Stopped by user.")
                    if stop is None:
                        time.sleep(2)
                    self._google["output"] = self._cli(["snapshot"], stop=stop)
                return self._observe_google(stop)
            except (BrowserError, ValueError, TypeError, OSError, RuntimeError) as exc:
                return self._finish_google("failed", str(exc)[:300])

    def google_step(self, decision, stop=None):
        """Execute one bounded, validated decision; no model runs in this module."""
        with self._lock:
            if not self._google:
                raise BrowserError("Open a Google source before taking a research step.")
            state = self._google
            if not state["running"]:
                return self._google_result()
            source, evidence, snapshot = state["source"], state["evidence"], state["snapshot"]
            try:
                if _stopped(stop):
                    raise BrowserError("Stopped by user.")
                if state["steps"] >= state["budget"]:
                    return self._finish_google("failed", "Browser step limit reached before usable evidence was extracted.")
                state["steps"] += 1
                if not isinstance(decision, dict):
                    raise BrowserError("The research agent returned an invalid decision.")
                action = decision.get("action")
                if action == "needs_login":
                    return self._finish_google("login_required", "Sign in manually in Chrome and rerun this research.")
                if action == "done":
                    evidence["data"] = self._extract(decision.get("data"), source, snapshot)
                    useful = bool(evidence["data"]["keywords"] or evidence["data"]["trends"])
                    if not useful and source == 'keyword_planner' and 'button "Discover new keywords' in snapshot and state['steps'] < state['budget']:
                        result = self._google_result()
                        result['hint'] = 'This is the Planner home screen. Click the observed Discover new keywords card, enter the seed, and get results before extracting.'
                        return result
                    return self._finish_google("live" if useful else "no_results",
                        None if useful else "No supported structured rows were visible; no metrics were invented.")
                if action == "download":
                    data, artifact = self._download_csv(source, snapshot, decision.get("ref"), stop=stop)
                    evidence["data"], evidence["download"] = data, artifact
                    evidence["raw_excerpt"] += "\nBrowser CSV export:\n" + artifact["raw_excerpt"]
                    return self._finish_google("live" if data["trends"] or data["keywords"] else "no_results")
                args, state["focused"] = self._action_args(decision, source, snapshot, state["brief"], state["focused"])
                self._event(source, "running", f"Research step {state['steps']}/{state['budget']}: {' '.join(args[:2])}.")
                try:
                    state["output"] = self._cli(args, stop=stop)
                except BrowserError:
                    if source != "keyword_planner" or args[0] != "click" or "Discover new keywords" not in self._ref_line(snapshot, args[1]):
                        raise
                    # Google can replace this card while the agent is deciding.
                    state["output"] = self._cli(["snapshot"], stop=stop)
                    refreshed = self._snapshot_text(state["output"])
                    if 'button "Get results"' not in refreshed:
                        card = next((line for line in refreshed.splitlines()
                                     if 'button "Discover new keywords Get keyword ideas' in line and "[ref=" in line), None)
                        if not card:
                            raise
                        ref = re.search(r"\[ref=(e\d+)\]", card).group(1)
                        state["output"] = self._cli(["click", ref], stop=stop)
                if args[0] != "snapshot":
                    state["output"] = self._cli(["snapshot"], stop=stop)
                return self._observe_google(stop)
            except (BrowserError, ValueError, TypeError, OSError, RuntimeError) as exc:
                return self._finish_google("failed", str(exc)[:300])

    def _amazon_page(self, config, stop=None):
        """Run the same checked-in script with JSON data, never model-generated code."""
        template = Path(__file__).with_name('amazon_research.js').read_text(encoding='utf-8')
        folder = self.root / '.playwright-cli'
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / f'amazon-{uuid4().hex}.js'
        try:
            script.write_text(template.replace('/*MARKETRADAR_CONFIG*/ {}', json.dumps(config, ensure_ascii=True)), encoding='utf-8')
            output = self._cli(['run-code', '--filename=' + str(script.resolve())], stop=stop, timeout=60)
            section = re.search(r'### Result\s*\n(.*?)(?=\n### |\Z)', output, re.S)
            result = json.loads(section.group(1)) if section else None
            if not isinstance(result, dict) or result.get('status') not in _STATUSES:
                raise BrowserError('Amazon script did not return structured evidence.')
            return result
        finally:
            script.unlink(missing_ok=True)

    def collect_suppliers(self, brief, product, *, stop=None):
        """Read public directory evidence; badges are observations, not our guarantees."""
        with self._lock:
            query = product.get('title') or product.get('product') or product.get('name') or brief['keyword']
            query = query if isinstance(query, str) and 2 <= len(query.strip()) <= 100 else brief['keyword']
            url = 'https://www.made-in-china.com/productdirectory.do?' + urlencode(
                {'word': query.strip(), 'subaction': 'hunt', 'mode': 'and', 'code': '0'})
            result = {'suppliers': [], 'status': 'failed', 'url': url,
                      'captured_at': datetime.now(timezone.utc).isoformat(),
                      'notes': ['Directory badges are recorded as displayed, not independently verified. Product fit, certifications, samples and commercial terms need human checks. No supplier was contacted.']}
            # Fixed browser code and a JSON URL: the LLM never supplies JavaScript.
            code = r'''async page => {
              const result = {suppliers: [], status: 'failed', notes: []};
            const safe = raw => typeof raw === 'string' &&
                /^https:\/\/[a-z0-9-]+\.en\.made-in-china\.com\/[a-z0-9_./?=%&+-]*$/i.test(raw) ? raw : null;
              const visit = async url => {
                const response = await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 20000});
                const current = page.url();
                if (!(current.startsWith('https://www.made-in-china.com/') || safe(current))) throw new Error('Directory redirected outside allowed public pages.');
                const body = await page.locator('body').innerText({timeout: 5000});
                if ([403, 429].includes(response?.status()) || /captcha|verify (?:you are|that you are) human|access denied|robot check|security verification/i.test(body)) {
                  result.status = 'blocked'; result.notes.push('Directory access check stopped collection; no bypass was attempted.'); return false;
                }
                if ((response?.status() || 200) >= 400) throw new Error('Directory page could not be read.');
                return true;
              };
              try {
                if (!await visit(/*SEARCH_URL*/)) return result;
                const cards = page.locator('.list-node, .J-prod-list .list-img, .J-prod-list .list-item');
                await cards.first().waitFor({timeout: 8000});
                const candidates = await cards.evaluateAll(cards => cards.slice(0, 15).map(card => {
                  const company = card.querySelector('.company-name-txt a, .company-name a');
                  return {name: company?.innerText?.trim(), url: company?.href,
                    product: card.querySelector('.product-name')?.innerText?.trim().slice(0, 400),
                    snippet: card.innerText.slice(0, 700)};
                }));
                const seen = new Set();
                for (const item of candidates) {
                  const profile = safe(item.url)?.split('?')[0];
                  if (!profile || !/^https:\/\/[a-z0-9-]+\.en\.made-in-china\.com\/$/i.test(profile) || !item.name || seen.has(profile)) continue;
                  seen.add(profile);
                  if (!await visit(profile)) return result;
                  const observed = await page.evaluate(() => ({
                    audit: (() => { const badge = document.querySelector('.shop-sign__as img[alt="Audited Supplier"]');
                      return badge?.getClientRects().length ? badge.alt : null; })(),
                    license: [...document.querySelectorAll('.shop-sign *')].some(el => el.innerText?.trim() === 'Suppliers with verified business licenses'),
                    contact: [...document.querySelectorAll('a[href]')].find(a => a.pathname === '/contact-info.html')?.href || null,
                    email: [...document.querySelectorAll('a[href^="mailto:"]')].find(a => a.textContent.includes('@'))?.getAttribute('href')?.slice(7).split('?')[0] || null
                  }));
                  const badges = [observed.audit === 'Audited Supplier' ? 'Audited Supplier' : null,
                    observed.license ? 'Suppliers with verified business licenses' : null].filter(Boolean);
                  result.suppliers.push({id: `supplier-${result.suppliers.length + 1}`, name: item.name.slice(0, 200), url: profile,
                    product: item.product, verification: badges.length ? `Directory badge observed: ${badges.join('; ')}` : 'Unverified: no supported directory badge observed.',
                    verification_url: badges.length ? page.url() : null,
                    contact_url: safe(observed.contact) && observed.contact.split('/')[2].toLowerCase() === profile.split('/')[2].toLowerCase() ? observed.contact : null,
                    email: /^[^\s@<>]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(observed.email || '') ? observed.email : null, captured_at: new Date().toISOString(),
                    notes: [`Search card: ${item.snippet}`, 'Certification and product quality were not independently checked.']});
                  if (result.suppliers.length === 3) break;
                }
                result.status = result.suppliers.length ? 'live' : 'failed';
                if (!result.suppliers.length) result.notes.push('No readable supplier cards matched the public directory layout.');
              } catch (error) { result.status = result.suppliers.length ? 'live' : 'failed'; result.notes.push(`Supplier collection stopped: ${error.message?.slice(0, 180) || 'page error'}`); }
              return result;
            }'''.replace('/*SEARCH_URL*/', json.dumps(url, ensure_ascii=True))
            folder = self.root / '.playwright-cli'
            folder.mkdir(parents=True, exist_ok=True)
            script = folder / f'suppliers-{uuid4().hex}.js'
            try:
                self._event('suppliers', 'running', f'Searching public supplier profiles for “{query}”.')
                script.write_text(code, encoding='utf-8')
                self._cli(['tab-new', 'about:blank'], stop=stop)
                output = self._cli(['run-code', '--filename=' + str(script.resolve())], stop=stop, timeout=110)
                section = re.search(r'### Result\s*\n(.*?)(?=\n### |\Z)', output, re.S)
                data = json.loads(section.group(1)) if section else None
                if not isinstance(data, dict) or data.get('status') not in ('live', 'blocked', 'failed'):
                    raise BrowserError('Supplier script did not return structured evidence.')
                result.update({key: data[key] for key in ('suppliers', 'status')})
                result['notes'].extend(data.get('notes', []))
            except (BrowserError, ValueError, TypeError, OSError) as exc:
                result['notes'].append(str(exc)[:300])
            finally:
                script.unlink(missing_ok=True)
                self._event('suppliers', result['status'], f"Supplier research: {len(result['suppliers'])} public profiles.")
            return result

    def collect_amazon(self, brief, searches, stop=None):
        with self._lock:
            evidence = {'id': 'amazon-research', 'source': 'marketplace', 'status': 'failed',
                        'url': 'https://www.amazon.com/s?' + urlencode({'k': brief['keyword']}),
                        'captured_at': datetime.now(timezone.utc).isoformat(),
                        'settings': {'storefront': 'amazon.com', 'country': brief['country'], 'language': brief['language'], 'date_range': brief['date_range']},
                        'settings_verified': {'country': False, 'language': False, 'date_range': False},
                        'data': {'products': [], 'competitors': [], 'searches': [], 'notes': []}}
            data = evidence['data']
            try:
                if _stopped(stop):
                    raise BrowserError('Stopped by user.')
                # Search planning belongs to the Amazon subagent; the browser only
                # accepts bounded plain-text queries and runs the fixed script.
                proposed = searches if isinstance(searches, list) else []
                searches = [brief['keyword']]
                for term in proposed[:8]:
                    if (isinstance(term, str) and 2 <= len(term.strip()) <= 160
                            and term.strip().casefold() != brief['keyword'].casefold()
                            and not re.search(r'https?://|[\x00-\x1f]', term)):
                        searches.append(term.strip())
                        break
                self._cli(['tab-new', 'about:blank'], stop=stop)
                candidates = []
                for term in searches:
                    self._event('marketplace', 'running', f'Amazon script: search “{term}”.')
                    result = self._amazon_page({'action': 'search', 'query': term}, stop)
                    data['searches'].append({'query': term, 'status': result['status']})
                    data['notes'].extend(result.get('notes', []))
                    evidence['status'] = result['status']
                    if result['status'] in ('challenge', 'rate_limited', 'login_required'):
                        return evidence
                    candidates.append(result.get('products', []))
                # Seed results plus one alternative; explicitly supplied products take priority.
                selected = {}
                for supplied in [brief.get('product_url', ''), *brief.get('competitor_urls', [])]:
                    if supplied:
                        match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)', safe_marketplace_url(supplied))
                        if match:
                            selected[match.group(1)] = {'asin': match.group(1)}
                # Ignore obvious size/colour variants with the same four-word title prefix.
                families = set()
                for position in range(6):
                    for rows in candidates:
                        if position < len(rows):
                            listing = rows[position]
                            family = ' '.join(listing['title'].casefold().split()[:4])
                            if family not in families:
                                selected.setdefault(listing['asin'], listing)
                                families.add(family)
                for asin, listing in list(selected.items())[:3]:
                    self._event('marketplace', 'running', f'Amazon script: reading features and reviews for {asin}.')
                    result = self._amazon_page({'action': 'product', 'asin': asin}, stop)
                    evidence['status'] = result['status']
                    data['notes'].extend(result.get('notes', []))
                    if result['products']:
                        # Keep search-page demand labels when the detail page omits them.
                        product = dict(listing)
                        product.update({key: value for key, value in result['products'][0].items() if value is not None})
                        data['products'].append(product)
                    if result['status'] in ('challenge', 'rate_limited', 'login_required'):
                        data['notes'].append('Collection stopped early: ' + result['status'])
                        evidence['status'] = result['status']
                        break
                if data['products']:
                    evidence['status'] = 'live'
                elif evidence['status'] == 'live':
                    evidence['status'] = 'no_results'
                data['competitors'] = data['products']
            except (BrowserError, ValueError, TypeError, OSError) as exc:
                evidence['status'] = 'live' if data['products'] else 'failed'
                data['competitors'] = data['products']
                data['notes'].append(str(exc)[:300])
            finally:
                count = len({review['id'] for product in data['products'] for review in product.get('reviews', [])})
                data['notes'].append(f"Sample: {len(data['products'])} products, {count} unique reviews; at most two searches, three products and twelve reviews per product, with review text limited to 1,200 characters. Embedded top/critical reviews may pool variants and countries; not representative of all buyers. Amazon.com delivery, language and review dates are not aligned to Google filters.")
                self._event('marketplace', evidence['status'], f"Amazon research: {len(data['products'])} products and {count} reviews.")
            return evidence
