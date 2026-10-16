import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shocking_vrchat as core


class FrontendLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def launch(self, args):
        with patch.object(core, 'APP_DIR', self.root), patch.object(core.sys, 'argv', ['neko_vrc.py', *args]), patch('subprocess.Popen') as process:
            core.main()
            return process.call_args

    def test_packaged_winui_entry(self):
        path = self.executable('Neko-VRC.exe')
        self.assertEqual(self.launch([]).args[0], [str(path)])

    def test_source_defaults_to_winui_not_wpf(self):
        self.executable('desktop/NekoVRC.Desktop/bin/Debug/net10.0-windows/Neko-VRC.exe')
        path = self.executable('desktop/NekoVRC.WinUI/bin/x64/Debug/net10.0-windows10.0.19041.0/win-x64/Neko-VRC.exe')
        self.assertEqual(self.launch([]).args[0], [str(path)])

    def test_explicit_wpf_does_not_launch_packaged_winui(self):
        self.executable('Neko-VRC.exe')
        path = self.executable('desktop/NekoVRC.Desktop/bin/Debug/net10.0-windows/Neko-VRC.exe')
        self.assertEqual(self.launch(['--wpf-ui']).args[0], [str(path)])

    def test_missing_winui_does_not_silently_fallback(self):
        self.executable('desktop/NekoVRC.Desktop/bin/Debug/net10.0-windows/Neko-VRC.exe')
        with self.assertRaisesRegex(RuntimeError, 'WinUI 3'):
            self.launch([])

    def test_missing_explicit_wpf_reports_wpf(self):
        self.executable('Neko-VRC.exe')
        with self.assertRaisesRegex(RuntimeError, 'WPF'):
            self.launch(['--wpf-ui'])

    def test_legacy_opt_in(self):
        with patch.object(core, 'legacy_main') as legacy, patch.object(core.sys, 'argv', ['neko_vrc.py', '--legacy-ui']), patch('subprocess.Popen') as process:
            core.main()
            legacy.assert_called_once()
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
