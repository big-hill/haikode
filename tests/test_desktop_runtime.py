"""Execute the exact shell program embedded in the native runtime bridge."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DesktopRuntimeTests(unittest.TestCase):
    def script(self):
        source = (ROOT / 'desktop/src/domain/PythonRuntime.h').read_text()
        return ''.join(ast.literal_eval(line.strip()) for line in
                       source.splitlines() if line.lstrip().startswith('"'))

    def run_runtime(self, candidates, args=('sessions',), extra=None):
        with tempfile.TemporaryDirectory() as directory:
            for name, body in candidates.items():
                path = Path(directory) / name
                path.write_text('#!/bin/sh\n' + body)
                path.chmod(0o755)
            env = dict(os.environ, PATH=directory, PYTHONPATH='/stale/tree')
            env.pop('HAI_PYTHONPATH', None)
            env.update(extra or {})
            return subprocess.run(['/bin/sh', '-c', self.script(),
                                   'haikode-runtime', 'haikode.configtool', *args],
                                  env=env, text=True, capture_output=True)

    def test_versioned_only_interpreter(self):
        result = self.run_runtime({'python3.10':
            'if [ "$1" = -c ]; then exit 0; fi\nprintf "%s\\n" "$@"\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ['-m', 'haikode.configtool', 'sessions'])

    def test_future_version_and_incompatible_plain_python(self):
        result = self.run_runtime({'python3': 'exit 1\n', 'python3.27':
            'if [ "$1" = -c ]; then exit 0; fi\necho future\n'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'future')

    def test_no_runtime_reports_actionable_failure(self):
        result = self.run_runtime({})
        self.assertEqual(result.returncode, 127)
        self.assertIn('Python 3.10+', result.stderr)

    def test_stale_pythonpath_is_not_inherited(self):
        result = self.run_runtime({'python3.10':
            'if [ "$1" = -c ]; then exit 0; fi\nprintf "%s" "$PYTHONPATH"\n'})
        self.assertEqual(result.stdout, '')

    def test_explicit_developer_path_and_quoted_arguments(self):
        result = self.run_runtime({'python3.10':
            'if [ "$1" = -c ]; then exit 0; fi\nprintf "%s\\n" "$PYTHONPATH" "$@"\n'},
            args=('a b', "x'y", '$(exit 9)'), extra={'HAI_PYTHONPATH': '/dev tree'})
        self.assertEqual(result.stdout.splitlines(),
                         ['/dev tree', '-m', 'haikode.configtool', 'a b', "x'y", '$(exit 9)'])

    def test_module_failure_is_not_retried_with_another_runtime(self):
        result = self.run_runtime({'python3.10':
            'if [ "$1" = -c ]; then exit 0; fi\necho actual-error >&2\nexit 23\n',
            'python3': 'echo wrong-runtime\n'})
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, '')
        self.assertIn('actual-error', result.stderr)

    def test_native_consumers_share_runtime(self):
        for name in ('ConfigBridge.cpp', 'AppController.cpp'):
            source = (ROOT / 'desktop/src/domain' / name).read_text()
            self.assertIn('kPythonRuntimeScript', source)
            self.assertNotIn('/boot/home/haikode', source)

    def test_sessions_do_not_require_provider_config(self):
        from haikode import configtool
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, HAIKODE_CONFIG_DIR=directory), \
                    patch.object(configtool, 'Config', side_effect=AssertionError('config read')):
                self.assertEqual(configtool.main(['sessions']), 0)

    @unittest.skipUnless(sys.platform.startswith('haiku'), 'requires native libbe')
    def test_compiled_config_bridge_output_stdin_and_failure(self):
        # Execute the real native bridge without a GUI or user database.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'haikode'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'configtool.py').write_text(
                'import sys\n'
                'if __name__ == "__main__":\n'
                '    if sys.argv[1] == "fail":\n'
                '        print("actual bridge error", file=sys.stderr)\n'
                '        sys.exit(23)\n'
                '    print(sys.stdin.read() if sys.argv[1] == "stdin" else "sessions-ok")\n')
            executable = root / 'bridge-probe'
            compiler = ['g++']
            if subprocess.check_output(['getarch'], text=True).strip() == 'x86_gcc2':
                compiler = ['setarch', 'x86', 'g++']
            subprocess.run(compiler + ['-I', str(ROOT / 'desktop/src/domain'),
                str(ROOT / 'tests/native/config_bridge_probe.cpp'),
                str(ROOT / 'desktop/src/domain/ConfigBridge.cpp'), '-lbe',
                '-o', str(executable)], check=True, capture_output=True)
            env = dict(os.environ, HAI_PYTHONPATH=directory)
            for mode, status, output in [('sessions', 0, 'sessions-ok'),
                                         ('stdin', 0, 'private input'),
                                         ('fail', 23, 'actual bridge error')]:
                result = subprocess.run([str(executable), mode], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout.strip(), output)
            env.update(HAI_PYTHONPATH=str(ROOT),
                       HAIKODE_CONFIG_DIR=str(root / 'isolated-store'),
                       HAI_DISABLE_KEYSTORE='1')
            result = subprocess.run([str(executable), 'sessions'], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, '')

    def test_session_failure_is_not_mislabelled_or_selectable(self):
        source = (ROOT / 'desktop/src/ui/HaiWindow.cpp').read_text()
        self.assertNotIn('store busy', source)
        self.assertNotIn('MSG_SESSIONS_RETRY', source)
        self.assertIn('dynamic_cast<SessionItem*>', source)


if __name__ == '__main__':
    unittest.main()
