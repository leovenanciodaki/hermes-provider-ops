"""Run only with the native test environment and clean-reference PYTHONPATH."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

class NativeReset(unittest.TestCase):
    def test_clean_gate_blocks_after_real_reset(self):
        from agent.credential_pool import CredentialPool, PooledCredential
        from agent.agent_runtime_helpers import restore_primary_runtime
        from hermes_cli.auth import write_credential_pool, read_credential_pool
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME':home,'HERMES_ROOT':home}):
            for model_only in (False,True):
                entry=PooledCredential(provider='anthropic',id='fixture',label='fixture',auth_type='api_key',priority=0,source='manual',access_token='fixture-no-network',last_status=None if model_only else 'exhausted',last_status_at=time.time(),last_error_reset_at=None if model_only else time.time()+3600,model_cooldowns={'claude-sonnet-4-5':time.time()+3600} if model_only else None)
                write_credential_pool('anthropic',[asdict(entry)])
                live=CredentialPool('anthropic',[entry])
                agent=SimpleNamespace(_fallback_activated=True,_primary_runtime={'provider':'anthropic','model':'claude-sonnet-4-5','base_url':'https://api.anthropic.com'},_rate_limited_until=time.monotonic()+3600,_credential_pool=live)
                other=CredentialPool('anthropic',[entry]);self.assertEqual(other.reset_statuses(),1)
                self.assertGreater(read_credential_pool('anthropic')[0]['status_cleared_at'],entry.last_status_at)
                self.assertFalse(restore_primary_runtime(agent))
                self.assertGreater(agent._rate_limited_until,time.monotonic())
                self.assertIs(agent._credential_pool,live)

if __name__=='__main__':unittest.main()
