import importlib.util,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
from provider_refresh import provider,LEO
class Bot(unittest.TestCase):
    def update(self):return {'update_id':12,'message':{'from':{'id':LEO},'chat':{'id':LEO,'type':'private'},'text':'provider refresh anthropic'}}
    def test_strict(self):
        u=self.update();self.assertEqual(provider(u),'anthropic')
        u['message']['from']['id']=1;self.assertIsNone(provider(u))
        u=self.update();u['message']['chat']['type']='group';self.assertIsNone(provider(u))
    def test_duplicate_and_unauthorized_no_transport(self):
        import types
        # Dispatcher imports the native Kanban module; no database operations are needed here.
        with patch.dict(sys.modules,{'hermes_cli':types.SimpleNamespace(kanban_db=object())}):
            spec=importlib.util.spec_from_file_location('dispatcher_copy',Path(__file__).resolve().parents[1]/'bot/dispatcher.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        state={'updates':{'12':{}},'messages':{},'sent':{},'decisions':{}}
        app=m.Dispatcher('fake',state)
        with patch.object(m,'audit'),patch('provider_refresh.forward') as forward:
            self.assertTrue(app.update(self.update())['duplicate'])
            u=self.update();u['message']['from']['id']=1
            self.assertTrue(app.update(u)['ignored']);forward.assert_not_called()
    def test_reply_sent_once_and_test_flag_strict(self):
        import types
        with patch.dict(sys.modules,{'hermes_cli':types.SimpleNamespace(kanban_db=object())}):
            spec=importlib.util.spec_from_file_location('dispatcher_reply',Path(__file__).resolve().parents[1]/'bot/dispatcher.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        for flag,prefix in [(True,True),('true',False),(False,False)]:
            state={'updates':{},'messages':{},'sent':{},'decisions':{}}
            app=m.Dispatcher('fake',state);u=self.update();u['provider_ops_test']=flag
            with patch.object(m,'audit'),patch.object(app,'save'),patch.object(app,'send') as send,patch('provider_refresh.forward',return_value={'ok':True,'result':'manu: liberado\nmafe: sem pool'}) as forward:
                app.update(u);app.update(u);send.assert_called_once();forward.assert_called_once()
                text=send.call_args.args[0];self.assertEqual(text.startswith('TESTE'),prefix);self.assertIn('luna: falhou:',text)
