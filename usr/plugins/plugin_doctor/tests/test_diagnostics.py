import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usr.plugins.plugin_doctor.helpers.diagnostics import inspect, locate


class DiagnosticsTests(unittest.TestCase):
    def test_builtin_source_is_not_shadowed_by_user_data_only_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [Path(tmp)/'user', Path(tmp)/'builtin']
            for root in roots:
                (root/'example').mkdir(parents=True)
            (roots[0]/'example/config.json').write_text('{"secret":"not source"}')
            (roots[1]/'example/plugin.yaml').write_text('name: example\ntitle: Example\n')
            self.assertEqual(locate(roots, 'example'), roots[1]/'example')
            self.assertTrue(inspect(roots, 'example')['passed'])
            (roots[0]/'example/plugin.yaml').write_text('name: example\ntitle: Override\n')
            self.assertEqual(locate(roots, 'example'), roots[0]/'example')

    def test_disabled_broken_code_is_diagnosed_without_execution_or_config_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'broken'; target.mkdir()
            (target/'plugin.yaml').write_text('name: broken\ntitle: Broken\n')
            (target/'.toggle-0').write_text('')
            (target/'hooks.py').write_text('raise RuntimeError("MUST_NOT_EXECUTE")\ndef bad(:\n')
            (target/'config.json').write_text('{"token":"SECRET_DO_NOT_READ"}')
            (target/'default_config.yaml').write_text('token: SECRET_DO_NOT_READ')
            report=inspect([root],'broken')
            self.assertFalse(report['passed']); self.assertFalse(report['executed_target_code'])
            self.assertEqual(report['issues'][0]['file'],'hooks.py')
            self.assertNotIn('SECRET_DO_NOT_READ',json.dumps(report))
            self.assertTrue((target/'.toggle-0').is_file())

    def test_invalid_path_and_oversized_source_fail_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('../escape','bad/name','',None,'a.b'):
                with self.assertRaises(ValueError): locate([tmp],name)
            target=Path(tmp)/'ok'; target.mkdir()
            (target/'plugin.yaml').write_text('name: ok\ntitle: OK\n')
            (target/'huge.py').write_bytes(b' '*(2*1024*1024+1))
            report=inspect([tmp],'ok')
            self.assertTrue(report['truncated']); self.assertFalse(report['passed'])

    def test_manifest_validation_and_file_budget_never_report_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'broken'; target.mkdir()
            self.assertFalse(inspect([root],'broken')['passed'])
            for value in ('name: wrong\ntitle: Wrong\n', 'name: broken\n', '[invalid'):
                (target/'plugin.yaml').write_text(value)
                self.assertFalse(inspect([root],'broken')['passed'])
            (target/'plugin.yaml').write_text('name: broken\ntitle: Broken\n')
            (target/'one.py').write_text('pass\n')
            with patch('usr.plugins.plugin_doctor.helpers.diagnostics.MAX_FILES',1):
                report=inspect([root],'broken')
            self.assertFalse(report['passed']); self.assertTrue(report['truncated'])
            self.assertEqual(len(report['files']),1)

    def test_linked_manifest_is_not_accepted_or_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'broken'; target.mkdir()
            external=root/'outside.yaml'; external.write_text('name: broken\ntitle: PRIVATE\n')
            manifest=target/'plugin.yaml'
            try: manifest.symlink_to(external)
            except OSError: self.skipTest('Symlink creation requires OS permission; also tested inside both host images')
            report=inspect([root],'broken')
            self.assertFalse(report['passed']); self.assertEqual(report['files'],[])
            self.assertNotIn('PRIVATE',json.dumps(report))
            with self.assertRaises(ValueError):
                (root/'linked').symlink_to(target,target_is_directory=True)
                locate([root],'linked')
