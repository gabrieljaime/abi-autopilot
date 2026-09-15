import unittest
from abi_autopilot.github import dependencies_from_body, dependency_refs_from_body

class DependencyTests(unittest.TestCase):
    def test_spanish_section(self):
        body="""## Dependencias\n- #90\n- #88\n\n## Tests\n- #999 no debe contar\n"""
        self.assertEqual(dependencies_from_body(body),[88,90])
    def test_depends_on(self):
        self.assertEqual(dependencies_from_body("Depends-On: #4, #7"),[4,7])
    def test_recomendada_is_soft_not_blocking(self):
        body = "## Dependencias\nUX-38, UX-45 recomendada.\n"
        self.assertEqual(dependency_refs_from_body(body), ["UX-38"])
    def test_opcional_is_soft_not_blocking(self):
        body = "## Dependencias\n#12, #34 opcional.\n"
        self.assertEqual(dependency_refs_from_body(body), ["#12"])
    def test_hard_refs_on_same_line_still_count(self):
        body = "## Dependencias\nUX-07, UX-11, UX-39, UX-41.\n"
        self.assertEqual(dependency_refs_from_body(body), ["UX-07", "UX-11", "UX-39", "UX-41"])

if __name__=='__main__': unittest.main()
