import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch
import provider_ops as p


class Quota(unittest.TestCase):
    def pool(self, provider):
        return NS(entries=lambda: [NS(id='a', label='LABEL', runtime_api_key='fake', base_url='exact')])

    def run_check(self, home, pct=100, reset='2030-11-18T13:57:00Z', now=1000, **kwargs):
        fetch = lambda provider, entry: {'rate_limit': {'secondary_window': {'used_percent': pct, 'reset_at': reset}}} if provider == 'openai-codex' else {}
        return p.check(home, 'fake', pools=self.pool, fetch=fetch, send=lambda token, text: (self.messages.append(text) or len(self.messages)), now=now, **kwargs)

    def setUp(self):
        self.messages = []

    def test_jitter_highest_and_shared_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_check(root+'/profiles/a'), [1])
            self.assertEqual(self.run_check(root+'/profiles/b', reset='2030-11-18T13:57:22Z', now=1300), [])
            state = json.loads((Path(root)/'provider-ops/state.json').read_text())
            self.assertEqual(len(state['sent']), 1)
            self.assertEqual(self.messages, ['Conta LABEL do Codex em 100% da janela semanal. Zera 18/11 às 10:57.'])

    def test_threshold_progression_and_stable_label(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_check(root, pct=85), [1])
            self.pool = lambda provider: NS(entries=lambda: [NS(id='changed', label='LABEL')])
            self.assertEqual(self.run_check(root, pct=85, now=1300), [])
            self.assertEqual(self.run_check(root, pct=95, now=1600), [2])
            self.assertEqual(self.run_check(root, now=1900), [])

    def test_migrate_old_state_and_moving_reset(self):
        with tempfile.TemporaryDirectory() as root:
            reset = datetime(2030, 11, 18, 13, 57, tzinfo=timezone.utc)
            old = p._hash(['openai-codex', 'a', 'secondary_window', reset.isoformat(), 95])
            folder = Path(root)/'profiles/a/provider-ops'
            folder.mkdir(parents=True)
            (folder/'state.json').write_text(json.dumps({'checked': 10, 'sent': {old: 763}}))
            dest = Path(root)/'provider-ops'
            dest.mkdir()
            (dest/'state.json').write_text(json.dumps({'checked': 20, 'sent': {'other': 123}}))
            state = p.migrate_state(root)
            self.assertEqual(state, {'checked': 20, 'sent': {'other': 123, old: 763}})
            self.assertEqual(self.run_check(root, reset='2030-11-18T13:57:22Z'), [])
            state = json.loads((dest/'state.json').read_text())
            self.assertEqual(list(state['sent'].values()).count(763), 2)
            self.assertEqual(set(state['sent'].values()), {123, 763})
            self.assertEqual(self.run_check(root, reset='2030-11-18T13:57:44Z', now=1300), [])

    def test_initial_stamp_before_fetch_and_interval(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            def fetch(*args):
                calls.append(json.loads((Path(root)/'provider-ops/state.json').read_text())['checked'])
                raise RuntimeError('fake')
            for now in (1000, 1299, 1300):
                p.check(root, 'fake', pools=self.pool, fetch=fetch, send=lambda *a: self.fail('send'), now=now)
            self.assertEqual(calls, [1000, 1000, 1300, 1300])

    def test_percent_not_fraction_and_overrides(self):
        self.assertEqual(list(p.windows('anthropic', {'five_hour': {'utilization': .95, 'resets_at': '2030-01-01T00:00:00Z'}}))[0][1], .95)
        self.assertEqual(list(p.windows('openai-codex', {'rate_limit': {'primary_window': {'used_percent': .5, 'reset_at': 1900000000}}}))[0][1], .5)
        with self.assertRaises(ValueError):
            p.check('/unused', 'fake', thresholds=(1,))
        with self.assertRaises(ValueError):
            p.check('/unused', 'fake', interval=0)
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_check(root, pct=1, thresholds=(1,), interval=0, test=True), [1])
            self.assertTrue(self.messages[0].startswith('TESTE '))

    def test_hook_owner_only_manu(self):
        constants = NS(get_default_hermes_root=lambda: '/opt/manuella')
        shared = NS(get_scoped_secret=lambda *args: 'fake')
        with patch.dict(sys.modules, {'hermes_constants': constants, 'gateway.platforms._shared': shared}), patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'read_text', return_value='manu'), patch.object(p.threading, 'Thread') as thread:
            p.post_llm_call()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            thread.reset_mock()
            constants.get_default_hermes_root = lambda: '/opt/luna'
            p.post_llm_call()
            thread.assert_not_called()
            constants.get_default_hermes_root = lambda: '/opt/manuella'
            with patch.object(Path, 'read_text', return_value='manu\n'):
                p.post_llm_call()
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
