'use strict';

const $ = (id) => document.getElementById(id);
const app = { status: null, run: null, runId: null, poll: null, selected: new Set(), loading: false, reviewHydrated: null, humanHydrated: null, reviewing: false };
const terminalStates = new Set(['completed', 'complete', 'done', 'succeeded', 'failed', 'error', 'cancelled', 'canceled', 'ready', 'awaiting_approval', 'partial', 'needs_attention', 'interrupted']);
const sourceNames = { keyword_planner: 'Keyword Planner', planner: 'Keyword Planner', google_ads: 'Keyword Planner', trends: 'Google Trends', google_trends: 'Google Trends', marketplace: 'Marketplace' };

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}

function readable(value) {
  if (value === null || value === undefined || value === '') return 'Not provided';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value).replace(/_/g, ' ');
}

function safeUrl(value) {
  try {
    const url = new URL(String(value));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function externalLink(value, label) {
  const href = safeUrl(value);
  if (!href) return null;
  const a = node('a', label || 'Open source ↗');
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

function empty(message, heading) {
  const el = node('div', undefined, 'empty-state');
  if (heading) el.append(node('strong', heading));
  el.append(node('span', message));
  return el;
}

function dateText(value, timeOnly = false) {
  if (!value) return 'Time unavailable';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return timeOnly ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers }, cache: 'no-store' });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { throw new Error(`The server returned an unreadable response (${response.status}).`); }
  if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : typeof data.detail === 'string' ? data.detail : `Request failed (${response.status}).`);
  return data;
}

function post(path, body = {}) { return api(path, { method: 'POST', body: JSON.stringify(body) }); }

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = `notice${error ? ' error' : ''}`;
  $('notice').setAttribute('role', error ? 'alert' : 'status');
  $('notice').hidden = false;
}

async function buttonAction(button, action, pending = 'Working…') {
  const text = button.textContent;
  button.disabled = true;
  button.textContent = pending;
  try { return await action(); }
  catch (error) { notice(error.message, true); return null; }
  finally { button.disabled = false; button.textContent = text; }
}

function navigate(view, focus = false) {
  const valid = ['research', 'connections', 'history'].includes(view) ? view : 'research';
  document.querySelectorAll('.view').forEach((el) => { el.hidden = el.id !== `view-${valid}`; });
  document.querySelectorAll('.nav-link').forEach((el) => {
    const active = el.dataset.view === valid;
    el.classList.toggle('active', active);
    if (active) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('view-name').textContent = valid.charAt(0).toUpperCase() + valid.slice(1);
  if (location.hash !== `#${valid}`) history.replaceState(null, '', `#${valid}`);
  if (valid === 'history') loadHistory();
  if (focus) $('main').focus({ preventScroll: true });
}

function badge(id, text, tone = '') {
  $(id).textContent = text;
  $(id).className = `status-pill ${tone}`.trim();
}

function browserReady(browser = {}) {
  return Boolean(browser.attached || browser.connected || ['ready', 'attached', 'connected'].includes(browser.status));
}

function renderStatus(status) {
  app.status = status;
  const reasoning = status.reasoning || {}, browser = status.browser || {};
  const reasoningReady = Boolean(reasoning.available !== false && reasoning.ready);
  const providerName = 'Alternate Clouds';
  const attached = browserReady(browser);
  badge('reasoning-badge', reasoningReady ? 'Configured · verify access' : 'Setup needed', reasoningReady ? 'good' : 'warn');
  $('provider-title').textContent = providerName;
  $('provider-description').textContent = 'Research uses the workspace’s Alternate Clouds inference connection. Configuration alone does not confirm a successful model request.';
  badge('browser-badge', attached ? 'Attached' : 'Not attached', attached ? 'good' : 'warn');
  $('global-status').textContent = reasoningReady && attached ? 'Ready to research' : reasoningReady ? 'Attach research browser' : 'Connect reasoning session';
  $('global-status').classList.toggle('ready', reasoningReady && attached);
  $('connection-dot').classList.toggle('ready', reasoningReady && attached);
  $('readiness-list').replaceChildren();
  [[`${providerName} reasoning`, reasoningReady ? 'Configured' : 'Needs setup', reasoningReady], ['Research browser', attached ? 'Attached' : 'Attach to verify', attached]].forEach(([title, text, ready]) => {
    const li = node('li'); li.append(node('span', title), node('span', text, `readiness-state${ready ? ' ready' : ''}`)); $('readiness-list').append(li);
  });
  $('reasoning-detail').replaceChildren();
  [['Model', reasoning.model || 'Not reported'], ['Session', reasoning.authenticated ? 'Authenticated request confirmed' : 'Connection not yet authenticated by a request']].forEach(([key, value]) => {
    $('reasoning-detail').append(node('dt', key), node('dd', value));
  });
  const browserMessage = browser.message || browser.error || browser.detail;
  if (browserMessage) { $('browser-detail').textContent = typeof browserMessage === 'string' ? browserMessage : JSON.stringify(browserMessage, null, 2); $('browser-detail').hidden = false; }
  renderDeployment(status.deployment || {});
  updateReviewState();
}

function renderDeployment(deployment) {
  const demo = deployment.role === 'demo';
  const liveMode = document.querySelector('[name="mode"][value="live"]');
  liveMode.disabled = demo;
  if (demo) document.querySelector('[name="mode"][value="sample"]').checked = true;
  updateMode();
  $('cancel-button').disabled = demo && app.run?.mode === 'live';
  const viewer = deployment.browser_viewer;
  $('cloud-browser-link').hidden = !viewer || demo;
  if (viewer && !demo) {
    try {
      const url = new URL(viewer, location.origin);
      if (url.origin === location.origin && url.pathname.startsWith('/browser/')) $('cloud-browser-link').href = url.href;
      else $('cloud-browser-link').hidden = true;
    } catch { $('cloud-browser-link').hidden = true; }
  }
  $('browser-owner-note').hidden = !demo;
  $('browser-owner-note').textContent = 'Browser sign-in and connection settings are managed by the workspace owner. You can run research, review findings, and export results.';
  document.querySelectorAll('#attach-browser, [data-open-source]').forEach((el) => { el.disabled = demo; });
}

async function refreshStatus() {
  try { renderStatus(await api('/api/status')); }
  catch (error) { $('global-status').textContent = 'Server unavailable'; $('global-status').classList.remove('ready'); notice(error.message, true); }
}

function briefFromForm() {
  const urls = $('competitor-urls').value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
  if (urls.length > 3) throw new Error('Use at most three competitor URLs.');
  if (urls.some((url) => !safeUrl(url))) throw new Error('Competitor links must be complete http or https URLs.');
  return { keyword: $('keyword').value.trim(), marketplace: $('marketplace').value, country: $('country').value, language: $('language').value, date_range: $('date-range').value, product_url: $('product-url').value.trim(), competitor_urls: urls };
}

function updateMode() {
  const sample = document.querySelector('[name="mode"]:checked').value === 'sample';
  const demo = app.status?.deployment?.role === 'demo';
  $('mode-note').hidden = !sample;
  $('mode-note').textContent = demo ? 'Owner sign-in runs live research; demo visitors can explore samples and review saved reports.' : 'Sample mode uses example evidence. It makes no external research or model calls.';
  $('run-button').replaceChildren(document.createTextNode(sample ? 'Explore sample research' : 'Research this market'), node('span', '↗'));
}

async function startRun(event) {
  event.preventDefault();
  if (app.loading) return;
  try {
    const brief = briefFromForm();
    if (!brief.keyword) throw new Error('Enter a product or seed keyword.');
    const mode = document.querySelector('[name="mode"]:checked').value;
    if (app.status?.deployment?.role === 'demo' && mode !== 'sample') throw new Error('Owner sign-in is required to run live research.');
    app.loading = true; $('run-button').disabled = true; $('notice').hidden = true;
    const result = await post('/api/runs', { ...brief, mode });
    if (!result.id) throw new Error('The server did not return a research run ID.');
    await openRun(result.id, true);
  } catch (error) { notice(error.message, true); }
  finally { app.loading = false; $('run-button').disabled = Boolean(app.run && !isTerminal(app.run)); }
}

function isTerminal(run) { return terminalStates.has(run.state); }

async function openRun(id, scroll = false) {
  clearTimeout(app.poll);
  if (app.runId !== id) { app.selected.clear(); app.reviewHydrated = null; $('review-note').value = ''; $('review-result').hidden = true; }
  app.runId = id; navigate('research');
  $('run-section').hidden = false;
  await pollRun(id);
  if (scroll) $('run-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function pollRun(id) {
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (app.runId !== id) return;
    app.run = run; renderRun(run);
    if (!isTerminal(run) && run.state !== 'awaiting_review') app.poll = setTimeout(() => pollRun(id), 2000);
  } catch (error) {
    if (app.runId !== id) return;
    $('progress-message').textContent = `Connection interrupted: ${error.message} Retrying…`;
    app.poll = setTimeout(() => pollRun(id), 4000);
  }
}

function sourceGroup(value) {
  const source = String(value || '').toLowerCase();
  if (/planner|google_ads|keyword/.test(source)) return 'keyword_planner';
  if (/trend/.test(source)) return 'trends';
  if (/market|amazon|ebay|etsy|competitor|review/.test(source)) return 'marketplace';
  return source;
}

function sourceTone(value) {
  if (['live', 'ok', 'success', 'collected', 'completed', 'verified', 'available'].includes(value)) return 'good';
  if (['failed', 'error', 'extraction_failed'].includes(value)) return 'bad';
  return 'warn';
}

function renderRun(run) {
  const brief = run.brief || {}, evidence = run.evidence || [], events = run.events || [];
  $('run-title').textContent = brief.keyword || 'Your research';
  $('run-meta').textContent = [readable(brief.marketplace), brief.country, brief.language, brief.date_range, dateText(run.created_at)].filter(Boolean).join(' · ');
  badge('run-state', readable(run.state), ['failed', 'error'].includes(run.state) ? 'bad' : isTerminal(run) ? 'good' : 'warn');
  $('progress-message').textContent = run.error || events.at(-1)?.message || (isTerminal(run) ? 'Research run finished.' : 'Preparing research…');
  $('cancel-button').hidden = isTerminal(run);
  $('cancel-button').disabled = app.status?.deployment?.role === 'demo' && run.mode === 'live';
  $('run-button').disabled = !isTerminal(run);
  $('export-button').hidden = false; $('export-button').href = `/api/runs/${encodeURIComponent(run.id)}/export`;
  const sample = run.mode === 'sample';
  $('run-banner').hidden = !sample && !run.error;
  $('run-banner').textContent = sample ? 'SAMPLE RESEARCH · These records are example data. No live source or model connection was exercised.' : run.error ? `This run encountered an error: ${run.error}` : '';
  renderStages(run); renderEvents(events); renderSources(evidence, run);
  $('report-content').hidden = !run.report;
  if (run.report) renderReport(run);
  renderHumanLoop(run);
}

function renderStages(run) {
  const state = String(run.state || 'queued');
  const stages = [['Brief', /load|queue/], ['Collect', /collect|browser|planner|trend|suppli/], ['Analyze', /analy/], ['Recommend', /recommend|validat/], ['Review', /review|approval|ready|complete|done|succeed/]];
  let index = stages.findIndex(([, pattern]) => pattern.test(state));
  if (index < 0) {
    const stage = run.events?.at(-1)?.stage || '';
    index = stages.findIndex(([, pattern]) => pattern.test(stage));
  }
  $('stage-track').replaceChildren();
  stages.forEach(([title], i) => $('stage-track').append(node('span', title, `stage-item${i < index ? ' complete' : i === index ? ' current' : ''}`)));
}

function renderEvents(events) {
  $('event-count').textContent = `${events.length} events`;
  $('event-list').replaceChildren();
  events.forEach((event) => {
    const li = node('li'); const time = node('time', dateText(event.time || event.timestamp, true));
    li.append(time, node('span', event.message || readable(event.stage))); $('event-list').append(li);
  });
}

function renderSources(evidence, run) {
  $('source-cards').replaceChildren();
  ['keyword_planner', 'trends', 'marketplace'].forEach((source) => {
    const records = evidence.filter((record) => sourceGroup(record.source) === source);
    const record = records.at(-1);
    const status = record?.status || (isTerminal(run) ? 'not_collected' : 'pending');
    const card = node('article', undefined, 'source-card');
    const header = node('div', undefined, 'source-card-header');
    const name = source === 'marketplace' && run.brief?.marketplace === 'amazon' ? 'Amazon.com' : sourceNames[source];
    header.append(node('h3', name), node('span', readable(status), `status-pill ${sourceTone(status)}`));
    card.append(header);
    const notes = record?.data?.notes;
    const message = Array.isArray(notes) ? notes.join(' ') : notes || record?.message || record?.error || (record ? `${records.length} evidence record${records.length === 1 ? '' : 's'} · ${dateText(record.captured_at)}` : 'No source evidence recorded yet.');
    card.append(node('p', message));
    const link = externalLink(record?.url); if (link) card.append(link);
    $('source-cards').append(card);
  });
}

function renderReport(run) {
  const report = run.report || {}, evidence = run.evidence || [];
  renderReview(run);
  $('report-summary').textContent = typeof report.summary === 'string' ? report.summary : readable(report.summary);
  $('report-observations').replaceChildren();
  (report.observations || []).forEach((observation) => $('report-observations').append(node('li', typeof observation === 'string' ? observation : observation.text || observation.observation || JSON.stringify(observation))));
  renderFindings(report, evidence); renderKeywords(evidence); renderTrends(evidence); renderCompetitors(evidence); renderRecommendations(report.recommendations || [], evidence); renderEvidence(evidence);
  const limitations = report.limitations || [];
  $('limitations').replaceChildren(); $('limitations').hidden = !limitations.length;
  if (limitations.length) {
    const list = node('ul'); limitations.forEach((item) => list.append(node('li', typeof item === 'string' ? item : JSON.stringify(item))));
    $('limitations').append(node('strong', 'What this research cannot establish'), list);
  }
  updateReviewState();
}

function keywordsFrom(evidence) {
  return evidence.flatMap((record) => (record.data?.keywords || []).map((keyword) => ({ ...(typeof keyword === 'string' ? { keyword } : keyword), evidenceSource: record.source, evidenceStatus: record.status })));
}

function volumeText(row) {
  const value = row.avg_monthly_searches ?? row.average_monthly_searches ?? row.monthly_searches ?? row.search_volume ?? row.volume ?? row.volume_range;
  if (value === null || value === undefined || value === '') return 'Unavailable';
  if (typeof value === 'object') {
    const min = value.min ?? value.low ?? value.lower, max = value.max ?? value.high ?? value.upper;
    return min !== undefined && max !== undefined ? `${min}–${max}` : JSON.stringify(value);
  }
  return String(value);
}

function renderKeywords(evidence) {
  const rows = keywordsFrom(evidence);
  $('keyword-count').textContent = `${rows.length} RECORDED`;
  $('keyword-table').replaceChildren();
  if (!rows.length) { $('keyword-table').append(empty('No keyword rows were collected. See source status for access or extraction issues.')); return; }
  const table = node('table'); const head = node('thead'); const tr = node('tr');
  ['Keyword', 'Monthly estimate', 'Advertiser competition'].forEach((title) => tr.append(node('th', title))); head.append(tr); table.append(head);
  const body = node('tbody');
  rows.slice(0, 60).forEach((row) => {
    const tr = node('tr'); const term = node('td', row.keyword || row.term || row.query || row.text || 'Unnamed keyword');
    term.append(node('small', `${sourceNames[sourceGroup(row.evidenceSource)] || readable(row.evidenceSource)} · ${readable(row.evidenceStatus)}`));
    tr.append(term, node('td', volumeText(row), 'numeric'), node('td', readable(row.advertiser_competition ?? row.competition)));
    body.append(tr);
  });
  table.append(body); $('keyword-table').append(table);
}

function trendSeries(evidence) {
  const series = new Map();
  evidence.forEach((record) => (record.data?.trends || []).forEach((row) => {
    if (Array.isArray(row.points) || Array.isArray(row.values)) {
      const term = row.term || row.keyword || row.name || record.settings?.keyword || 'Search interest';
      const points = (row.points || row.values).map((point, i) => typeof point === 'number' ? { date: String(i + 1), value: point } : { date: point.date || point.time || point.period || String(i + 1), value: point.value ?? point.interest ?? point.index });
      series.set(`${record.id}:${term}`, { term, points, evidenceId: record.id });
    } else if (row.term || row.keyword || row.query) {
      const term = row.term || row.keyword || row.query; const key = `${record.id}:${term}`;
      if (!series.has(key)) series.set(key, { term, points: [], evidenceId: record.id });
      series.get(key).points.push({ date: row.date || row.time || row.period || '', value: row.value ?? row.interest ?? row.index });
    } else {
      const date = row.date || row.time || row.period;
      Object.entries(row).filter(([key, value]) => !['date', 'time', 'period', 'isPartial', 'is_partial'].includes(key) && typeof value === 'number').forEach(([term, value]) => {
        const key = `${record.id}:${term}`; if (!series.has(key)) series.set(key, { term, points: [], evidenceId: record.id });
        series.get(key).points.push({ date, value });
      });
    }
  }));
  return [...series.values()].map((item) => ({ ...item, points: item.points.filter((p) => p.value !== null && p.value !== '' && Number.isFinite(Number(p.value)) && Number(p.value) >= 0 && Number(p.value) <= 100).map((p) => ({ ...p, value: Number(p.value) })) })).filter((item) => item.points.length);
}

function svgNode(tag, attributes, text) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attributes || {}).forEach(([key, value]) => el.setAttribute(key, value));
  if (text !== undefined) el.textContent = text;
  return el;
}

function renderTrends(evidence) {
  $('trend-chart').replaceChildren();
  // Each evidence record contains one source comparison. Do not overlay separately normalized exports.
  const comparison = evidence.find((record) => (record.data?.trends || []).length);
  const series = trendSeries(comparison ? [comparison] : []).slice(0, 3);
  if (!series.length) { $('trend-chart').append(empty('A chart appears only when actual source values are available.', 'No trend series recorded')); return; }
  const colors = ['#214d3f', '#bb602f', '#75888b'];
  const svg = svgNode('svg', { viewBox: '0 0 520 245', class: 'trend-svg', role: 'img', 'aria-label': 'Collected relative search interest, scaled from zero to one hundred' });
  svg.append(svgNode('title', {}, 'Relative interest from collected Google Trends evidence'));
  const left = 32, top = 18, width = 472, height = 180;
  [0, 25, 50, 75, 100].forEach((value) => { const y = top + height * (1 - value / 100); svg.append(svgNode('line', { x1: left, x2: left + width, y1: y, y2: y, class: 'chart-grid' }), svgNode('text', { x: left - 8, y: y + 3, 'text-anchor': 'end', class: 'chart-label' }, String(value))); });
  series.forEach((item, index) => {
    const points = item.points.map((point, i) => `${left + (item.points.length === 1 ? width / 2 : i / (item.points.length - 1) * width)},${top + height * (1 - point.value / 100)}`).join(' ');
    if (item.points.length > 1) svg.append(svgNode('polyline', { points, fill: 'none', stroke: colors[index], 'stroke-width': '2.5', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    else svg.append(svgNode('circle', { cx: left + width / 2, cy: top + height * (1 - item.points[0].value / 100), r: 4, fill: colors[index] }));
  });
  const first = series[0].points[0].date, last = series[0].points.at(-1).date;
  svg.append(svgNode('text', { x: left, y: 224, class: 'chart-label' }, String(first).slice(0, 20)), svgNode('text', { x: left + width, y: 224, 'text-anchor': 'end', class: 'chart-label' }, String(last).slice(0, 20)));
  const legend = node('div', undefined, 'chart-legend');
  series.forEach((item, index) => { const entry = node('span'); const swatch = node('i'); swatch.style.backgroundColor = colors[index]; entry.append(swatch, document.createTextNode(item.term)); legend.append(entry); });
  $('trend-chart').append(svg, legend);
}

function renderCompetitors(evidence) {
  const competitors = evidence.flatMap((record) => (record.data?.products || record.data?.competitors || []).map((item) => ({ ...item, evidenceId: record.id, sourceUrl: record.url, evidenceStatus: record.status })));
  $('competitor-cards').replaceChildren();
  if (!competitors.length) { $('competitor-cards').append(empty('No comparable listings were collected. Marketplace access may be unavailable.')); return; }
  competitors.slice(0, 3).forEach((item, index) => {
    const card = node('article', undefined, 'competitor-card'); card.append(node('span', `SAMPLE ${String(index + 1).padStart(2, '0')} · ${readable(item.evidenceStatus)}`, 'tiny-label'), node('h4', item.title || item.name || 'Observed listing'));
    if (item.price !== null && item.price !== undefined) card.append(node('span', `${item.currency || ''} ${readable(item.price)}`.trim(), 'price'));
    const details = [item.rating != null ? `Rating: ${item.rating}` : null, item.review_count != null ? `Reviews: ${item.review_count}` : null, item.variant || item.condition].filter(Boolean);
    if (details.length) card.append(node('p', details.join(' · ')));
    if (item.demand_signal) card.append(node('p', `Observed demand signal: ${item.demand_signal}`));
    if (item.notes || item.description || item.positioning) card.append(node('p', readable(item.notes || item.description || item.positioning)));
    if (item.features?.length) {
      const features = node('ul', undefined, 'observation-list');
      item.features.slice(0, 3).forEach((feature) => features.append(node('li', feature)));
      card.append(node('strong', 'Observed features'), features);
    }
    renderProductReviews(card, item.reviews || []);
    if (item.review_status === 'login_required') card.append(node('p', 'Amazon requires sign-in in the cloud browser to read customer reviews. Product features are available.'));
    const link = externalLink(item.url || item.sourceUrl, 'View observed listing ↗'); if (link) card.append(link);
    $('competitor-cards').append(card);
  });
}

function evidenceAnchor(id) { return `evidence-${String(id).replace(/[^a-zA-Z0-9_-]/g, '_')}`; }

function sourceLink(sourceId, evidence) {
  const record = evidence.find((item) => item.id === sourceId);
  if (!record) return null;
  const link = node('a', `${sourceNames[sourceGroup(record.source)] || readable(record.source)} · ${sourceId} ↗`);
  link.href = `#${evidenceAnchor(sourceId)}`;
  link.addEventListener('click', (event) => { event.preventDefault(); document.querySelector('.evidence-details').open = true; $(evidenceAnchor(sourceId))?.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
  return link;
}

function renderFindings(report, evidence) {
  const sections = [
    ['products_in_demand', 'Products with demand signals', 'No supported product or keyword demand signals were reported.'],
    ['complaints', 'Buyer complaints', 'No review-backed buyer complaints were recorded in this section.'],
    ['requested_features', 'Features buyers request', 'No review-backed feature requests were recorded in this section.'],
    ['market_gaps', 'Potential market gaps', 'No market-gap hypothesis was recorded in this section.']
  ];
  const reviews = new Map(evidence.flatMap((record) => (record.data?.products || []).flatMap((product) => (product.reviews || []).map((review) => [review.id, review]))));
  $('gap-findings').replaceChildren();
  sections.forEach(([key, title, missing]) => {
    const section = node('section', undefined, 'competitor-section');
    const heading = node('div', undefined, 'section-heading compact'), label = node('h3', title);
    label.id = `findings-${key}`; section.setAttribute('aria-labelledby', label.id); heading.append(label);
    const cards = node('div', undefined, 'competitor-grid');
    (report[key] || []).slice(0, 6).forEach((finding) => {
      const card = node('article', undefined, 'competitor-card');
      card.append(node('h4', finding.title), node('p', finding.detail));
      const links = node('div', undefined, 'evidence-links');
      (finding.evidence_ids || []).forEach((id) => { const link = sourceLink(id, evidence); if (link) links.append(link); });
      (finding.review_ids || []).forEach((id) => { const review = reviews.get(id), link = review && externalLink(review.url, `Review ${id} ↗`); if (link) links.append(link); });
      card.append(links); cards.append(card);
    });
    if (!cards.childElementCount) cards.append(empty(missing));
    section.append(heading, cards); $('gap-findings').append(section);
  });
}

function renderProductReviews(card, reviews) {
  if (!reviews.length) { card.append(node('p', 'No buyer review excerpts were collected for this product.')); return; }
  const details = node('details', undefined, 'extra-fields');
  details.append(node('summary', `Read ${Math.min(3, reviews.length)} of ${reviews.length} sampled reviews`));
  reviews.slice(0, 3).forEach((review) => {
    const excerpt = node('article');
    excerpt.append(node('h4', review.title || 'Sampled buyer review'));
    const meta = [review.rating ? `Rating: ${review.rating}` : null, review.date, review.variant].filter(Boolean);
    if (meta.length) excerpt.append(node('p', meta.join(' · ')));
    const body = String(review.body || 'No review text recorded.');
    excerpt.append(node('p', body.length > 600 ? `${body.slice(0, 600)}…` : body));
    const link = externalLink(review.url, `Review ${review.id} ↗`); if (link) excerpt.append(link);
    details.append(excerpt);
  });
  card.append(details);
}

function renderRecommendations(recommendations, evidence) {
  $('recommendations').replaceChildren();
  if (!recommendations.length) { $('recommendations').append(empty('The available evidence did not support a recommendation. Review the missing sources below.')); return; }
  recommendations.slice(0, 3).forEach((item, index) => {
    const id = String(item.id ?? `action-${index + 1}`); const row = node('div', undefined, 'recommendation');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = `action-${index}`; checkbox.value = id; checkbox.checked = app.selected.has(id);
    checkbox.addEventListener('change', () => { if (checkbox.checked) app.selected.add(id); else app.selected.delete(id); updateReviewState(); });
    const content = node('div'); const label = node('label', item.title || item.proposed_action || 'Suggested follow-up'); label.htmlFor = checkbox.id;
    content.append(label, node('p', item.reason || item.interpretation || 'Inspect the source evidence before choosing this action.'));
    const links = node('div', undefined, 'evidence-links');
    (item.evidence_ids || []).forEach((sourceId) => {
      const link = sourceLink(sourceId, evidence); if (link) links.append(link);
    });
    content.append(links); row.append(checkbox, content); $('recommendations').append(row);
  });
}

function renderEvidence(evidence) {
  $('evidence-count').textContent = `${evidence.length} source records`;
  $('evidence-notebook').replaceChildren();
  evidence.forEach((record) => {
    const article = node('article', undefined, 'evidence-record'); article.id = evidenceAnchor(record.id);
    article.append(node('h4', `${record.id} · ${sourceNames[sourceGroup(record.source)] || readable(record.source)}`));
    const link = externalLink(record.url, record.url); if (link) article.append(link);
    article.append(node('p', `${readable(record.status)} · captured ${dateText(record.captured_at)}`));
    if (record.settings && Object.keys(record.settings).length) article.append(node('pre', JSON.stringify(record.settings, null, 2)));
    if (record.raw_excerpt) article.append(node('pre', record.raw_excerpt));
    const details = node('details'); details.append(node('summary', 'Inspect structured evidence'), node('pre', JSON.stringify(record.data || {}, null, 2))); article.append(details);
    $('evidence-notebook').append(article);
  });
}

function updateReviewState() {
  const count = app.selected.size;
  $('approval-count').textContent = count ? `${count} action${count === 1 ? '' : 's'} selected for approval` : 'No actions selected';
  const approval = app.run?.approval;
  const reviewMatches = Boolean(approval && approval.report_version === app.run?.report_version
    && JSON.stringify([...app.selected].sort()) === JSON.stringify([...approval.approved_action_ids].sort())
    && $('review-note').value.trim() === approval.note);
  const completed = app.run?.state === 'completed';
  $('approve-button').disabled = !completed || !app.run?.report || reviewMatches;
  $('approve-button').textContent = reviewMatches ? 'Review recorded ✓' : app.run?.mode === 'sample' ? 'Record sample review' : count ? `Approve ${count} follow-up${count === 1 ? '' : 's'}` : 'Record review · no actions approved';
  $('review-hint').textContent = reviewMatches ? 'Your decision is saved in History. No external action was executed.' : approval ? 'Your selection or note changed. Record a new review to keep your decision.' : 'Approve useful follow-ups, or record that none are ready. Your decision stays in History.';
  document.querySelectorAll('#recommendations input').forEach((input) => { input.disabled = !completed; });
}

function renderHumanLoop(run) {
  const request = run.review_request, action = request?.action_requests?.[0], contact = action?.name === 'contact_supplier';
  const waiting = run.state === 'awaiting_review' && Boolean(action), native = Boolean(request || run.approved_product || run.review_history?.length);
  const demo = app.status?.deployment?.role === 'demo', suppliers = run.suppliers?.suppliers || [];
  $('human-loop').hidden = !native;
  const retrySuppliers = run.approved_product && run.suppliers && run.suppliers.status !== 'live' && !run.outreach;
  const continueApproved = run.state === 'failed' && run.approved_product && !run.closed && !run.outreach;
  $('human-review-start').hidden = (native && !retrySuppliers && !continueApproved) || (run.state !== 'completed' && !continueApproved) || run.mode !== 'live' || !run.report || demo;
  $('human-review-start').textContent = continueApproved ? 'Continue approved workflow' : retrySuppliers ? 'Retry supplier research' : 'Review this research with the agent';
  document.querySelectorAll('#review-note, label[for="review-note"], .save-bar').forEach((el) => { el.hidden = native; });
  if (native) $('review-result').hidden = true;
  if (!native) return;
  $('human-loop-title').textContent = waiting ? contact ? 'Review the supplier message.' : 'Choose the next direction.' : 'Your decisions guide the agent.';
  $('human-loop-status').textContent = waiting ? 'Waiting for you' : readable(run.state);
  $('human-loop-description').textContent = waiting ? contact ? 'Inspect the supplier evidence and edit the message. Approval sends through configured email, or saves a draft for you to send manually.' : 'Approve a researched product to find suppliers, or send feedback to run the research again. The agent pauses here for your decision.' : run.approved_product ? `Approved product: ${run.approved_product.title || run.approved_product.asin}. ${run.approved_product.requirements || ''}` : 'Your review and feedback stay with this research run.';
  $('human-product-fields').hidden = !waiting || contact;
  $('human-message-fields').hidden = !waiting || !contact;
  $('human-decision-fields').hidden = !waiting;
  $('human-viewer-note').hidden = !demo;
  const key = `${run.id}:${run.report_version}:${request?.id || ''}`;
  if (waiting && app.humanHydrated !== key) {
    const args = action.args || {}, products = (run.evidence || []).filter((r) => sourceGroup(r.source) === 'marketplace').flatMap((r) => r.data?.products || []);
    $('human-product').replaceChildren();
    products.filter((p) => p.asin).forEach((p) => { const option = node('option', p.title || p.asin); option.value = p.asin; $('human-product').append(option); });
    if (args.product_id) $('human-product').value = args.product_id;
    $('human-requirements').value = args.requirements || run.approved_product?.requirements || '';
    $('human-keyword').value = ''; $('human-feedback').value = '';
    $('human-subject').value = args.subject || ''; $('human-body').value = args.body || '';
    const supplier = suppliers.find((s) => s.id === args.supplier_id) || {};
    $('human-recipient').textContent = `Recipient: ${supplier.email || 'Contact through the supplier website'}${supplier.name ? ` · ${supplier.name}` : ''}`;
    $('human-contact-source').replaceChildren();
    const link = externalLink(supplier.contact_url || supplier.url, 'Inspect supplier contact source ↗'); if (link) $('human-contact-source').append(link);
    app.humanHydrated = key;
  }
  $('human-approve').textContent = contact ? 'Approve message' : 'Approve product & find suppliers';
  $('human-revise').textContent = contact ? 'Send feedback & revise message' : 'Send feedback & research again';
  document.querySelectorAll('#human-decision-fields button, #human-decision-fields textarea, #human-product-fields input, #human-product-fields textarea, #human-product-fields select, #human-message-fields input, #human-message-fields textarea').forEach((el) => { el.disabled = demo || app.reviewing; });
  $('human-approve').disabled ||= !contact && !$('human-product').value;
  $('supplier-results').replaceChildren();
  if (run.suppliers) {
    $('supplier-results').append(node('h4', 'Supplier research'), node('p', readable(run.suppliers.notes || 'Review source evidence before choosing a supplier.')));
    const list = node('div', undefined, 'competitor-grid');
    suppliers.forEach((supplier) => {
      const card = node('article', undefined, 'competitor-card');
      card.append(node('h4', supplier.name), node('p', supplier.product || ''), node('p', `Verification evidence: ${readable(supplier.verification)}`));
      if (supplier.notes) card.append(node('p', readable(supplier.notes)));
      const links = node('div', undefined, 'evidence-links');
      [[supplier.url, 'Supplier page ↗'], [supplier.verification_url, 'Verification source ↗'], [supplier.contact_url, 'Contact source ↗']].forEach(([url, label]) => { const link = externalLink(url, label); if (link) links.append(link); });
      card.append(links); list.append(card);
    });
    if (!suppliers.length) list.append(empty('No supported supplier candidates were found.'));
    $('supplier-results').append(list);
  }
  $('outreach-result').replaceChildren();
  if (run.outreach) {
    const outreach = run.outreach, messages = { approved_draft: 'Message approved · send it manually below.', sent: 'Message sent after your approval.', unknown: 'Delivery outcome unknown. Check your sent mail before sending again.', rejected: 'Supplier contact was not approved.' };
    $('outreach-result').append(node('h4', messages[outreach.status] || readable(outreach.status)), node('p', `Recipient: ${outreach.recipient || 'Supplier contact page'}`), node('strong', outreach.subject || ''), node('p', outreach.body || '', 'message-preview'));
    if (outreach.status === 'approved_draft') {
      if (typeof outreach.mailto === 'string' && /^mailto:[^\r\n]+$/i.test(outreach.mailto)) { const link = node('a', 'Open approved draft in your email app ↗', 'button secondary small'); link.href = outreach.mailto; $('outreach-result').append(link); }
      const link = externalLink(outreach.contact_url, 'Open supplier contact page ↗'); if (link) $('outreach-result').append(link);
    }
  }
  $('human-review-history').replaceChildren();
  if (run.review_history?.length) {
    $('human-review-history').append(node('h4', 'Human decisions'));
    const list = node('ol', undefined, 'observation-list');
    run.review_history.forEach((review) => list.append(node('li', `${dateText(review.at)} · ${readable(review.stage)} · ${readable(review.decision)}${review.feedback ? ` — ${review.feedback}` : ''}`)));
    $('human-review-history').append(list);
  }
}

async function reviewAgent(decision) {
  if (app.reviewing || app.status?.deployment?.role === 'demo' || app.run?.state !== 'awaiting_review') return;
  const feedback = $('human-feedback').value.trim();
  if (decision === 'revise' && !feedback) { notice('Add feedback to explain how the agent should revise its work.', true); $('human-feedback').focus(); return; }
  app.reviewing = true; renderHumanLoop(app.run);
  try {
    await post(`/api/runs/${encodeURIComponent(app.runId)}/review`, { decision, feedback, review_id: app.run.review_request.id, report_version: app.run.report_version, product_id: $('human-product').value, requirements: $('human-requirements').value.trim(), keyword: $('human-keyword').value.trim(), subject: $('human-subject').value, body: $('human-body').value });
    clearTimeout(app.poll); await pollRun(app.runId);
    notice(decision === 'reject' ? 'Your decision is saved. The workflow has stopped.' : decision === 'revise' ? 'Your feedback is saved. The agent is continuing with your direction.' : 'Your approval is saved. The agent is continuing.');
  } catch (error) { notice(error.message, true); }
  finally { app.reviewing = false; if (app.run) renderHumanLoop(app.run); }
}

function renderReview(run) {
  const review = run.approval;
  if (!review) { $('review-result').hidden = true; return; }
  const key = `${run.id}:${review.revision}`;
  if (app.reviewHydrated !== key) {
    app.selected = new Set(review.approved_action_ids || []);
    $('review-note').value = review.note || '';
    app.reviewHydrated = key;
  }
  const count = (review.approved_action_ids || []).length;
  $('review-result').hidden = false;
  $('review-result').className = `save-result${review.sample ? ' warn' : ''}`;
  $('review-result').replaceChildren(node('div', `${review.sample ? 'SAMPLE REVIEW · ' : ''}${count} follow-up${count === 1 ? '' : 's'} approved · ${dateText(review.reviewed_at)} · revision ${review.revision}`));
  if (review.note) $('review-result').append(node('p', review.note));
  if (review.sample) $('review-result').append(node('p', 'This decision uses synthetic evidence and demonstrates the review workflow only.'));
}

async function approveRun() {
  if (!app.run || $('approve-button').disabled) return;
  await buttonAction($('approve-button'), async () => {
    await post(`/api/runs/${encodeURIComponent(app.runId)}/approve`, {
      approved_action_ids: [...app.selected], note: $('review-note').value.trim(), report_version: app.run.report_version
    });
    await pollRun(app.runId);
  }, 'Recording your decision…');
  updateReviewState();
}

async function loadHistory() {
  $('history-list').replaceChildren(empty('Loading saved research runs…'));
  try {
    const result = await api('/api/runs'); const runs = result.runs || [];
    $('history-list').replaceChildren();
    if (!runs.length) { $('history-list').append(empty('Start a focused research run. It will appear here with its evidence and source status.', 'Your notebook is ready')); return; }
    runs.forEach((run) => {
      const row = node('article', undefined, 'history-row'); const main = node('div', undefined, 'history-row-main');
      main.append(node('h2', run.keyword || run.brief?.keyword || 'Research run'), node('p', `${dateText(run.created_at)} · ${run.mode === 'sample' ? 'Sample data' : 'Live mode'}${run.country ? ` · ${run.country}` : ''}`));
      if (run.reviewed_at) main.append(node('p', `Human review recorded · ${run.approved_action_count || 0} follow-up${run.approved_action_count === 1 ? '' : 's'} approved`, 'review-recorded'));
      const status = node('span', readable(run.state), `status-pill ${['failed', 'error'].includes(run.state) ? 'bad' : isTerminal(run) ? 'good' : 'warn'}`);
      const button = node('button', 'Open research ↗', 'button secondary small'); button.type = 'button'; button.addEventListener('click', () => openRun(run.id, true).catch((error) => notice(error.message, true)));
      row.append(main, status, button); $('history-list').append(row);
    });
  } catch (error) { $('history-list').replaceChildren(empty(error.message, 'History could not be loaded')); }
}

function bindEvents() {
  document.querySelectorAll('[data-view]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); navigate(link.dataset.view, true); }));
  document.querySelectorAll('[data-go]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.go, true)));
  window.addEventListener('hashchange', () => { if (['research', 'connections', 'history'].includes(location.hash.slice(1))) navigate(location.hash.slice(1)); });
  $('research-form').addEventListener('submit', startRun);
  document.querySelectorAll('[name="mode"]').forEach((input) => input.addEventListener('change', updateMode));
  $('refresh-status').addEventListener('click', () => buttonAction($('refresh-status'), refreshStatus, 'Checking…'));
  $('refresh-history').addEventListener('click', loadHistory);
  $('approve-button').addEventListener('click', approveRun);
  ['approve', 'revise', 'reject'].forEach((decision) => $(`human-${decision}`).addEventListener('click', () => reviewAgent(decision)));
  $('human-review-start').addEventListener('click', () => buttonAction($('human-review-start'), async () => {
    await post(`/api/runs/${encodeURIComponent(app.runId)}/review-start`);
    clearTimeout(app.poll); await pollRun(app.runId);
    notice('The agent is continuing from your saved research and approvals.');
  }, 'Opening agent review…'));
  $('human-product').addEventListener('change', () => { if (app.run) renderHumanLoop(app.run); });
  $('review-note').addEventListener('input', updateReviewState);
  $('cancel-button').addEventListener('click', () => {
    if (app.status?.deployment?.role === 'demo' && app.run?.mode === 'live') return;
    return buttonAction($('cancel-button'), async () => { await post(`/api/runs/${encodeURIComponent(app.runId)}/cancel`); await pollRun(app.runId); }, 'Cancelling…');
  });
  $('attach-browser').addEventListener('click', () => buttonAction($('attach-browser'), async () => { const result = await post('/api/browser/attach'); $('browser-detail').textContent = JSON.stringify(result, null, 2); $('browser-detail').hidden = false; await refreshStatus(); }, 'Attaching…'));
  document.querySelectorAll('[data-open-source]').forEach((button) => button.addEventListener('click', () => buttonAction(button, async () => {
    const result = await post('/api/browser/open', { source: button.dataset.openSource, brief: briefFromForm() });
    $('browser-detail').textContent = JSON.stringify(result, null, 2); $('browser-detail').hidden = false;
    notice('Browser request returned. Complete any login or account challenge manually in the browser.'); await refreshStatus();
  }, 'Opening…')));
}

async function init() {
  bindEvents(); updateMode(); navigate(location.hash.slice(1));
  await refreshStatus();
}

init();
