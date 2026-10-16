"""Subscription provider checks; all normal tests use subprocess doubles."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from MarketRadar.models import CodexModel, _terminate_process_tree


def events(answer='{"ok":true}', usage=None):
    return '\n'.join(json.dumps(event) for event in [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': answer}},
        {'type': 'turn.completed', 'usage': usage or {'input_tokens': 12, 'output_tokens': 3}},
    ])


class FakeProcess:
    def __init__(self, output='', errors='', returncode=0, pending=False):
        self.output, self.errors = output, errors
        self.returncode = None if pending else returncode
        self.pid = 123456
        self.inputs = []
        self.killed = False

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        if self.returncode is None:
            raise subprocess.TimeoutExpired('codex', timeout)
        return self.output, self.errors

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class CodexModelTests(unittest.TestCase):
    def setup_provider(self, output=None):
        login = FakeProcess('Logged in using ChatGPT')
        turn = FakeProcess(events() if output is None else output)
        return login, turn

    def test_json_answer_usage_and_isolated_subscription_command(self):
        login, turn = self.setup_provider()
        commands = []
        def launch(command, **kwargs):
            commands.append((command, kwargs))
            if 'login' in command:
                return login
            folder = Path(command[command.index('--cd') + 1])
            self.assertEqual(list(folder.iterdir()), [folder / 'instructions.md'])
            instructions = (folder / 'instructions.md').read_text(encoding='utf-8')
            self.assertIn('Custom analyst policy', instructions)
            self.assertNotIn('Sensitive supplied context', instructions)
            return turn
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=launch), patch.dict(os.environ, {'OPENAI_API_KEY': 'not-used', 'CODEX_API_KEY': 'not-used', 'OPENAI_BASE_URL': 'not-used'}):
            model = CodexModel(model='test-model')
            result = model.complete_json('Custom analyst policy', 'Sensitive supplied context')
        self.assertEqual(result, {'ok': True})
        self.assertEqual(model.last_usage['total_tokens'], 15)
        self.assertEqual(model.calls_used, 1)
        command, options = commands[-1]
        for flag in ('--ignore-user-config', '--ephemeral', '--json', '--skip-git-repo-check', 'forced_login_method="chatgpt"', 'features.shell_tool=false', 'features.plugins=false', 'features.skip_host_skill_discovery=true'):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertEqual(command[command.index('--model') + 1], 'test-model')
        self.assertIn('Sensitive supplied context', turn.inputs[0])
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'):
            self.assertNotIn(key, options['env'])
        self.assertFalse(folder_exists(command))

    def test_api_login_never_starts_a_model_call(self):
        login = FakeProcess('Logged in using an API key')
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', return_value=login) as launch:
            model = CodexModel()
            with self.assertRaisesRegex(RuntimeError, 'ChatGPT'):
                model.complete_json('policy', 'task')
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(model.calls_used, 0)

    def test_health_checks_login_without_consuming_a_turn(self):
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', return_value=FakeProcess('Logged in using ChatGPT')) as launch:
            model = CodexModel()
            health = model.health()
        self.assertTrue(health['available'])
        self.assertTrue(health['authenticated'])
        self.assertEqual(model.calls_used, 0)
        self.assertEqual(launch.call_count, 1)
        self.assertIn('login', launch.call_args.args[0])

    def test_missing_cli_health_is_unavailable(self):
        with patch('MarketRadar.models.shutil.which', return_value=None):
            self.assertFalse(CodexModel().health()['available'])

    def test_empty_optional_model_setting_uses_the_default(self):
        with patch.dict(os.environ, {'MARKETRADAR_CODEX_MODEL': ''}):
            self.assertEqual(CodexModel().model, 'gpt-6-luna')

    def test_rejects_failed_incomplete_non_object_and_tool_output(self):
        failures = [
            json.dumps({'type': 'turn.failed', 'error': {'message': 'quota exhausted'}}),
            json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}}),
            events('[]'), events('not JSON'), events('{"x":NaN}'),
            json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}}) + '\n' + events(),
        ]
        for output in failures:
            with self.subTest(output=output):
                login, turn = self.setup_provider(output)
                with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=[login, turn]) as launch:
                    model = CodexModel()
                    with self.assertRaises((RuntimeError, ValueError)):
                        model.complete_json('policy', 'task')
                    self.assertEqual(launch.call_count, 2, 'Failures must not trigger retries')

    def test_budget_blocks_second_turn_before_subprocess(self):
        login, turn = self.setup_provider()
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=[login, turn]) as launch:
            model = CodexModel(max_calls=1)
            model.complete_json('policy', 'task')
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                model.complete_json('policy', 'task')
        self.assertEqual(launch.call_count, 2)

    def test_cancelled_before_launch_does_not_consume_quota(self):
        stop = threading.Event()
        stop.set()
        with patch('MarketRadar.models.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                CodexModel().complete_json('policy', 'task', stop=stop)
        self.assertEqual(launch.call_count, 0)

    def test_cancellation_terminates_the_running_process_tree(self):
        stop = threading.Event()
        login, turn = self.setup_provider()
        turn.returncode = None
        def launch(command, **kwargs):
            if 'login' in command:
                return login
            stop.set()
            return turn
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=launch), patch('MarketRadar.models._terminate_process_tree', side_effect=lambda process: process.kill()) as terminate:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                CodexModel().complete_json('policy', 'task', stop=stop)
        self.assertTrue(turn.killed)
        terminate.assert_called_once_with(turn)

    def test_timeout_in_login_terminates_without_model_call(self):
        login = FakeProcess(pending=True)
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', return_value=login) as launch, patch('MarketRadar.models._terminate_process_tree', side_effect=lambda process: process.kill()) as terminate:
            with self.assertRaises(TimeoutError):
                CodexModel().complete_json('policy', 'task', timeout=.01)
        self.assertEqual(launch.call_count, 1)
        terminate.assert_called_once_with(login)

    def test_timeout_during_inference_terminates_and_keeps_call_accounted(self):
        login, _ = self.setup_provider()
        turn = FakeProcess(pending=True)
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=[login, turn]), patch('MarketRadar.models._terminate_process_tree', side_effect=lambda process: process.kill()) as terminate:
            model = CodexModel()
            with self.assertRaises(TimeoutError):
                model.complete_json('policy', 'task', timeout=.01)
        self.assertEqual(model.calls_used, 1)
        terminate.assert_called_once_with(turn)

    def test_windows_termination_targets_child_tree(self):
        process = FakeProcess(pending=True)
        with patch('MarketRadar.models.os.name', 'nt'), patch('MarketRadar.models.subprocess.run') as kill_tree:
            _terminate_process_tree(process)
        self.assertEqual(kill_tree.call_args.args[0], ['taskkill', '/PID', str(process.pid), '/T', '/F'])
        self.assertTrue(process.killed)

    def test_separate_instances_serialize_model_turns(self):
        first_started, second_started, release_first = threading.Event(), threading.Event(), threading.Event()
        outputs, errors = [], []
        class WaitingProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                if not release_first.wait(timeout):
                    raise subprocess.TimeoutExpired('codex', timeout)
                return super().communicate(input, timeout)
        turns = 0
        def launch(command, **kwargs):
            nonlocal turns
            if 'login' in command:
                return FakeProcess('Logged in using ChatGPT')
            turns += 1
            if turns == 1:
                first_started.set()
                return WaitingProcess(events())
            second_started.set()
            return FakeProcess(events())
        def invoke():
            try:
                outputs.append(CodexModel().complete_json('policy', 'task', timeout=5))
            except Exception as exc:
                errors.append(exc)
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=launch):
            first, second = threading.Thread(target=invoke), threading.Thread(target=invoke)
            first.start()
            self.assertTrue(first_started.wait(1))
            second.start()
            try:
                self.assertFalse(second_started.wait(.05))
            finally:
                release_first.set()
                first.join(2)
                second.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(outputs, [{'ok': True}, {'ok': True}])

    def test_optional_schema_is_supplied_to_cli(self):
        login, turn = self.setup_provider()
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
        def launch(command, **kwargs):
            if 'login' in command:
                return login
            schema_path = command[command.index('--output-schema') + 1]
            self.assertEqual(json.loads(Path(schema_path).read_text(encoding='utf-8')), schema)
            return turn
        with patch('MarketRadar.models.shutil.which', return_value='codex'), patch('MarketRadar.models.subprocess.Popen', side_effect=launch):
            self.assertEqual(CodexModel().complete_json('policy', 'task', schema=schema), {'ok': True})

    def test_legacy_module_import_and_default_factory_need_no_google_credentials(self):
        import sys
        with patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': '', 'MARKETRADAR_PROVIDER': 'codex'}):
            sys.modules.pop('models', None)
            legacy = importlib.import_module('models')
            self.assertIsInstance(legacy.get_research_model(), CodexModel)
            self.assertIn('Listing Extraction Agent', legacy.system_prompt)
            with self.assertRaisesRegex(RuntimeError, 'GOOGLE_CLOUD_PROJECT'):
                legacy.get_google_model()


def folder_exists(command):
    return Path(command[command.index('--cd') + 1]).exists()


if __name__ == '__main__':
    unittest.main()
