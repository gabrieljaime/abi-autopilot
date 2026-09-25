import tempfile
import unittest
from pathlib import Path
from abi_autopilot.validator import CheckResult, failure_signatures, mark_inherited_failures

class BaselineTests(unittest.TestCase):
    def _r(self, root, name, text, rc=1):
        p=Path(root)/f'{name}.log'; p.write_text(text,encoding='utf-8')
        return CheckResult(name,'npm test',root,rc,str(p))

    def test_vitest_signature(self):
        with tempfile.TemporaryDirectory() as td:
            r=self._r(td,'a','FAIL  src/x.test.tsx > suite > case\n')
            self.assertEqual(failure_signatures(r), {'vitest:src/x.test.tsx > suite > case'})

    def test_same_failure_is_inherited(self):
        with tempfile.TemporaryDirectory() as td:
            cand=self._r(td,'cand','FAIL  src/x.test.tsx > suite > case\n')
            base=self._r(td,'base','FAIL  src/x.test.tsx > suite > case\n')
            base.name='cand'
            out, notes=mark_inherited_failures([cand],[base])
            self.assertTrue(out[0].passed)
            self.assertTrue(out[0].ignored)
            self.assertTrue(notes)

    def test_new_failure_not_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            cand=self._r(td,'cand','FAIL  src/x.test.tsx > suite > NEW\n')
            base=self._r(td,'base','FAIL  src/x.test.tsx > suite > OLD\n')
            base.name='cand'
            out, notes=mark_inherited_failures([cand],[base])
            self.assertFalse(out[0].passed)
            self.assertFalse(notes)

    def test_baseline_pass_does_not_hide_candidate_failure(self):
        with tempfile.TemporaryDirectory() as td:
            cand=self._r(td,'cand','FAIL  src/x.test.tsx > suite > case\n')
            p=Path(td)/'base.log'; p.write_text('all good')
            base=CheckResult('cand','npm test',td,0,str(p))
            out, notes=mark_inherited_failures([cand],[base])
            self.assertFalse(out[0].passed)
            self.assertFalse(notes)

if __name__=='__main__': unittest.main()
