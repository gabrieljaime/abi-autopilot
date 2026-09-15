import unittest
from abi_autopilot.gitops import slugify

class GitOpsTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify('[UX-41] Targets táctiles y tipografía mínima'), 'targets-t-ctiles-y-tipograf-a-m-nima')

if __name__=='__main__': unittest.main()
