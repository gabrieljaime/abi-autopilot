import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot.agents import run_claude_review


class P:
    def __init__(self, rc, payload, stderr=""):
        self.returncode = rc
        self.stdout = json.dumps(payload)
        self.stderr = stderr


class Issue:
    number=93
    title="[UX-47] CSS"
    url="https://example.invalid/93"
    risk="medium"
    body="acceptance"


class ReviewerResumeTests(unittest.TestCase):
    def test_max_turns_resumes_same_session_and_returns_review(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'prompts').mkdir()
            (root/'prompts'/'reviewer.md').write_text('review',encoding='utf-8')
            run_dir=root/'run'; run_dir.mkdir()
            wt=root/'wt'; wt.mkdir()
            cfg={'claude':{'command':'claude','model':'sonnet','max_turns':8,'resume_turns':4,'max_turn_resumes':2,'permission_mode':'plan','max_diff_chars':1000}}
            error={'is_error':True,'subtype':'error_max_turns','terminal_reason':'max_turns','session_id':'abc','errors':['Reached maximum number of turns (8)']}
            ok={'is_error':False,'result':json.dumps({'verdict':'PASS','summary':'ok','blocking':[],'non_blocking':[]})}
            calls=[]
            def fake(args,**kwargs):
                calls.append(args)
                # first four calls collect git evidence
                if args and args[0]=='git':
                    return P(0,{}) if False else type('R',(),{'returncode':0,'stdout':'','stderr':''})()
                if len([c for c in calls if c and c[0]=='claude'])==1:
                    return P(1,error)
                return P(0,ok)
            with patch('abi_autopilot.agents.run_capture', side_effect=fake):
                review=run_claude_review(root,cfg,wt,Issue(),'base','fast pass',run_dir)
            self.assertEqual(review['verdict'],'PASS')
            claude_calls=[c for c in calls if c and c[0]=='claude']
            self.assertEqual(len(claude_calls),2)
            self.assertIn('--resume',claude_calls[1])
            self.assertIn('abc',claude_calls[1])
            self.assertTrue((run_dir/'claude-review-raw-resume-1.json').exists())

    def test_max_turn_resume_limit_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'prompts').mkdir(); (root/'prompts'/'reviewer.md').write_text('review')
            run_dir=root/'run'; run_dir.mkdir(); wt=root/'wt'; wt.mkdir()
            cfg={'claude':{'command':'claude','max_turns':2,'resume_turns':1,'max_turn_resumes':1,'max_diff_chars':1000}}
            error={'is_error':True,'subtype':'error_max_turns','session_id':'abc','errors':['Reached maximum number of turns']}
            def fake(args,**kwargs):
                if args and args[0]=='git': return type('R',(),{'returncode':0,'stdout':'','stderr':''})()
                return P(1,error)
            from abi_autopilot.shell import CommandError
            with patch('abi_autopilot.agents.run_capture', side_effect=fake):
                with self.assertRaises(CommandError):
                    run_claude_review(root,cfg,wt,Issue(),'base','fast pass',run_dir)

if __name__=='__main__': unittest.main()
