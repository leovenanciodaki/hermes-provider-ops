import importlib.machinery,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import host_worker as w
import rollout
from remote_docker import recipe
class Rollout(unittest.TestCase):
    def test_contract_preserved(self):
        d={'Config':{'User':'root','Env':['SECRET=x'],'Labels':{'x':'y'},'Entrypoint':['a','b']},'HostConfig':{'Binds':['/old:/opt/hermes/tools/delegate_tool.py:ro'],'Memory':234,'Mounts':[{'Type':'tmpfs'}],'CapAdd':['SYS_PTRACE']},'NetworkSettings':{'Networks':{'net':{'Aliases':['alias'],'EndpointID':'stale','IPAMConfig':{'IPv4Address':'10.0.0.2'}}}}}
        p=recipe(d,'new');self.assertEqual(p['HostConfig'],d['HostConfig']);self.assertEqual(p['User'],'root');self.assertNotIn('EndpointID',p['NetworkingConfig']['EndpointsConfig']['net']);p['HostConfig']['Memory']=0;self.assertEqual(d['HostConfig']['Memory'],234)
    def test_advisory_readiness_does_not_require_new_pid_or_platforms(self):
        b={'h':{'pid':1,'start_time':1,'platforms':{'telegram':{'state':'fatal'},'api_server':{'state':'connected'}}}}
        s={'pid':2,'start_time':2,'gateway_state':'running','code_version':'0.23.1','updated_at':'2026-11-14T01:00:00+00:00','platforms':{'api_server':{'state':'connected'}}}
        self.assertTrue(w.ready(b,{'h':s},0));s['pid']=1;s['start_time']=1;self.assertTrue(w.ready(b,{'h':s},0));s['pid']=2;s['platforms']={};self.assertTrue(w.ready(b,{'h':s},0));s['code_version']='0.23.0';self.assertFalse(w.ready(b,{'h':s},0))
    def test_scan_scope_and_exact_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp);(r/'sessions').mkdir();(r/'sessions/anthropic_pool_unlatch.py').write_text('keep');old=r/'anthropic_pool_unlatch.py';old.write_text('original');absent=r/'new-plugin';backup=r/'backup.tar.gz'
            self.assertEqual(w.legacy([r]),[old]);w.snapshot([old,absent],backup);old.write_text('changed');absent.mkdir();w.restore(backup);self.assertEqual(old.read_text(),'original');self.assertFalse(absent.exists())
    def test_native_local_install_and_disable(self):
        with patch.object(w,'run') as run:
            w.native_install('/cli',Path('/local'), 'a'*40,Path('/home'),legacy_present=True)
        self.assertIn('file:///local',run.call_args_list[1].args[0]);self.assertEqual(run.call_args_list[0].args[0][-3:],['plugins','disable','aquario-pool-fastrotate'])
    def test_hops_and_mafe_root_worker(self):
        self.assertIn('sudo -n ssh',rollout.ssh_argv('luna',['true'])[-1])
        with patch.object(rollout,'remote',return_value=b'42') as remote:
            rollout.launch('mafe','/tmp/job','a'*40,None,False)
        cmd=remote.call_args.args[1][-20:];self.assertIn('os.chroot',str(cmd));self.assertIn('--pid',str(remote.call_args))
    def test_rollback_after_create_failure(self):
        d={'Mounts':[{'Source':'/data','Destination':'/home/hermes/.hermes'}],'Image':'old-image','Config':{'User':'root'},'HostConfig':{},'NetworkSettings':{'Networks':{}}}
        a=Mock(target='mafe',image=None,repo_ref='a'*40,accept_turns=False)
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'api',side_effect=[d,RuntimeError('create')]),patch.object(w,'states',return_value={'/data':{'pid':1,'start_time':2}}),patch.object(w,'homes',return_value=[]),patch.object(w,'legacy',return_value=[]),patch.object(w,'image_for',return_value='image'),patch.object(w,'snapshot'),patch.object(w,'restore') as restore,patch.object(w,'run',side_effect=lambda argv,**kw: '{"1":2}' if 'python3' in argv else '') as run:
            with self.assertRaises(RuntimeError):w.docker_transaction(a,'name',Path(tmp))
            restore.assert_called_once();self.assertEqual(run.call_args.args[0],['docker','start','name'])
    def test_operator_safe_failure(self):
        loader=importlib.machinery.SourceFileLoader('operator_test',str(ROOT/'bin/provider-operator'));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
        with patch.object(m.subprocess,'run',side_effect=OSError('secret sk-bad')):
            self.assertEqual(m.invoke({'name':'manu','argv':['/fixed']},'anthropic'),'manu: falhou:unavailable')
    def test_deploy_hash_guard(self):
        from deploy_bot import deploy
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'src').write_text('new');(p/'dst').write_text('old')
            with self.assertRaises(ValueError):deploy(p/'src',p/'dst','bad')
            self.assertEqual((p/'dst').read_text(),'old')

class BoundedRollout(unittest.TestCase):
    def test_redaction_preserves_error(self):
        from rollout_support import redact
        text=redact('failed port=8642 Bearer abc sk-secret token=abc password=def key=xyz')
        self.assertIn('failed port=8642',text)
        for secret in ('abc','sk-secret','def','xyz'):self.assertNotIn(secret,text)
    def test_failed_command_and_timeout_logged(self):
        import subprocess,os
        from rollout_support import run
        with tempfile.TemporaryDirectory() as tmp:
            old=os.getcwd();os.chdir(tmp)
            try:
                with patch('rollout_support.subprocess.run',return_value=Mock(returncode=2,stdout='useful output',stderr='failure token=secret')):
                    with self.assertRaises(RuntimeError):run(['tool','arg'])
                with patch('rollout_support.subprocess.run',side_effect=subprocess.TimeoutExpired(['tool'],3,output='waiting')):
                    with self.assertRaises(subprocess.TimeoutExpired):run(['tool'])
                log=(Path(tmp)/'job.log').read_text()
                for text in ('step=','command=tool','returncode=2','useful output','timeout'):self.assertIn(text,log)
                self.assertNotIn('token=secret',log)
            finally:os.chdir(old)
    def test_selected_cap_before_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);f=p/'large';f.touch()
            with f.open('r+b') as stream:stream.truncate(w.LIMIT+1)
            with patch.object(w,'disk_check'):
                with self.assertRaisesRegex(RuntimeError,'100MiB'):w.snapshot([f],p/'backup.tar.gz')
            self.assertFalse((p/'backup.tar.gz').exists())
    def test_disk_reserve(self):
        with patch.object(w.shutil,'disk_usage',return_value=Mock(free=100)):
            with self.assertRaisesRegex(RuntimeError,'disk'):w.disk_check(Path('.'),100)
    def test_readiness_retries(self):
        with patch.object(w,'states',side_effect=[{}, {'h':{}}]),patch.object(w,'ready',side_effect=[False,True]),patch.object(w.time,'sleep'):
            self.assertEqual(w.wait_ready({'h':{}},Path('.'),0,w.time.monotonic()+10),{'h':{}})
    def test_deleted_backup_rollback_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'rollback.json').write_text('{"backup_deleted":true}')
            with self.assertRaisesRegex(RuntimeError,'deleted'):w.rollback(p)
    def test_api_145_and_error_body(self):
        import remote_docker as d
        response=Mock(status=400);response.read.return_value=b'{"message":"bad mount token=secret"}'
        connection=Mock();connection.getresponse.return_value=response
        with patch.object(d,'Connection',return_value=connection):
            with self.assertRaisesRegex(RuntimeError,'bad mount'):d.api('POST','/containers/create')
        self.assertEqual(connection.request.call_args.args[1],'/v1.45/containers/create')
    def test_stale_platform_writer(self):
        self.assertEqual(w.connected({'pid':1,'start_time':2,'platforms':{'telegram':{'state':'connected','writer_pid':9,'writer_start_time':2}}}),set())

    def test_historical_legacy_data_is_not_copied_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); legacy=root/'anthropic-rescue-20261110'; saved=legacy/'backup'
            saved.mkdir(parents=True); (saved/'state.db').write_text('historical data')
            code=legacy/'apply_overlay.py';code.write_text('old code')
            selected=w.legacy([root]); self.assertEqual(selected,[code])
            archive=root/'minimal.tar.gz';w.snapshot(selected,archive)
            self.assertLess(archive.stat().st_size,1024*1024)
            w.clean(selected,[]);self.assertEqual((saved/'state.db').read_text(),'historical data')
            self.assertEqual(w.legacy([root]),[])
            w.restore(archive);self.assertEqual(code.read_text(),'old code')
    def test_port_discovery(self):
        from turn_probe import discover_port
        self.assertEqual(discover_port({'platforms':{'api_server':{'metrics':{'port':9001}}}},1),9001)
        self.assertEqual(discover_port({'platforms':{'api_server':{'listener_base':'http://127.0.0.1:9002'}}},1),9002)
