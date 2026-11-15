import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_worker as w


class AquarioImageTests(unittest.TestCase):
    def test_inventory_and_repair_only_execution_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / 'old', root / 'new'
            for tree in (old, new):
                (tree / 'docker').mkdir(parents=True)
                for keep in ('.venv', '.hermes', 'bin', 'node_modules'):
                    (tree / keep).mkdir()
                    (tree / keep / 'preserved').write_text('runtime')
                    (tree / keep / 'preserved').chmod(0o755)
            for name, mode in [('dispatch', 0o755), ('private', 0o710), ('gone', 0o755), ('plain', 0o644)]:
                (old / 'docker' / name).write_text('old source')
                (old / 'docker' / name).chmod(mode)
                if name != 'gone':
                    (new / 'docker' / name).write_text('new source')
                    (new / 'docker' / name).chmod(0o664)
            (old / 'link').symlink_to(old / 'docker/dispatch')
            (new / 'docker/link').symlink_to(new / 'docker/plain')
            inventory = {}
            def run(argv, **kw):
                if argv[1] == 'run':
                    from contextlib import redirect_stdout
                    from io import StringIO
                    out = StringIO()
                    with redirect_stdout(out):
                        exec(argv[-1].replace('/opt/hermes', str(old)), {})
                    inventory.update(json.loads(out.getvalue()))
                    return out.getvalue()
                exec(argv[-2].replace('/opt/hermes', str(new)).replace('sys.argv[1]', repr(argv[-1])), {})
                return ''
            with patch.object(w, 'run', side_effect=run) as mocked:
                w.repair_executables('current', 'stage')
            self.assertEqual(mocked.call_args_list[0].args[0][1:8], ['run', '--rm', '--network', 'none', '--entrypoint', 'python3', 'current'])
            self.assertEqual(inventory, {'docker/dispatch': 0o111, 'docker/private': 0o110, 'docker/gone': 0o111})
            for name, mode in [('dispatch', 0o775), ('private', 0o774), ('plain', 0o664)]:
                self.assertEqual(stat.S_IMODE((new / 'docker' / name).stat().st_mode), mode)
                self.assertEqual((new / 'docker' / name).read_text(), 'new source')

    def test_smoke_default_entrypoint_cleanup_on_failure_and_timeout(self):
        import subprocess
        for error in (RuntimeError('permission denied'), subprocess.TimeoutExpired('docker', 60)):
            with self.subTest(error=error), patch.object(w, 'run', side_effect=['smoke-id', error, '']) as run:
                with self.assertRaises(type(error)):
                    w.smoke_image('image')
                create = run.call_args_list[0].args[0]
                self.assertNotIn('--entrypoint', create)
                self.assertNotIn('-v', create)
                self.assertIn('none', create)
                self.assertEqual(create[-2:], ['image', '/bin/true'])
                self.assertEqual(run.call_args_list[1].kwargs['timeout'], 60)
                self.assertEqual(run.call_args.args[0], ['docker', 'rm', '-f', '-v', 'smoke-id'])

    def test_smoke_requires_zero_exit(self):
        with patch.object(w, 'run', side_effect=['id', '', '1', '']):
            with self.assertRaisesRegex(RuntimeError, 'smoke'):
                w.smoke_image('image')

    def test_rebuilt_image_restores_runtime_config_before_dispatcher_smoke(self):
        import tarfile
        config = {'Entrypoint': ['/opt/hermes/docker/entrypoint-dispatch.sh'], 'Cmd': ['sleep', 'infinity']}
        committed = {}
        def fetch(url, archive):
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(Path(archive).parent / 'upstream', arcname='hermes')
        def run(argv, **kw):
            if argv[:2] == ['docker', 'commit']:
                self.assertEqual(argv, ['docker', 'commit', '--change', 'ENTRYPOINT '+json.dumps(config['Entrypoint']),
                    '--change', 'CMD '+json.dumps(config['Cmd']), 'stage'])
                for change in (argv[3], argv[5]):
                    key, value = change.split(' ', 1)
                    committed[key] = json.loads(value)
                return 'rebuilt'
            if argv[:2] == ['docker', 'create']:
                if '--entrypoint' in argv:
                    return 'stage'
                self.assertEqual(committed, {'ENTRYPOINT': config['Entrypoint'], 'CMD': config['Cmd']})
                self.assertEqual(argv[-2:], ['rebuilt', '/bin/true'])
                return 'smoke'
            if argv[:2] == ['docker', 'inspect']:
                return '0'
            return ''
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / 'upstream').mkdir()
            with patch('urllib.request.urlretrieve', side_effect=fetch), patch.object(w, 'apply_patch'), patch.object(w, 'repair_executables'), patch.object(w, 'run', side_effect=run) as mocked:
                self.assertEqual(w.image_for({'Image': 'current', 'Config': config}, 'tanque', None, work), 'rebuilt')
            commands = [call.args[0] for call in mocked.call_args_list]
            self.assertIn(['docker', 'start', '-a', 'smoke'], commands)
            self.assertEqual(commands[-1], ['docker', 'rm', '-f', 'stage'])

    def test_permission_failure_before_stop(self):
        d = {'Mounts': [{'Source': '/data', 'Destination': '/home/hermes/.hermes'}], 'Image': 'current', 'Config': {'Entrypoint': ['dispatcher'], 'Cmd': ['sleep', 'infinity']}}
        a = Mock(target='aquario', image='candidate', repo_ref='ref', accept_turns=False)
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ['docker', 'exec']:
                return '{"1":2}'
            if argv[:2] == ['docker', 'create']:
                return 'stage' if '--entrypoint' in argv else 'smoke-id'
            if argv[:2] == ['docker', 'commit']:
                return 'repaired'
            if argv[:3] == ['docker', 'start', '-a']:
                raise RuntimeError('permission denied')
            return ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'api', return_value=d), patch.object(w, 'states', return_value={'/data': {'pid': 1, 'start_time': 2}}), patch.object(w, 'homes', return_value=[]), patch.object(w, 'legacy', return_value=[]), patch.object(w, 'snapshot'), patch.object(w, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                w.docker_transaction(a, 'live', Path(tmp))
        self.assertFalse(any(cmd[:2] == ['docker', 'stop'] for cmd in calls))
        self.assertIn(['docker', 'rm', '-f', '-v', 'smoke-id'], calls)
        self.assertEqual(calls[-1], ['docker', 'rm', '-f', 'stage'])

    def test_aquario_repairs_verified_staged_source_before_smoke(self):
        config = {'Entrypoint': ['dispatcher'], 'Cmd': ['sleep', 'infinity']}
        for explicit in (None, 'explicit'):
            candidate = explicit or w.STAGED['aquario']
            events = Mock()
            events.run.side_effect = ['stage', '', 'repaired', '']
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlretrieve') as fetch, patch.object(w, 'verify_core_image', events.core), patch.object(w, 'repair_executables', events.repair), patch.object(w, 'verify_image', events.verify), patch.object(w, 'run', events.run):
                self.assertEqual(w.image_for({'Image': 'current', 'Config': config}, 'aquario', explicit, Path(tmp)), 'repaired')
                fetch.assert_not_called()
            from unittest.mock import call
            self.assertEqual(events.mock_calls, [
                call.core(candidate),
                call.run(['docker', 'create', '--entrypoint', '/bin/sleep', candidate, 'infinity']),
                call.run(['docker', 'start', 'stage']),
                call.repair('current', 'stage'),
                call.run(['docker', 'commit', '--change', 'ENTRYPOINT '+json.dumps(config['Entrypoint']), '--change', 'CMD '+json.dumps(config['Cmd']), 'stage']),
                call.verify('repaired'),
                call.run(['docker', 'rm', '-f', 'stage']),
            ])

    def test_invalid_staged_core_is_rejected_before_modification(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'verify_core_image', side_effect=RuntimeError('digest mismatch')) as verify, patch.object(w, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
                w.image_for({'Image': 'current'}, 'aquario', None, Path(tmp))
            verify.assert_called_once_with(w.STAGED['aquario'])
            run.assert_not_called()

    def test_other_staged_target_retains_direct_verification(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(w, 'verify_image') as verify, patch.object(w, 'repair_executables') as repair:
            self.assertEqual(w.image_for({}, 'mafe', None, Path(tmp)), w.STAGED['mafe'])
            verify.assert_called_once_with(w.STAGED['mafe'])
            repair.assert_not_called()
