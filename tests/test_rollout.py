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
    def test_baseline_only_and_fresh_process(self):
        b={'h':{'pid':1,'start_time':1,'platforms':{'telegram':{'state':'fatal'},'api_server':{'state':'connected'}}}}
        s={'pid':2,'start_time':2,'gateway_state':'running','code_version':'0.23.1','updated_at':'2026-11-14T01:00:00+00:00','platforms':{'api_server':{'state':'connected'}}}
        self.assertTrue(w.ready(b,{'h':s},0));s['pid']=1;s['start_time']=1;self.assertFalse(w.ready(b,{'h':s},0));s['pid']=2;s['platforms']={};self.assertFalse(w.ready(b,{'h':s},0))
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
        d={'Mounts':[{'Source':'/data','Destination':'/home/hermes/.hermes'}],'Config':{'User':'root'},'HostConfig':{},'NetworkSettings':{'Networks':{}}}
        a=Mock(target='mafe',image=None,repo_ref='a'*40,accept_turns=False)
        with tempfile.TemporaryDirectory() as tmp, patch.object(w,'api',side_effect=[d,RuntimeError('create')]),patch.object(w,'states',return_value={'/data':{}}),patch.object(w,'homes',return_value=[]),patch.object(w,'legacy',return_value=[]),patch.object(w,'image_for',return_value='image'),patch.object(w,'snapshot'),patch.object(w,'restore') as restore,patch.object(w,'run',return_value='') as run:
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
