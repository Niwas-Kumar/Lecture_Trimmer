"""Protect package imports and the installed runtime's update contract."""
import ast
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

from lecture_trimmer.paths import APPLICATION_ROOT, ASSET_ROOT
from tools.release_manifest import APP_FILES, source_for


class SourceLayoutTests(unittest.TestCase):
    def test_release_allowlist_covers_package_and_assets_without_private_files(self):
        root = APPLICATION_ROOT
        expected = {
            path.relative_to(root).as_posix()
            for path in (root / 'lecture_trimmer').rglob('*.py')
        }
        expected.update(path.relative_to(root).as_posix()
                        for path in ASSET_ROOT.iterdir() if path.is_file())
        expected.update(path.relative_to(root).as_posix()
                        for path in (root / 'lecture_trimmer' / 'web' / 'dist').rglob('*') if path.is_file())
        expected.update({'app.py', 'audio.py', 'access_control.py',
                         'pw_desktop_auth.py', 'diagnostics.py', 'vendor/pw_access.py',
                         'desktop_runtime/__init__.py', 'desktop_runtime/desktop_paths.py',
                         'desktop_runtime/desktop_update.py'})
        self.assertEqual(set(APP_FILES), expected)
        self.assertEqual(len(APP_FILES), len(set(APP_FILES)))
        for name in APP_FILES:
            self.assertTrue((root / source_for(name)).is_file(), name)

    def test_existing_frozen_host_imports_share_canonical_module_state(self):
        pairs = {
            'app': 'lecture_trimmer.ui.app',
            'audio': 'lecture_trimmer.media.audio',
            'access_control': 'lecture_trimmer.auth.access_control',
            'pw_desktop_auth': 'lecture_trimmer.auth.pw_desktop_auth',
            'diagnostics': 'lecture_trimmer.runtime.diagnostics',
        }
        for legacy, canonical in pairs.items():
            with self.subTest(module=legacy):
                spec = importlib.util.spec_from_file_location(
                    legacy, APPLICATION_ROOT / source_for(legacy + '.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertIs(sys.modules[legacy], importlib.import_module(canonical))

    def test_only_public_entry_points_remain_at_root(self):
        self.assertEqual({p.name for p in APPLICATION_ROOT.glob('*.py')}, {'app.py', 'main.py'})

    def test_old_frozen_host_shares_update_state_with_new_package(self):
        code = '''
import sys, types
sys.frozen = True
paths = types.ModuleType('desktop_paths')
update = types.ModuleType('desktop_update')
update.guard = object()
sys.modules['desktop_paths'] = paths
sys.modules['desktop_update'] = update
from desktop_runtime import desktop_paths, desktop_update
assert desktop_paths is paths
assert desktop_update is update
assert desktop_update.guard is update.guard
'''
        subprocess.run([sys.executable, '-c', code], cwd=APPLICATION_ROOT,
                       check=True, capture_output=True, timeout=20)

    def test_application_uses_package_imports_instead_of_legacy_adapters(self):
        legacy = {
            'app', 'audio', 'access_control', 'pw_desktop_auth', 'diagnostics',
            'config', 'models', 'classifier', 'lesson_classifier', 'prompts',
            'visual_evidence', 'pipeline', 'editor', 'lesson_planner', 'job_control',
            'cutter', 'transcriber', 'pw_proxy', 'pw_diagnostics', 'ai_usage',
            'branding', 'ui_style',
        }
        for path in (APPLICATION_ROOT / 'lecture_trimmer').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertNotIn(node.module, legacy, str(path))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, legacy, str(path))

    def test_source_root_and_branding_survive_module_relocation(self):
        from lecture_trimmer import config
        from lecture_trimmer.ui.branding import ICON_PATH, logo
        self.assertEqual(APPLICATION_ROOT, Path(__file__).resolve().parents[1])
        self.assertEqual(config.PROJECT_ROOT, APPLICATION_ROOT)
        from desktop_runtime.desktop_paths import DATA_ROOT, RESOURCE_ROOT
        import os
        if not os.getenv('TRIMMER_DATA_DIR'):
            self.assertEqual(DATA_ROOT, APPLICATION_ROOT)
        self.assertEqual(RESOURCE_ROOT, APPLICATION_ROOT)
        self.assertTrue(ICON_PATH.is_file())
        self.assertIn('<svg', logo())

    def test_pw_kit_stays_isolated_per_login_at_its_supported_location(self):
        from lecture_trimmer.integrations.pw_proxy import load_kit
        first, second = load_kit(), load_kit()
        self.assertIsNot(first, second)
        self.assertEqual(Path(first.__file__), APPLICATION_ROOT / 'vendor' / 'pw_access.py')
        self.assertEqual(first.APP_NAME, 'Lecture_Trimmer')


if __name__ == '__main__':
    unittest.main()
