import unittest
from abi_autopilot.github import dependency_refs_from_body

class NamedDependencyTests(unittest.TestCase):
    def test_named_and_numeric(self):
        body='''## Dependencias\n- UX-14\n- COACH-03\n- #91\n\n## Verificación\nfoo'''
        self.assertEqual(dependency_refs_from_body(body, ['UX', 'COACH']), ['#91','COACH-03','UX-14'])

    def test_named_refs_ignored_without_configured_prefixes(self):
        body='''## Dependencias
- UX-14
- UTF-8
- #91
'''
        self.assertEqual(dependency_refs_from_body(body), ['#91'])
