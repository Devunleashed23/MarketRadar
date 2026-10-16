"""AF inference tests use transport doubles; no network or billable calls."""
import io
import json
import os
import threading
import unittest
import urllib.error
from unittest.mock import patch

from MarketRadar.cloud_model import CloudModel, _http_transport


CREDS = {"AF_API_KEY": "af_test_private_credential", "AF_ORG_ID": "org_private_test"}


def response(content='{"ok":true}', finish_reason="stop", **message_fields):
    return json.dumps({"choices": [{"finish_reason": finish_reason, "message": {"content": content, **message_fields}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}).encode()


class CloudModelTests(unittest.TestCase):
    def test_transport_allows_the_full_inference_timeout_before_headers(self):
        with patch('MarketRadar.cloud_model.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read1.return_value = b''
            _http_transport(urllib.request.Request('https://example.test'), 90)
        self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 90)

    def test_fixed_endpoint_json_protocol_usage_and_no_credentials_in_events(self):
        calls, events = [], []
        def transport(request, timeout):
            calls.append((request, timeout))
            return response()
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=transport, emit=lambda kind, **details: events.append((kind, details)))
            result = model.complete_json("Analyst policy", "Evidence only", timeout=3)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "https://auth.alternatefutures.ai/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CREDS["AF_API_KEY"])
        self.assertEqual(request.get_header("X-organization-id"), CREDS["AF_ORG_ID"])
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][1]["content"], "Evidence only")
        self.assertLessEqual(timeout, 3)
        self.assertEqual(model.calls_used, 1)
        self.assertEqual(model.last_usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertNotIn(CREDS["AF_API_KEY"], str(events))
        self.assertNotIn(CREDS["AF_ORG_ID"], str(events))

    def test_health_is_a_nonbillable_configuration_check(self):
        def forbidden(*args):
            raise AssertionError("health must not invoke the network")
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=forbidden)
            health = model.health()
        self.assertTrue(health["available"])
        self.assertTrue(health["configured"])
        self.assertIsNone(health["authenticated"])
        self.assertEqual(model.calls_used, 0)

    def test_missing_credentials_do_not_dispatch_or_claim_authentication(self):
        with patch.dict(os.environ, {"AF_API_KEY": "", "AF_ORG_ID": ""}):
            model = CloudModel(transport=lambda *args: self.fail("must not dispatch"))
            self.assertFalse(model.health()["available"])
            with self.assertRaisesRegex(RuntimeError, "AF_API_KEY.*AF_ORG_ID"):
                model.complete_json("policy", "evidence")
        self.assertEqual(model.calls_used, 0)

    def test_cloud_model_can_be_selected_by_environment_without_endpoint_override(self):
        with patch.dict(os.environ, dict(CREDS, MARKETRADAR_CLOUD_MODEL="gpt-5.6-sol", OPENAI_BASE_URL="https://evil.example")):
            calls = []
            model = CloudModel(transport=lambda request, timeout: calls.append(request.full_url) or response())
            model.complete_json("policy", "evidence")
        self.assertEqual(model.model, "gpt-5.6-sol")
        self.assertEqual(calls, ["https://auth.alternatefutures.ai/v1/chat/completions"])

    def test_schema_is_forwarded_as_strict_json_schema(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        sent = []
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=lambda request, timeout: sent.append(json.loads(request.data)) or response())
            model.complete_json("policy", "evidence", schema=schema)
        self.assertEqual(sent[0]["response_format"], {"type": "json_schema", "json_schema": {"name": "marketradar_response", "strict": True, "schema": schema}})

    def test_rejects_invalid_or_incomplete_json_and_never_retries(self):
        failures = [response("[]"), response("not JSON"), response('{"n":NaN}'),
                    response('{"ok":true,"ok":false}'), response('```json\n{"ok":true}\n```'),
                    response(finish_reason="length"), response(tool_calls=[{"type": "function"}]),
                    response(refusal="Cannot comply"), b'{"error":"invalid"}', b"not an envelope"]
        for payload in failures:
            with self.subTest(payload=payload), patch.dict(os.environ, CREDS):
                calls = []
                model = CloudModel(transport=lambda request, timeout: calls.append(request) or payload)
                with self.assertRaises((RuntimeError, ValueError)):
                    model.complete_json("policy", "evidence")
                self.assertEqual(len(calls), 1)
                self.assertEqual(model.calls_used, 1)

    def test_http_errors_never_expose_credentials_or_response_body(self):
        for code in [401, 403, 429, 500]:
            with self.subTest(code=code), patch.dict(os.environ, CREDS):
                calls = []
                def transport(request, timeout):
                    calls.append(request)
                    leaked = CREDS["AF_API_KEY"] + CREDS["AF_ORG_ID"]
                    raise urllib.error.HTTPError(request.full_url, code, leaked, {}, io.BytesIO(leaked.encode()))
                model = CloudModel(transport=transport)
                with self.assertRaises(RuntimeError) as caught:
                    model.complete_json("policy", "evidence")
                self.assertIn(str(code), str(caught.exception))
                self.assertNotIn(CREDS["AF_API_KEY"], str(caught.exception))
                self.assertNotIn(CREDS["AF_ORG_ID"], str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_transport_failure_is_generic_and_is_not_retried(self):
        with patch.dict(os.environ, CREDS):
            def transport(request, timeout):
                raise RuntimeError("secret " + CREDS["AF_API_KEY"])
            model = CloudModel(transport=transport)
            with self.assertRaises(RuntimeError) as caught:
                model.complete_json("policy", "evidence")
        self.assertNotIn(CREDS["AF_API_KEY"], str(caught.exception))
        self.assertEqual(model.calls_used, 1)

    def test_call_budget_blocks_a_second_dispatch(self):
        calls = []
        with patch.dict(os.environ, CREDS):
            model = CloudModel(max_calls=1, transport=lambda *args: calls.append(1) or response())
            model.complete_json("policy", "evidence")
            with self.assertRaisesRegex(RuntimeError, "budget"):
                model.complete_json("policy", "evidence")
        self.assertEqual(calls, [1])

    def test_pre_cancelled_request_does_not_consume_a_call(self):
        stop = threading.Event()
        stop.set()
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=lambda *args: self.fail("must not dispatch"))
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                model.complete_json("policy", "evidence", stop=stop)
        self.assertEqual(model.calls_used, 0)

    def test_cancellation_during_started_event_still_prevents_dispatch(self):
        stop = threading.Event()
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=lambda *args: self.fail("must not dispatch"),
                               emit=lambda *args, **kwargs: stop.set())
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                model.complete_json("policy", "evidence", stop=stop)
        self.assertEqual(model.calls_used, 0)

    def test_cancellation_returns_promptly_and_blocks_overlapping_dispatch(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def transport(request, timeout):
            entered.set()
            release.wait(2)
            return response()
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=transport)
            def invoke():
                try:
                    model.complete_json("policy", "evidence", stop=stop, timeout=2)
                except RuntimeError as exc:
                    errors.append(str(exc))
            worker = threading.Thread(target=invoke)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                stop.set()
                worker.join(.5)
                self.assertFalse(worker.is_alive())
                self.assertIn("cancelled", errors[0])
                with self.assertRaisesRegex(RuntimeError, "in flight"):
                    model.complete_json("policy", "another request")
                self.assertEqual(model.calls_used, 1)
            finally:
                release.set()
                worker.join(1)

    def test_timeout_preserves_dispatched_call_accounting(self):
        release = threading.Event()
        with patch.dict(os.environ, CREDS):
            model = CloudModel(transport=lambda *args: release.wait(1) or response())
            try:
                with self.assertRaises(TimeoutError):
                    model.complete_json("policy", "evidence", timeout=.02)
                self.assertEqual(model.calls_used, 1)
            finally:
                release.set()

    def test_invalid_inputs_and_header_injection_are_rejected_before_dispatch(self):
        with patch.dict(os.environ, dict(CREDS, AF_API_KEY="bad\r\nInjected: value")):
            model = CloudModel(transport=lambda *args: self.fail("must not dispatch"))
            with self.assertRaises(RuntimeError):
                model.complete_json("policy", "evidence")
        with patch.dict(os.environ, CREDS):
            for arguments in [{"timeout": 0}, {"timeout": True}, {"schema": []}]:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    CloudModel().complete_json("policy", "evidence", **arguments)


if __name__ == "__main__":
    unittest.main()
