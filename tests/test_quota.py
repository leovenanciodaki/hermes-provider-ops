import json
import tempfile
import unittest
from types import SimpleNamespace as NS
import provider_ops as p

class Quota(unittest.TestCase):
    def pool(self,provider):return NS(entries=lambda:[NS(id='a',label='Account',runtime_api_key='fake',base_url='exact')])
    def test_percent_not_fraction(self):
        rows=list(p.windows('anthropic',{'five_hour':{'utilization':0.95,'resets_at':'2030-01-01T00:00:00Z'}}))
        self.assertEqual(rows[0][1],0.95)
    def test_dedup_and_profile_isolation(self):
        with tempfile.TemporaryDirectory() as home:
            sent=[]
            def send(token,text):sent.append(text);return len(sent)
            fetch=lambda provider,entry: {'five_hour':{'utilization':96,'resets_at':'2030-01-01T00:00:00Z'}} if provider=='anthropic' else {}
            for name,now in [('a',1000),('a',1001),('a',1400),('b',1400)]:
                p.check(home+'/'+name,'fake',pools=self.pool,fetch=fetch,send=send,now=now)
            self.assertEqual(len(sent),4)
            self.assertIn('Account',sent[0]);self.assertIn('reset',sent[0])
    def test_retry_and_test_single_message(self):
        with tempfile.TemporaryDirectory() as home:
            fetch=lambda provider,entry:{'five_hour':{'utilization':96,'resets_at':1900000000}} if provider=='anthropic' else {}
            def fail(*args):raise RuntimeError('secret must not appear')
            self.assertEqual(p.check(home,'fake',pools=self.pool,fetch=fetch,send=fail,now=1000),[])
            ids=p.check(home,'fake',pools=self.pool,fetch=fetch,send=lambda *a:42,now=1400)
            self.assertEqual(ids,[42,42])
            text=[]
            ids=p.check(home,'fake',pools=self.pool,fetch=fetch,send=lambda token,t:(text.append(t) or 99),test=True,thresholds=(1,),interval=0)
            self.assertEqual(ids,[99]);self.assertTrue(text[0].startswith('TESTE '))
    def test_raw_codex(self):
        self.assertEqual(list(p.windows('openai-codex',{'rate_limit':{'primary_window':{'used_percent':.5,'reset_at':1900000000}}}))[0][1],.5)
    def test_no_implicit_test(self):
        with self.assertRaises(ValueError):p.check('/unused','fake',thresholds=(1,))

if __name__=='__main__':unittest.main()
