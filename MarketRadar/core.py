"""Bounded research, persisted human decisions, and optional external copies."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
import uuid

from .fastn import FastnClient
from .settings import Settings

FINAL_STATES = ('completed', 'failed', 'cancelled', 'interrupted', 'awaiting_review')


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_brief(raw):
    if not isinstance(raw, dict):
        raise ValueError('A research brief must be a JSON object.')
    keyword = str(raw.get('keyword', '')).strip()
    if not 2 <= len(keyword) <= 160:
        raise ValueError('Enter a product keyword between 2 and 160 characters.')
    marketplace = raw.get('marketplace', 'amazon')
    if marketplace != 'amazon':
        raise ValueError('This workflow researches Amazon.com products.')
    country = str(raw.get('country', 'US')).upper()
    if not re.fullmatch('[A-Z]{2}', country):
        raise ValueError('Country must be a two-letter code such as US or PK.')
    urls = raw.get('competitor_urls', [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
    if not isinstance(urls, list) or len(urls) > 3:
        raise ValueError('Provide at most three competitor URLs.')
    product_url = str(raw.get('product_url', '')).strip()
    from .browser import safe_marketplace_url
    for url in ([product_url] if product_url else []) + urls:
        if not isinstance(url, str) or len(url) > 2000:
            raise ValueError('Invalid marketplace URL.')
        checked = safe_marketplace_url(url)
        if not re.search(r'/(?:dp|gp/product)/[A-Z0-9]{10}(?:/|$)', urlsplit(checked).path):
            raise ValueError('Use an Amazon.com product link containing a ten-character ASIN.')
    language = str(raw.get('language', 'English')).strip()
    if not 1 <= len(language) <= 60:
        raise ValueError('Enter a valid language.')
    date_range = str(raw.get('date_range', 'today 12-m')).strip()
    if not re.fullmatch(r'(today \d{1,3}-[mdy]|now \d{1,3}-[dH]|\d{4}-\d{2}-\d{2} \d{4}-\d{2}-\d{2})', date_range):
        raise ValueError('Use a Trends date range such as today 12-m or two ISO dates.')
    try:
        steps = int(raw.get('max_browser_steps', 6))
    except (TypeError, ValueError):
        raise ValueError('Invalid browser step limit.') from None
    return {'keyword': keyword, 'marketplace': marketplace, 'country': country,
            'language': language, 'date_range': date_range, 'product_url': product_url,
            'competitor_urls': urls, 'max_browser_steps': max(1, min(8, steps))}


def validate_report(value, evidence):
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str):
        raise ValueError('The model did not return a structured research report.')
    allowed_ids = {e['id'] for e in evidence if e.get('status') in ('live', 'sample', 'imported')}
    output = {'summary': value['summary'][:5000]}
    for key in ('observations', 'limitations'):
        items = value.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError(f'Report {key} must be a list of text observations.')
        output[key] = [i[:2500] for i in items[:20]]
    from subagents.amazon_market_agent_v2 import validate_findings
    output.update(validate_findings(value, evidence))
    recs = value.get('recommendations', [])
    if not isinstance(recs, list) or len(recs) > 3:
        raise ValueError('The report must contain at most three recommendations.')
    output['recommendations'] = []
    ids = set()
    for index, rec in enumerate(recs):
        if not isinstance(rec, dict):
            raise ValueError('Invalid recommendation.')
        refs = rec.get('evidence_ids', [])
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r in allowed_ids for r in refs):
            raise ValueError('Every recommendation must cite available source evidence IDs.')
        identity = str(rec.get('id') or f'action-{index + 1}')
        if len(identity) > 80:
            identity = identity[:70] + f'-{index + 1}'
        if identity in ids or not isinstance(rec.get('title'), str) or not isinstance(rec.get('reason'), str):
            raise ValueError('Invalid or duplicate recommendation.')
        ids.add(identity)
        output['recommendations'].append({'id': identity, 'title': rec['title'][:300],
                                          'reason': rec['reason'][:2500], 'evidence_ids': refs})
    return output


def sample_evidence(brief):
    settings = {k: brief[k] for k in ('country', 'language', 'date_range')}
    common = {'status': 'sample', 'captured_at': now(), 'settings': settings,
              'raw_excerpt': 'Synthetic demonstration data. Not collected from Google or a marketplace.'}
    product = {'asin': 'SAMPLE0001', 'title': 'Example travel mug (synthetic)', 'url': 'https://www.amazon.com/',
               'price': '24.99 (synthetic)', 'currency': 'USD', 'rating': '4.2 (synthetic)',
               'review_count': '120 (synthetic)', 'demand_signal': None,
               'features': ['Insulated body and removable lid (synthetic)'],
               'reviews': [{'id': 'sample-review-1', 'title': 'Synthetic review', 'rating': '2 (synthetic)',
                            'body': 'Synthetic example: the lid leaked in my bag.', 'url': 'https://www.amazon.com/'},
                           {'id': 'sample-review-2', 'title': 'Synthetic request', 'rating': '3 (synthetic)',
                            'body': 'Synthetic example: I wish the lid were easier to take apart for cleaning.',
                            'url': 'https://www.amazon.com/'}]}
    return [
        dict(common, id='sample-planner', source='keyword_planner', url='https://ads.google.com/aw/keywordplanner/home',
             data={'keywords': [{'keyword': brief['keyword'], 'volume': '1K–10K (synthetic)',
                                 'competition': 'Medium (synthetic)', 'bid': None, 'currency': None}],
                   'notes': ['Synthetic ranges demonstrate display only.']}),
        dict(common, id='sample-trends', source='google_trends', url='https://trends.google.com/trends/explore',
             data={'trends': [{'date': f'2026-{m:02d}', 'value': v, 'term': brief['keyword']}
                              for m, v in enumerate([32, 37, 36, 48, 52, 58, 54, 65], 1)],
                   'notes': ['Synthetic normalized series; not actual search interest.']}),
        dict(common, id='sample-market', source='marketplace', url='https://www.amazon.com/',
             data={'products': [product], 'competitors': [product], 'searches': [brief['keyword']],
                   'notes': ['Sample product positioning, not market evidence.']})]


class Radar:
    def __init__(self, directory, model_factory=None, browser_factory=None, fastn_factory=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.directory)
        self.lock = threading.RLock()
        self.threads, self.stops = {}, {}
        self.model_factory = model_factory
        self.browser_factory = browser_factory
        self.fastn_factory = fastn_factory or FastnClient
        for path in self.directory.glob('run-*.json'):
            run = json.loads(path.read_text(encoding='utf-8'))
            if run['state'] not in FINAL_STATES:
                run['state'] = 'interrupted'
                run['error'] = 'The local server stopped before this run finished. Start a new run.'
                self._write(run)

    def _path(self, identity):
        if not re.fullmatch('[a-f0-9]{32}', identity):
            raise ValueError('Invalid run ID.')
        return self.directory / ('run-' + identity + '.json')

    def _write(self, run):
        with self.lock:
            path = self._path(run['id'])
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)

    def get(self, identity):
        with self.lock:
            path = self._path(identity)
            if not path.exists():
                raise ValueError('Research run not found.')
            return json.loads(path.read_text(encoding='utf-8'))

    def list(self):
        with self.lock:
            runs = [json.loads(p.read_text(encoding='utf-8')) for p in self.directory.glob('run-*.json')]
        return sorted([{'id': r['id'], 'keyword': r['brief']['keyword'], 'state': r['state'],
                        'created_at': r['created_at'], 'mode': r['mode'],
                        'reviewed_at': (r.get('approval') or {}).get('reviewed_at'),
                        'approved_action_count': len((r.get('approval') or {}).get('approved_action_ids', []))}
                       for r in runs],
                      key=lambda r: r['created_at'], reverse=True)[:100]

    def _change(self, identity, **values):
        with self.lock:
            run = self.get(identity)
            run.update(values)
            run['updated_at'] = now()
            self._write(run)
            return run

    def _event(self, identity, stage, message, **fields):
        with self.lock:
            run = self.get(identity)
            event = {'time': now(), 'stage': stage, 'message': str(message)[:1600]}
            if fields.get('usage'):
                event['usage'] = fields['usage']
            run['events'] = (run['events'] + [event])[-200:]
            self._write(run)

    def _create(self, brief, mode, use_fastn):
        run = {'id': uuid.uuid4().hex, 'brief': brief, 'mode': mode, 'use_fastn': use_fastn,
               'state': 'queued', 'created_at': now(), 'report_version': 1, 'events': [],
               'evidence': [], 'report': None, 'error': None, 'approval': None,
               'approval_history': [], 'sync': {'status': 'not_saved'}}
        self._write(run)
        return run

    def start(self, raw):
        brief = validate_brief(raw)
        mode = raw.get('mode', 'live')
        if mode not in ('sample', 'live'):
            raise ValueError('Choose live research or the labeled sample demonstration.')
        use_fastn = raw.get('use_fastn', False)
        if type(use_fastn) is not bool:
            raise ValueError('use_fastn must be a boolean.')
        with self.lock:
            if any(t.is_alive() for t in self.threads.values()):
                raise ValueError('One research run is already active; it owns the shared browser session.')
            if mode == 'live' and use_fastn and not self.settings.public()['configured']:
                raise ValueError('Configure fastn to use optional enrichment, or leave use_fastn off.')
            run = self._create(brief, mode, use_fastn if mode == 'live' else False)
            self.stops[run['id']] = threading.Event()
            thread = threading.Thread(target=self._execute, args=(run['id'],), daemon=True)
            self.threads[run['id']] = thread
            thread.start()
        return run

    def wait(self, identity, timeout=5):
        self.threads[identity].join(timeout)

    def cancel(self, identity):
        if identity in self.stops:
            self.stops[identity].set()
        self._event(identity, 'cancelling', 'Cancellation requested; stopping the active subprocess.')
        return self.get(identity)

    def _execute(self, identity, decision=None):
        stop = self.stops[identity]
        run = self.get(identity)
        brief = run['brief']
        def check():
            if stop.is_set():
                raise RuntimeError('Research cancelled.')
        def emit(kind, **fields):
            if isinstance(kind, dict):
                stage = kind.get('source')
                if stage in ('research_agent', 'opportunity_agent'):
                    self._change(identity, state='analyzing' if stage == 'research_agent' else 'recommending')
                self._event(identity, kind.get('source', kind.get('type', 'browser')),
                            kind.get('message', kind.get('status', 'Browser observation')))
            else:
                self._event(identity, kind, fields.get('detail', fields.get('message', kind)), usage=fields.get('usage'))
        try:
            if run['mode'] == 'sample':
                evidence = sample_evidence(brief)
                report = validate_report({'summary': 'Sample walkthrough: these observations and metrics are synthetic.',
                    'observations': ['The sample shows the shape of a combined research brief.'],
                    'limitations': ['No browser collection, Codex turn, or fastn call was made.'],
                    'products_in_demand': [{'title': 'Synthetic keyword demand candidate',
                        'detail': 'Example Planner volume: 1K–10K (synthetic). This is not measured product sales.',
                        'evidence_ids': ['sample-planner'], 'review_ids': []}],
                    'complaints': [{'title': 'Synthetic example: a leaking lid',
                        'detail': 'One synthetic buyer reports that the lid leaked in a bag.',
                        'evidence_ids': ['sample-market'], 'review_ids': ['sample-review-1']}],
                    'requested_features': [{'title': 'Synthetic example: easier lid cleaning',
                        'detail': 'One synthetic buyer explicitly requests a lid that comes apart more easily.',
                        'evidence_ids': ['sample-market'], 'review_ids': ['sample-review-2']}],
                    'market_gaps': [{'title': 'Synthetic example: test a better lid',
                        'detail': 'Hypothesis: a leak-resistant lid with easier cleaning could address these example complaints.',
                        'evidence_ids': ['sample-market'], 'review_ids': ['sample-review-1', 'sample-review-2']}],
                    'recommendations': [{'id': 'sample-next', 'title': 'Run a live comparison after connecting your tools',
                                         'reason': 'Synthetic data cannot justify a seller decision.',
                                         'evidence_ids': ['sample-planner', 'sample-trends']}]}, evidence)
                self._event(identity, 'sample', 'Loaded synthetic example data. No external tools or model quota used.')
                self._change(identity, evidence=evidence, report=report, state='completed')
                return
            self._change(identity, state='loading')
            if run['use_fastn'] and decision is None:
                client = self.fastn_factory(self.settings.get())
                self._event(identity, 'fastn', 'Loading research context through the configured fastn MCP tool.')
                loaded = client.load_brief(brief, identity)
                check()
                # Workspace tools may return a canonical brief or extra contextual records.
                if isinstance(loaded, dict) and isinstance(loaded.get('brief'), dict):
                    brief = validate_brief(dict(brief, **loaded['brief']))
                    self._change(identity, brief=brief)
                self._change(identity, fastn_context=loaded)
            else:
                self._event(identity, 'research_brief', 'Using the research brief you supplied; optional fastn enrichment is off.')
            if self.model_factory:
                model = self.model_factory(emit=emit, max_calls=30)
            else:
                from models import get_research_model
                model = get_research_model(emit=emit, max_calls=30)
            check()
            if self.browser_factory:
                browser = self.browser_factory(emit=emit)
            else:
                from .browser import BrowserCollector
                browser = BrowserCollector(emit=emit)
            self._change(identity, state='collecting')
            from .agents import run_research
            agent_brief = dict(brief)
            workspace_context = self.get(identity).get('fastn_context')
            if workspace_context is not None:
                agent_brief['workspace_context'] = workspace_context
            from langgraph.checkpoint.sqlite import SqliteSaver
            with SqliteSaver.from_conn_string(str(self.directory / 'checkpoints.sqlite')) as saver:
                evidence, report = run_research(agent_brief, model, browser, stop=stop, emit=emit,
                    on_evidence=lambda records: self._change(identity, evidence=records),
                    session=run, checkpointer=saver, resume=decision,
                    on_update=lambda **fields: self._change(identity, **fields))
            successes = [e for e in evidence if e.get('status') in ('live', 'imported')]
            check()
            if any(e.get('status') not in ('live', 'imported') for e in evidence):
                report['limitations'].append('Some sources were unavailable; this is a partial evidence report.')
            for source in successes:
                unverified = [key for key, verified in source.get('settings_verified', {}).items() if not verified]
                if unverified:
                    report['limitations'].append(f"{source['source']}: requested {', '.join(unverified)} settings were not confirmed in the source UI.")
            report['limitations'] = list(dict.fromkeys(report['limitations']))
            pending = self.get(identity).get('state') == 'awaiting_review'
            self._change(identity, report=report, state='awaiting_review' if pending else 'completed')
            if not pending:
                self._event(identity, 'completed', 'Workflow finished. Human decisions and any supplier handoff are saved in History.')
        except Exception as exc:
            message = str(exc)
            secret = self.settings.get().get('api_key', '')
            if secret:
                message = message.replace(secret, '[redacted]')
            self._change(identity, state='cancelled' if stop.is_set() else 'failed', error=message[:2500])
            self._event(identity, 'failed', message)

    def begin_review(self, identity):
        """Continue a saved live report with native approval, preserving its capture dates."""
        with self.lock:
            run = self.get(identity)
            retry_suppliers = bool(run.get('approved_product') and run.get('suppliers')
                and run['suppliers'].get('status') != 'live' and not run.get('outreach'))
            continue_approved = bool(run['state'] == 'failed' and run.get('approved_product')
                and not run.get('closed') and not run.get('outreach'))
            if (run['mode'] != 'live' or not run.get('report')
                    or (run['state'] != 'completed' and not continue_approved)
                    or ((run.get('review_history') or run.get('closed')) and not (retry_suppliers or continue_approved))):
                raise ValueError('Choose a completed live report or a failed approved workflow without an outreach attempt.')
            if any(thread.is_alive() for thread in self.threads.values()):
                raise ValueError('Another research action is still running.')
            self._change(identity, state='queued', closed=False, error=None,
                         **({'suppliers': None} if retry_suppliers else {}))
            self._event(identity, 'human_review', 'Continuing the workflow with the existing human product approval.' if retry_suppliers or continue_approved
                        else 'Continuing the saved report with its original evidence capture dates; sources are not refreshed until you request a revision.')
            self.stops[identity] = threading.Event()
            thread = threading.Thread(target=self._execute, args=(identity,), daemon=True)
            self.threads[identity] = thread
            thread.start()
            return self.get(identity)

    def review(self, identity, body):
        """Resume the exact DeepAgents interrupt with a human decision, never a model decision."""
        with self.lock:
            run = self.get(identity)
            request = run.get('review_request') or {}
            if (run['state'] != 'awaiting_review' or body.get('review_id') != request.get('id')
                    or type(body.get('report_version')) is not int or body['report_version'] != run['report_version']):
                raise ValueError('This review is stale. Reload the pending proposal before deciding.')
            if any(thread.is_alive() for thread in self.threads.values()):
                raise ValueError('Another research action is still running.')
            decision, feedback = body.get('decision'), body.get('feedback', '')
            if decision not in ('approve', 'revise', 'reject') or not isinstance(feedback, str) or len(feedback) > 2000:
                raise ValueError('Choose approve, revise or reject, with feedback of at most 2000 characters.')
            action = request['action_requests'][0]
            name, args = action['name'], dict(action['args'])
            fields = {'state': 'queued', 'review_request': None, 'error': None}
            if decision == 'approve':
                if name == 'approve_product':
                    args = {'product_id': body.get('product_id', args.get('product_id')),
                            'requirements': body.get('requirements', args.get('requirements', ''))}
                    products = [p for r in run['evidence'] if r['source'] == 'marketplace'
                                for p in r.get('data', {}).get('products', [])]
                    if not any(p.get('asin') == args['product_id'] for p in products):
                        raise ValueError('Choose a product from the collected Amazon evidence.')
                    if not isinstance(args['requirements'], str) or len(args['requirements']) > 2000:
                        raise ValueError('Product requirements must be text of at most 2000 characters.')
                elif name == 'contact_supplier':
                    args = {'supplier_id': args.get('supplier_id'),
                            **{key: body.get(key, args.get(key)) for key in ('subject', 'body')}}
                    if not any(s.get('id') == args['supplier_id'] for s in (run.get('suppliers') or {}).get('suppliers', [])):
                        raise ValueError('The proposed supplier is not in the collected evidence. Request a revised proposal.')
                    if (not isinstance(args.get('subject'), str) or not 1 <= len(args['subject'].strip()) <= 200
                            or any(c in args['subject'] for c in '\r\n')
                            or not isinstance(args.get('body'), str) or not 1 <= len(args['body'].strip()) <= 5000):
                        raise ValueError('Review a valid subject and message before approving.')
                else:
                    raise ValueError('Unknown human approval gate.')
                native = {'type': 'edit', 'edited_action': {'name': name, 'args': args}}
            else:
                native = {'type': 'reject', 'message': feedback.strip() or 'The human declined this action.'}
                if decision == 'reject':
                    fields['closed'] = True
                elif not feedback.strip():
                    raise ValueError('Tell the agent what to change before requesting a revision.')
                elif name == 'approve_product':
                    redirected = body.get('keyword') and body['keyword'].strip() != run['brief']['keyword']
                    fields.update(revision_requested=True, approved_product=None, suppliers=None, outreach=None,
                                  report_version=run['report_version'] + 1,
                                  brief=validate_brief(dict(run['brief'], keyword=body.get('keyword') or run['brief']['keyword'],
                                      **({'product_url': '', 'competitor_urls': []} if redirected else {}))))
                    fields['report_history'] = (run.get('report_history', []) + [{'version': run['report_version'],
                        'report': run['report'], 'evidence': run['evidence']}])[-5:]
                else:
                    # Keep the same observed supplier candidates; the rejection goes back to the model.
                    fields['outreach'] = None
            fields['human_feedback'] = feedback.strip()
            fields['review_history'] = (run.get('review_history', []) + [{'at': now(), 'stage': name,
                'decision': decision, 'feedback': feedback.strip(), 'report_version': run['report_version'],
                'review_id': request['id'], 'approved_args': args if decision == 'approve' else None}])[-30:]
            self._change(identity, **fields)
            self._event(identity, 'human_feedback', f'Human decision: {decision}. ' + feedback.strip())
            self.stops[identity] = threading.Event()
            thread = threading.Thread(target=self._execute, args=(identity, {'decisions': [native]}), daemon=True)
            self.threads[identity] = thread
            thread.start()
            return self.get(identity)

    @staticmethod
    def _select_actions(run, approved_action_ids):
        if (not isinstance(approved_action_ids, list)
                or not all(isinstance(item, str) for item in approved_action_ids)
                or len(set(approved_action_ids)) != len(approved_action_ids)):
            raise ValueError('Select valid, distinct action IDs.')
        actions = run['report'].get('recommendations', [])
        if not set(approved_action_ids).issubset({action['id'] for action in actions}):
            raise ValueError('An approved action does not belong to this report version.')
        return [copy.deepcopy(action) for action in actions if action['id'] in approved_action_ids]

    def approve(self, identity, approved_action_ids, note='', *, report_version=None):
        """Record a human's decision in workspace history, without any external call."""
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError('Review notes must be text of at most 2000 characters.')
        with self.lock:
            run = self.get(identity)
            if run['state'] != 'completed' or not run.get('report'):
                raise ValueError('Finish research before recording your review.')
            if report_version is not None and (type(report_version) is not int or report_version != run['report_version']):
                raise ValueError('The report version changed. Review the current report before approving actions.')
            actions = self._select_actions(run, approved_action_ids)
            selected = [action['id'] for action in actions]
            previous = run.get('approval') or {}
            note = note.strip()
            if (previous.get('report_version') == run['report_version']
                    and previous.get('approved_action_ids') == selected
                    and previous.get('note') == note):
                return copy.deepcopy(previous)
            approval = {'report_version': run['report_version'], 'revision': previous.get('revision', 0) + 1,
                        'approved_action_ids': selected, 'approved_actions': actions,
                        'declined_action_ids': [action['id'] for action in run['report'].get('recommendations', [])
                                                if action['id'] not in selected],
                        'note': note, 'reviewed_at': now(), 'mode': run['mode'],
                        'sample': run['mode'] == 'sample'}
            history = (run.get('approval_history', []) + [copy.deepcopy(approval)])[-20:]
            self._change(identity, approval=approval, approval_history=history)
            label = 'Sample review recorded' if approval['sample'] else 'Human review recorded'
            self._event(identity, 'human_review', f'{label}: {len(actions)} follow-up(s) approved. No external action executed.')
            return copy.deepcopy(approval)

    def save(self, identity, approved_action_ids):
        with self.lock:
            run = self.get(identity)
            if run['mode'] != 'live' or run['state'] != 'completed':
                raise ValueError('Only completed live reports can be saved to fastn; sample data cannot be published.')
            if run.get('sync', {}).get('status') in ('saving', 'unknown'):
                raise ValueError('The previous write may have completed. Check the destination before another write.')
            actions = self._select_actions(run, approved_action_ids)
            approval = run.get('approval') or {}
            if (approval.get('report_version') != run['report_version']
                    or approval.get('approved_action_ids') != [action['id'] for action in actions]):
                raise ValueError('Record a human review of these actions before sending an external copy.')
            if run.get('sync', {}).get('status') in ('verified', 'saved_unverified'):
                if run['sync'].get('approval_revision') == approval['revision']:
                    return run['sync']
                raise ValueError('This report version already has an external copy. Start a new run to send different reviewed actions.')
            if not self.settings.public()['configured']:
                raise ValueError('Configure fastn load/store workflows before saving.')
            payload = {'run_id': identity, 'brief': run['brief'], 'report_version': run['report_version'],
                       'evidence': run['evidence'], 'report': run['report'],
                       'approved_actions': actions, 'human_review': approval}
            self._change(identity, sync={'status': 'saving'})
        try:
            result = self.fastn_factory(self.settings.get()).store_run(payload)
        except Exception:
            self._change(identity, sync={'status': 'unknown', 'detail': 'Write outcome is uncertain. Inspect fastn executions/destination before retrying.'})
            raise
        result = dict(result, approval_revision=approval['revision'])
        self._change(identity, sync=result)
        self._event(identity, 'fastn_saved', result.get('detail', 'fastn returned a write result.'))
        return result
