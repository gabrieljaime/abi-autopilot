import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot.agents import run_claude_review

class Issue:
    number=93
    title='[UX-47] CSS'
    url='https://example.invalid/93'
    risk='medium'
    body='acceptance'

class R:
    returncode=0
    stderr=''
    stdout=''

class WindowsPromptStdinTests(unittest.TestCase):
    def test_large_prompt_is_sent_via_stdin_not_argv(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'prompts').mkdir(); (root/'prompts'/'reviewer.md').write_text('X'*50000)
            run=root/'run'; run.mkdir(); wt=root/'wt'; wt.mkdir()
            cfg={'claude':{'command':'claude','model':'sonnet','max_turns':8,'max_diff_chars':60000,'permission_mode':'plan'}}
            captured={}
            def fake(args, **kwargs):
                if args and args[0]=='git': return R()
                captured['args']=args; captured['stdin']=kwargs.get('input_text','')
                payload={'is_error':False,'structured_output':{'verdict':'PASS','summary':'ok','blocking':[],'non_blocking':[]}}
                return type('P',(),{'returncode':0,'stdout':json.dumps(payload),'stderr':''})()
            with patch('abi_autopilot.agents.run_capture', side_effect=fake):
                review=run_claude_review(root,cfg,wt,Issue(),'base','fast pass',run)
            self.assertEqual(review['verdict'],'PASS')
            self.assertGreater(len(captured['stdin']),50000)
            self.assertLess(sum(len(str(x)) for x in captured['args']),5000)
            self.assertNotIn('X'*1000, ' '.join(captured['args']))
            self.assertIn('--json-schema', captured['args'])

    def test_structured_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'prompts').mkdir(); (root/'prompts'/'reviewer.md').write_text('review')
            run=root/'run'; run.mkdir(); wt=root/'wt'; wt.mkdir()
            cfg={'claude':{'command':'claude','max_turns':2,'max_diff_chars':1000}}
            def fake(args, **kwargs):
                if args and args[0]=='git': return R()
                payload={'is_error':False,'structured_output':{'verdict':'FAIL','summary':'x','blocking':[{'criterion':'c','evidence':'e','required_fix':'f'}],'non_blocking':[]}}
                return type('P',(),{'returncode':0,'stdout':json.dumps(payload),'stderr':''})()
            with patch('abi_autopilot.agents.run_capture', side_effect=fake):
                review=run_claude_review(root,cfg,wt,Issue(),'base','fast pass',run)
            self.assertEqual(review['verdict'],'FAIL')
