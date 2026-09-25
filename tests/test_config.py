import json
import tempfile
import unittest
from pathlib import Path

from abi_autopilot import __version__
from abi_autopilot.config import write_initial_config, load_config, upgrade_config


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / 'repo'; repo.mkdir()
            cfg_path = root / 'config.local.json'
            write_initial_config(cfg_path, str(repo), 'x/y', 'integration/test')
            cfg = load_config(cfg_path)
            self.assertEqual(cfg['repo_slug'], 'x/y')
            self.assertFalse(cfg['integration']['auto_integrate'])
            self.assertTrue(cfg['dependency_cache']['enabled'])
            self.assertEqual(cfg['workspace_bootstrap'][0]['strategy'], 'npm_shared_cache')
            self.assertEqual(cfg['process_env']['PLAYWRIGHT_HTML_OPEN'], 'never')
            self.assertTrue(cfg['integration']['manual_close_after_success'])
            self.assertTrue(cfg['validation']['targeted_backend_no_cov'])
            self.assertTrue(cfg['batch_validation']['full_suite_once_at_end'])
            self.assertEqual(cfg['validation_workers']['vitest'], 4)
            self.assertIn('batch_full', cfg['validation'])
            self.assertEqual(cfg['autopilot']['implementer'], 'codex')
            self.assertIsNone(cfg['autopilot']['implementer_fallback'])
            self.assertEqual(cfg['claude']['implement_permission_mode'], 'acceptEdits')

    def test_upgrade_v14_bootstrap_to_shared_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / 'repo'; repo.mkdir()
            cfg_path = root / 'config.local.json'
            old = {
                'repo_path': str(repo),
                'repo_slug': 'x/y',
                'base_branch': 'integration/test',
                'worktree_root': str(root / 'worktrees'),
                'workspace_bootstrap': [{
                    'name': 'frontend-npm-ci',
                    'cwd': 'frontend',
                    'command': 'npm ci --no-audit --no-fund',
                    'if_missing': 'node_modules/jsdom/package.json',
                    'rerun_if_changed': ['frontend/package.json','frontend/package-lock.json'],
                    'timeout': 2400,
                }],
            }
            cfg_path.write_text(json.dumps(old), encoding='utf-8')
            upgraded = upgrade_config(cfg_path)
            spec = upgraded['workspace_bootstrap'][0]
            self.assertEqual(spec['strategy'], 'npm_shared_cache')
            self.assertEqual(spec['link_path'], 'node_modules')
            self.assertTrue(upgraded['dependency_cache']['enabled'])
            self.assertEqual(upgraded['process_env']['PLAYWRIGHT_HTML_OPEN'], 'never')
            self.assertTrue(Path(str(cfg_path) + f'.pre-v{__version__}.bak').exists())


if __name__=='__main__': unittest.main()
