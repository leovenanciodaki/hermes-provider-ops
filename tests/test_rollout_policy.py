"""Hermetic policy regressions. No SSH, Docker daemon or provider is contacted."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_worker as w
import rollout
import turn_probe as probe

NOW = '2026-11-16T20:00:00+00:00'
SINCE = w.timestamp(NOW)


def state(platforms, pid=2, updated=NOW):
    return {'pid': pid, 'start_time': 10, 'updated_at': updated,
            'code_version': '0.23.1', 'gateway_state': 'running', 'platforms': platforms}


class PolicyTests(unittest.TestCase):
    def test_old_job_and_dead_writer_are_ignored(self):
        before = {'h': state({'telegram': {'state': 'connected'}})}
        for after, processes in [
            (state({'telegram': {'state': 'fatal'}}, updated='2026-11-10T00:00:00+00:00'), {'2': 10}),
            (state({'telegram': {'state': 'fatal', 'writer_pid': 99, 'writer_start_time': 10}}), {'2': 10}),
            (state({'telegram': {'state': 'fatal'}}), {}),
        ]:
            with self.subTest(after=after):
                self.assertEqual(w.platform_regressions(before, {'h': after}, SINCE, processes), {})

    def test_preexisting_faults_do_not_fail_but_new_fault_is_partial(self):
        before = {'h': state({'telegram': {'state': 'fatal'}, 'api_server': {'state': 'connected'}})}
        self.assertEqual(w.platform_regressions(before, before, SINCE, {'2': 10}), {})
        after = {'h': state({'api_server': {'state': 'fatal'}})}
        self.assertEqual(w.platform_regressions(before, after, SINCE, {'2': 10}), {'h': ['api_server']})
        self.assertTrue(w.ready(before, before, SINCE))  # Same live PID is not a veto.

    def test_acceptance_does_not_require_status_files(self):
        evidence = {'pid': 44, 'version': '0.23.1', 'output': 'FROTA_OK'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'states', return_value={}), patch.object(w, 'homes', return_value=[Path('/home')]), patch.object(w, 'run', return_value=json.dumps(evidence)) as run:
            self.assertEqual(w.accept_turn(Path('/home'), None, Path(tmp)), evidence)
            self.assertEqual(json.loads((Path(tmp) / 'turn.json').read_text())['pid'], 44)
            run.assert_called_once()

    def test_candidates_stop_after_first_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'states', return_value={}), patch.object(w, 'homes', return_value=[Path('/a'), Path('/b')]), patch.object(w, 'run', side_effect=[RuntimeError('API disabled'), json.dumps({'pid': 2, 'version': '0.23.1', 'output': 'ok'})]) as run:
            w.accept_turn(Path('/'), None, Path(tmp))
            self.assertEqual(run.call_count, 2)

    def test_evidence_write_failure_is_not_total_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'states', return_value={}), patch.object(w, 'homes', return_value=[Path('/a')]), patch.object(w, 'run', return_value=json.dumps({'pid': 2, 'version': '0.23.1', 'output': 'ok'})), patch.object(w, 'private', side_effect=OSError('disk full')):
            with self.assertRaises(w.RetainedUpdate):w.accept_turn(Path('/'), None, Path(tmp))

    def test_health_requires_live_expected_version(self):
        for health in ({'version': '0.23.0', 'pid': 1}, {'version': '0.23.1'}, {'version': '0.23.1', 'pid': -1}):
            with self.subTest(health=health), self.assertRaises(RuntimeError):probe.validate_health(health)
        with patch.object(probe.os, 'kill', side_effect=ProcessLookupError):
            with self.assertRaises(ProcessLookupError):probe.validate_health({'version': '0.23.1', 'pid': 44})

    def test_real_http_contract_with_fake_transport(self):
        def response(data):
            stream = io.BytesIO(json.dumps(data).encode())
            return stream
        health = {'version': '0.23.1', 'pid': 44, 'gateway_state': 'degraded'}
        with patch.object(probe.os, 'kill') as kill, patch.object(probe.urllib.request, 'urlopen', side_effect=[response(health), response({'choices': [{'message': {'content': 'real answer'}}]})]) as http:
            self.assertEqual(probe.live_turn(8642, 'test-key')['output'], 'real answer')
            self.assertEqual([c.args[0].get_method() for c in http.call_args_list], ['GET', 'POST'])
            self.assertEqual(kill.call_count, 1)

    def transaction(self, failure=None, turn_error=None, post_error=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        work = Path(tmp)
        d = {'Mounts': [{'Source': '/data', 'Destination': '/home/hermes/.hermes'}], 'Image': 'old', 'Config': {'User': 'root'}, 'HostConfig': {}, 'NetworkSettings': {'Networks': {}}, 'State': {'Running': False}}
        a = Namespace(target='aquario', image=None, repo_ref='a'*40, accept_turns=False, job_started_at=SINCE)
        stack.enter_context(patch.object(w, 'api', return_value=d))
        stack.enter_context(patch.object(w, 'states', return_value={}))
        stack.enter_context(patch.object(w, 'process_table', return_value={}))
        stack.enter_context(patch.object(w, 'homes', return_value=[]))
        stack.enter_context(patch.object(w, 'legacy', return_value=[]))
        stack.enter_context(patch.object(w, 'image_for', return_value='new'))
        stack.enter_context(patch.object(w, 'snapshot'))
        stack.enter_context(patch.object(w, 'log'))
        restore = stack.enter_context(patch.object(w, 'restore'))
        turn = stack.enter_context(patch.object(w, 'accept_turn', side_effect=turn_error))
        stack.enter_context(patch.object(w, 'readback', side_effect=post_error, return_value={}))
        stack.enter_context(patch.object(w, 'finish_backup', return_value={}))
        def command(argv, **kw):
            if failure and argv[:len(failure)] == failure:
                raise subprocess.TimeoutExpired(argv, 120)
            return ''
        run = stack.enter_context(patch.object(w, 'run', side_effect=command))
        return a, work, run, turn, restore

    def test_partial_readback_failure_retains_new_version_no_retry(self):
        a, work, run, turn, restore = self.transaction(post_error=RuntimeError('readback'))
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        turn.assert_called_once();restore.assert_not_called()
        self.assertEqual(sum(c.args[0][:2] == ['docker', 'stop'] for c in run.call_args_list), 1)
        self.assertEqual(sum(c.args[0][:2] == ['docker', 'start'] for c in run.call_args_list), 1)
        self.assertFalse(any(c.args[0][:3] == ['docker', 'rm', '-f'] for c in run.call_args_list))

    def test_start_timeout_with_live_turn_keeps_new_version(self):
        a, work, run, turn, restore = self.transaction(failure=['docker', 'start'])
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        turn.assert_called_once();restore.assert_not_called()

    def test_no_turn_rolls_back_once_without_update_retry(self):
        a, work, run, turn, restore = self.transaction(turn_error=w.NoRealTurn('no API response'))
        with self.assertRaises(w.GatewayUnavailable):w.docker_transaction(a, 'live', work)
        turn.assert_called_once();restore.assert_called_once()
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(commands.count(['docker', 'stop', '-t', '60', 'live']), 1)
        self.assertEqual(commands.count(['docker', 'start', 'live']), 2)  # new + old, no retry.
        stop = next(c for c in run.call_args_list if c.args[0][:2] == ['docker', 'stop'])
        self.assertGreater(stop.kwargs['timeout'], 60)

    def test_stop_timeout_restores_old_once(self):
        a, work, run, turn, restore = self.transaction(failure=['docker', 'stop'])
        with self.assertRaises(w.GatewayUnavailable):w.docker_transaction(a, 'live', work)
        turn.assert_not_called();restore.assert_called_once()
        self.assertEqual(sum(c.args[0][:2] == ['docker', 'stop'] for c in run.call_args_list), 1)

    def test_host_cooldown_force_expiry_and_active_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = base/'jobs/a'; b = base/'jobs/b'
            a.mkdir(parents=True);b.mkdir()
            with patch.object(w.time, 'time', return_value=100000):
                lock = w.acquire_lock(a)
                with self.assertRaises(w.RolloutRefused):w.acquire_lock(b, force=True)
                lock.close()
                with self.assertRaises(w.RolloutRefused):w.acquire_lock(b)
                w.acquire_lock(b, force=True).close()
            with patch.object(w.time, 'time', return_value=100000+w.COOLDOWN):
                w.acquire_lock(a).close()

    def test_force_forwarded_to_host_worker(self):
        with patch.object(rollout, 'remote', return_value=b'42') as remote:
            rollout.launch('aquario', '/job', 'a'*40, None, False, force=True)
        self.assertIn('--force', remote.call_args.args[1])

    def test_main_partial_and_catastrophic_exit_codes_without_retry(self):
        for error, expected in [(w.RetainedUpdate('partial'), 2), (w.GatewayUnavailable('total'), 1)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                with patch.object(w.Path, 'cwd', return_value=work), patch.object(w, 'acquire_lock', return_value=Mock()), patch.object(w, 'disk_check'), patch.object(w, 'log'), patch.object(w, 'service_transaction', side_effect=error) as transaction, patch.object(sys, 'argv', ['worker', '--target', 'luna', '--repo-ref', 'a'*40]):
                    self.assertEqual(w.main(), expected)
                    self.assertEqual(json.loads((work/'result.json').read_text())['exit_code'], expected)
                    transaction.assert_called_once()

    def test_partial_platform_failure_keeps_version(self):
        a, work, run, turn, restore = self.transaction()
        with patch.object(w, 'platform_regressions', return_value={'h': ['telegram']}):
            with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        turn.assert_called_once();restore.assert_not_called()

    def test_permission_timeout_before_cut_does_not_restart(self):
        a, work, run, turn, restore = self.transaction(failure=['docker', 'exec', '-u', '0', 'live', 'chown'])
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        turn.assert_not_called();restore.assert_not_called()
        self.assertFalse(any(c.args[0][:2] in (['docker', 'stop'], ['docker', 'start']) for c in run.call_args_list))

    def test_probe_deadline_after_completed_turn_is_partial(self):
        a, work, run, turn, restore = self.transaction(post_error=subprocess.TimeoutExpired('readback', 120))
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        restore.assert_not_called();turn.assert_called_once()

    def test_status_port_ignores_old_or_dead_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'gateway_state.json'
            record = state({'api_server': {'metrics': {'port': 9001}}})
            path.write_text(json.dumps(record))
            with patch.object(probe.os, 'kill'), patch.object(probe, 'process_start', return_value=10):
                self.assertEqual(probe.status_port(path, 8642, SINCE), 9001)
                self.assertEqual(probe.status_port(path, 8642, SINCE+1), 8642)
            with patch.object(probe.os, 'kill', side_effect=ProcessLookupError):
                self.assertEqual(probe.status_port(path, 8642, SINCE), 8642)
            record['platforms']['api_server']['writer_pid'] = 99
            path.write_text(json.dumps(record))
            with patch.object(probe.os, 'kill'), patch.object(probe, 'process_start', return_value=10):
                self.assertEqual(probe.status_port(path, 8642, SINCE), 8642)

    def test_baseline_diagnostics_failure_does_not_veto_update(self):
        with patch.object(w, 'process_table', side_effect=RuntimeError('diagnostic timeout')), patch.object(w, 'log'):
            self.assertEqual(w.baseline(Path('/unused'), 'unused', SINCE), {})

    def test_duplicate_worker_refusal_preserves_running_job_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with patch.object(w.Path, 'cwd', return_value=work), patch.object(w, 'acquire_lock', side_effect=w.RolloutRefused('active', active_job=str(work))), patch.object(w, 'log'), patch.object(sys, 'argv', ['worker', '--target', 'luna', '--repo-ref', 'a'*40]):
                self.assertEqual(w.main(), 3)
                self.assertFalse((work/'result.json').exists())
                self.assertFalse((work/'exit-code').exists())

    def test_refused_new_job_has_terminal_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with patch.object(w.Path, 'cwd', return_value=work), patch.object(w, 'acquire_lock', side_effect=w.RolloutRefused('cooldown')), patch.object(w, 'log'), patch.object(sys, 'argv', ['worker', '--target', 'luna', '--repo-ref', 'a'*40]):
                self.assertEqual(w.main(), 3)
                self.assertEqual(json.loads((work/'result.json').read_text())['exit_code'], 3)

    def test_http_probe_against_loopback_fixture(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):pass
            def send(self, data):
                payload = json.dumps(data).encode()
                self.send_response(200);self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)
            def do_GET(self):
                calls.append(self.path)
                self.send({'version': '0.23.1', 'pid': os.getpid(), 'gateway_state': 'degraded'})
            def do_POST(self):
                calls.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                self.send({'choices': [{'message': {'content': 'FROTA_OK'}}]})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            self.assertEqual(probe.live_turn(server.server_port, 'fixture-only')['pid'], os.getpid())
            self.assertEqual(calls, ['/health/detailed', '/v1/chat/completions'])
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_service_adapter_partial_keeps_version_total_failure_restores_once(self):
        for failure, expected in [('partial', w.RetainedUpdate), ('total', w.GatewayUnavailable), ('restart_timeout', w.RetainedUpdate)]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root = Path(tmp)/'home';core = Path(tmp)/'core';work = Path(tmp)/'job'
                root.mkdir();core.mkdir();work.mkdir()
                manifest = json.loads((Path(w.__file__).resolve().parents[1]/'patches/manifest.json').read_text())
                for entry in manifest['files']:
                    path = core/entry['path'];path.parent.mkdir(parents=True, exist_ok=True);path.write_text('old')
                real_path = Path
                def path(value):
                    return {'/opt/manuella': root, '/opt/manuella/runtime': core}.get(str(value), real_path(value))
                stack.enter_context(patch.object(w, 'Path', side_effect=path))
                for name, value in [('states', {}), ('process_table', {}), ('homes', []), ('legacy', []), ('finish_backup', {})]:
                    stack.enter_context(patch.object(w, name, return_value=value))
                for name in ('snapshot', 'native_install', 'apply_patch', 'clean', 'log'):
                    stack.enter_context(patch.object(w, name))
                restore = stack.enter_context(patch.object(w, 'restore'))
                turn = stack.enter_context(patch.object(w, 'accept_turn', side_effect=w.NoRealTurn('no turn') if failure=='total' else None))
                stack.enter_context(patch.object(w, 'readback', side_effect=RuntimeError('readback') if failure=='partial' else None, return_value={}))
                restarts = []
                def run(argv, **kw):
                    if argv[0]=='sudo':
                        restarts.append(argv)
                        if failure=='restart_timeout':raise subprocess.TimeoutExpired(argv, 60)
                        return ''
                    return json.dumps({'sha': w.SHA})
                stack.enter_context(patch.object(w, 'run', side_effect=run))
                with self.assertRaises(expected):w.service_transaction(Namespace(target='manu', repo_ref='a'*40, job_started_at=SINCE), work)
                self.assertEqual(len(restarts), 2 if failure=='total' else 1)
                self.assertEqual(restore.call_count, 1 if failure=='total' else 0)
                turn.assert_called_once()

    def test_unknown_probe_error_is_not_proof_of_total_failure(self):
        a, work, run, turn, restore = self.transaction(turn_error=OSError('probe script missing'))
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        restore.assert_not_called();turn.assert_called_once()

    def test_stop_timeout_leaves_running_original_without_rollback(self):
        a, work, run, turn, restore = self.transaction(failure=['docker', 'stop'])
        original = w.api.return_value
        original['State']['Running'] = True
        with self.assertRaises(w.RetainedUpdate):w.docker_transaction(a, 'live', work)
        restore.assert_not_called();turn.assert_not_called()
        self.assertFalse(any(c.args[0][:2] == ['docker', 'start'] for c in run.call_args_list))

    def test_probe_health_waits_without_retrying_inference(self):
        health = {'version': '0.23.1', 'pid': 44}
        responses = [OSError('starting'), io.BytesIO(json.dumps(health).encode()), io.BytesIO(json.dumps({'choices': [{'message': {'content': 'FROTA_OK'}}]}).encode())]
        with patch.object(probe.os, 'kill'), patch.object(probe.time, 'sleep'), patch.object(probe.urllib.request, 'urlopen', side_effect=responses) as http:
            probe.live_turn(8642, 'test', wait=10)
            self.assertEqual([c.args[0].get_method() for c in http.call_args_list], ['GET', 'GET', 'POST'])

    def test_bookkeeping_warning_after_live_turn_is_retained(self):
        evidence = {'pid': 44, 'version': '0.23.1', 'output': 'FROTA_OK', 'warnings': ['bookkeeping failed']}
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'states', return_value={}), patch.object(w, 'homes', return_value=[Path('/home')]), patch.object(w, 'run', return_value=json.dumps(evidence)) as run:
            with self.assertRaises(w.RetainedUpdate):w.accept_turn(Path('/home'), None, Path(tmp))
            self.assertTrue((Path(tmp)/'turn.json').exists());run.assert_called_once()

    def test_refused_manual_rollback_does_not_expose_prior_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp);old = {'ok': True, 'exit_code': 0}
            (work/'result.json').write_text(json.dumps(old));eid = 'b'*32
            with patch.object(w.Path, 'cwd', return_value=work), patch.object(w, 'acquire_lock', side_effect=w.RolloutRefused('cooldown')), patch.object(w, 'log'), patch.object(sys, 'argv', ['worker', '--target', 'luna', '--repo-ref', 'a'*40, '--rollback', '--execution-id', eid]):
                self.assertEqual(w.main(), 3)
                self.assertEqual(json.loads((work/('result-'+eid+'.json')).read_text())['exit_code'], 3)
                self.assertEqual(json.loads((work/'result.json').read_text()), old)

    def test_foreground_wait_cannot_report_success_while_pending(self):
        with patch.object(rollout, 'status', side_effect=[{'ok': None}, {'ok': False, 'exit_code': 2}]), patch.object(rollout.time, 'sleep'):
            self.assertEqual(rollout.wait_result('aquario', '/job')['exit_code'], 2)
        with patch.object(rollout, 'status', return_value={'ok': None}), patch.object(rollout.time, 'monotonic', side_effect=[0, 4000]):
            self.assertEqual(rollout.wait_result('aquario', '/job')['exit_code'], 2)

    def test_status_queries_exact_execution_result(self):
        with patch.object(rollout, 'remote', return_value=b'{"ok":false,"exit_code":3}') as remote:
            self.assertEqual(rollout.status('aquario', '/job', 'b'*32)['exit_code'], 3)
            self.assertEqual(remote.call_args.args[1][-1], 'result-'+'b'*32+'.json')

    def test_port_hint_recycled_pid_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'gateway_state.json'
            path.write_text(json.dumps(state({'api_server': {'metrics': {'port': 9001}}})))
            with patch.object(probe.os, 'kill'), patch.object(probe, 'process_start', return_value=99):
                self.assertEqual(probe.status_port(path, 8642, SINCE), 8642)

    def test_actual_probe_wrapper_failure_is_not_total_gateway_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'states', return_value={}), patch.object(w, 'homes', return_value=[Path('/a')]):
            for output in ('not-json', json.dumps({'failure': 'probe_infrastructure', 'error': 'ImportError'})):
                with patch.object(w, 'run', return_value=output):
                    with self.assertRaises(w.RetainedUpdate):w.accept_turn(Path('/'), None, Path(tmp))
            with patch.object(w, 'run', return_value=json.dumps({'failure': 'api_unavailable', 'error': 'TimeoutError'})):
                with self.assertRaises(w.NoRealTurn):w.accept_turn(Path('/'), None, Path(tmp))

    def test_transient_container_name_gap_is_only_a_poll_retry(self):
        with patch.object(rollout, 'status', side_effect=[RuntimeError('No such container'), {'ok': True, 'exit_code': 0}]) as status, patch.object(rollout.time, 'sleep'):
            self.assertTrue(rollout.wait_result('mafe', '/job')['ok'])
            self.assertEqual(status.call_count, 2)

    def test_cache_trees_are_pruned_before_legacy_scan_and_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for cache in w.CACHE_DIRS:
                folder = root/cache;folder.mkdir()
                (folder/'anthropic_pool_unlatch.py').write_text('not source')
                (folder/'exe').write_text('x');(folder/'exe').chmod(0o755)
            (root/'source').write_text('x');(root/'source').chmod(0o755)
            self.assertEqual(w.legacy([root]), [])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(w.EXECUTABLE_INVENTORY.replace('/opt/hermes', str(root)), {})
            self.assertEqual(json.loads(output.getvalue()), {'source': 0o111})
