"""Install into temporary user directories and exercise the generated launchers."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aur_auto_review import cli, installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "prefix 'quoted' 安装"
        self.config = self.root / "config"
        self.cache = self.root / "cache"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.tool("yay", "#!/bin/sh\nprintf '%s\\n' 'yay v13.0.1 - libalpm v16.0.1'\n")
        self.tool("codex", "#!/bin/sh\nprintf '%s\\n' '--output-schema --ignore-user-config --ignore-rules --ephemeral'\n")
        environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.config), "XDG_CACHE_HOME": str(self.cache),
            "PATH": str(self.tools) + os.pathsep + os.environ["PATH"],
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.init = self.config / "yay" / "init.lua"
        self.init.parent.mkdir(parents=True)

    def tool(self, name, body):
        path = self.tools / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.prefix)

    def test_existing_configuration_preserved_backed_up_and_second_install_idempotent(self):
        original = '-- 自定义设置\nyay.opt.editor = "nano"\n'
        self.init.write_text(original, encoding="utf-8")
        self.install()
        after = self.init.read_text(encoding="utf-8")
        backups = list(self.init.parent.glob("init.lua.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        self.assertTrue(after.startswith(original))
        self.assertEqual(after.count(installer.BEGIN), 1)
        self.assertEqual(after.count(installer.END), 1)
        self.assertIn("dofile(", after)
        self.install()
        self.assertEqual(self.init.read_text(encoding="utf-8"), after)
        self.assertEqual(list(self.init.parent.glob("init.lua.bak.*")), backups)

    def test_install_without_existing_init_does_not_create_spurious_backup(self):
        self.install()
        self.assertTrue(self.init.is_file())
        self.assertEqual(list(self.init.parent.glob("init.lua.bak.*")), [])
        for name in ("aur-auto-review", "aur-auto-review-makepkg"):
            self.assertTrue(os.access(self.prefix / "bin" / name, os.X_OK))
        plugin = (self.init.parent / "aur-auto-review.lua").read_text(encoding="utf-8")
        self.assertIn(str(self.prefix / "bin" / "aur-auto-review"), plugin)
        self.assertIn(str(self.prefix / "bin" / "aur-auto-review-makepkg"), plugin)

    def test_reinstall_preserves_unrelated_settings_after_managed_block(self):
        self.install()
        appended = '\n-- Keep this hook\nyay.opt.editor = "vim"\n'
        with self.init.open("a", encoding="utf-8") as stream:
            stream.write(appended)
        self.install()
        self.assertTrue(self.init.read_text(encoding="utf-8").endswith(appended))
        self.assertEqual(self.init.read_text().count(installer.BEGIN), 1)

    def test_malformed_existing_markers_abort_before_install_changes(self):
        for contents in (installer.BEGIN, installer.END,
                         installer.BEGIN + installer.END + installer.BEGIN + installer.END):
            with self.subTest(contents=contents):
                self.init.write_text(contents)
                with self.assertRaises(cli.GateError):
                    self.install()
                self.assertEqual(self.init.read_text(), contents)
                self.assertFalse((self.prefix / "bin").exists())

    def test_incompatible_yay_or_codex_does_not_change_configuration(self):
        original = '-- untouched\n'
        self.init.write_text(original)
        self.tool("yay", "#!/bin/sh\nprintf '%s\\n' 'yay v12.5.0'\n")
        with self.assertRaises(cli.GateError):
            self.install()
        self.assertEqual(self.init.read_text(), original)
        self.assertFalse((self.prefix / "bin").exists())
        self.tool("yay", "#!/bin/sh\nprintf '%s\\n' 'yay v13.0.1'\n")
        self.tool("codex", "#!/bin/sh\nprintf '%s\\n' '--output-schema'\n")
        with self.assertRaises(cli.GateError):
            self.install()
        self.assertEqual(self.init.read_text(), original)
        self.assertFalse((self.prefix / "bin").exists())

    def test_launchers_ignore_cwd_and_pythonpath_code_in_aur_checkout(self):
        self.install()
        checkout = self.root / "untrusted-package"
        checkout.mkdir()
        marker = self.root / "package-code-executed"
        malicious = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('package injected')\n"
        (checkout / "sitecustomize.py").write_text(malicious)
        (checkout / "usercustomize.py").write_text(malicious)
        package = checkout / "aur_auto_review"
        package.mkdir()
        (package / "__init__.py").write_text(malicious)
        (package / "cli.py").write_text(malicious)
        environment = dict(os.environ, PYTHONPATH=str(checkout), PYTHONSTARTUP=str(checkout / "sitecustomize.py"))
        environment.pop(cli.SESSION_ENV, None)
        for name, arguments, expected in (
            ("aur-auto-review", ["--help"], 0),
            ("aur-auto-review-makepkg", ["--verifysource"], 1),
        ):
            with self.subTest(launcher=name):
                launcher = self.prefix / "bin" / name
                self.assertIn(" -I", launcher.read_text().splitlines()[0])
                process = subprocess.run([str(launcher), *arguments], cwd=checkout,
                                         env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, expected, process.stderr)
                self.assertFalse(marker.exists())
                self.assertNotIn("package injected", process.stderr)

    def test_installed_session_helper_creates_private_directory_and_prints_only_token(self):
        self.install()
        process = subprocess.run([str(self.prefix / "bin" / "aur-auto-review"), "session"],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertRegex(process.stdout, r"\A[0-9a-f]{32}\n\Z")
        token = process.stdout.strip()
        directory = self.cache / "aur-auto-review" / "builds" / token
        self.assertTrue(directory.is_dir())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(process.stderr, "")


if __name__ == "__main__":
    unittest.main()
