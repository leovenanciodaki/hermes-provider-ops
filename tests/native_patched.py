"""Verify generated patch using real native pools/auth writes; no model/network."""
import os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from make_patch import changes
class PatchedReset(unittest.TestCase):
    def test_live_agent_account_and_model_reset(self):
        import agent.agent_runtime_helpers as helpers
        from agent.credential_pool import CredentialPool,PooledCredential
        from hermes_cli.auth import write_credential_pool,read_credential_pool
        from dataclasses import asdict
        # Execute patched helper definitions only in a separate namespace, reference stays read-only.
        source=next(b for path,a,b in changes() if path.endswith('agent_runtime_helpers.py'))
        ns=dict(helpers.__dict__);exec(compile(source,'patched_runtime_helpers','exec'),ns)
        for model_only in (False,True):
            with tempfile.TemporaryDirectory() as home,patch.dict(os.environ,{'HERMES_HOME':home,'HERMES_ROOT':home}):
                stamp=time.time()-1
                entry=PooledCredential(provider='anthropic',id='fixture',label='fixture',auth_type='api_key',priority=0,source='manual',access_token='fixture-no-network',last_status=None if model_only else 'exhausted',last_status_at=stamp,last_error_reset_at=None if model_only else time.time()+3600,model_cooldowns={'claude-sonnet-4-5':time.time()+3600} if model_only else None)
                write_credential_pool('anthropic',[asdict(entry)])
                live=CredentialPool('anthropic',[entry]);agent=SimpleNamespace(_fallback_activated=True,_primary_runtime={'provider':'anthropic','model':'claude-sonnet-4-5','base_url':'https://api.anthropic.com'},_rate_limited_until=time.monotonic()+3600,_provider_ops_cooldown_armed_at=stamp,_credential_pool=live,model='fallback',provider='other')
                CredentialPool('anthropic',[entry]).reset_statuses()
                def fresh(*args):
                    from agent.credential_pool import load_pool
                    return load_pool('anthropic')
                ns['credential_pool_matches_provider']=lambda pool,*a,**k:pool.provider=='anthropic'
                ns['_primary_reset_gate_blocks']=lambda *args:(False,None,False)
                with patch('agent.custom_entry_pool.load_runtime_pool',side_effect=fresh),patch('agent.route_binding.reinstall_primary_runtime') as reinstall:
                    self.assertTrue(ns['restore_primary_runtime'](agent))
                    self.assertEqual(agent._rate_limited_until,0)
                    self.assertTrue(agent._credential_pool.has_available(model='claude-sonnet-4-5'))
                    reinstall.assert_called_once()
if __name__=='__main__':unittest.main()
