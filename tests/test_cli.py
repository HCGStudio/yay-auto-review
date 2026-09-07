"""Exercise the approval boundary with real Git data and no package execution."""

import argparse
import contextlib
import dataclasses
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from yay_auto_review import cli, core


class GateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            "YAY_AUTO_REVIEW_LANG": "zh_CN",
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.session = cli.new_session()
        session_environment = mock.patch.dict(os.environ, {cli.SESSION_ENV: self.session})
        session_environment.start()
        self.addCleanup(session_environment.stop)
        self.repo = cli.session_directory(self.session) / "demo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("PKGBUILD", "pkgname=demo\npkgver=1\npkgrel=1\n")
        self.write(".SRCINFO", "pkgbase = demo\n\tpkgver = 1\n\tpkgrel = 1\n")
        self.write("demo.install", "post_install() { true; }\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.args = argparse.Namespace(directory=str(self.repo), pkgbase="demo",
                                       last_modified=1234567890, version="1-1")
        self.result = core.ReviewResult("green", "完整审查通过", reviewed_at=1234567890)
        self.makepkg = self.root / "makepkg"
        self.makepkg.write_text("#!/bin/sh\nexit 97\n")
        self.makepkg.chmod(0o755)
        self.config = core.ReviewConfig(cache_dir=self.root / "reviews")
        self.reviewer = self.patch("Reviewer").return_value
        self.reviewer.review.return_value = self.result
        self.remote = self.patch("remote_head", return_value=self.head)
        self.confirm = self.patch("confirm", return_value=True)
        self.execv = self.patch_object(cli.os, "execv")
        self.patch("reject_yay_overrides")
        self.config_loader = self.patch("load_config", return_value=(self.config, str(self.makepkg)))
        self.output = io.StringIO()
        redirect = contextlib.redirect_stderr(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def patch(self, name, **kwargs):
        return self.patch_object(cli, name, **kwargs)

    def patch_object(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout

    def write(self, name, contents):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def gate(self, *args):
        with contextlib.chdir(self.repo):
            return cli.makepkg_gate(list(args) or ["--verifysource"])

    def receipt(self):
        return cli.read_receipt(self.repo, self.session)

    def test_each_permitted_grade_requires_confirmation_and_records_exact_files(self):
        for level in ("green", "white", "yellow"):
            with self.subTest(level=level):
                self.reviewer.review.return_value = dataclasses.replace(self.result, level=level)
                self.assertEqual(cli.hook(self.args), 0)
                self.confirm.assert_called_with("demo", level)
                receipt = self.receipt()
                self.assertEqual(receipt["commit"], self.head)
                self.assertEqual(receipt["session"], self.session)
                self.assertEqual(receipt["digest"], core.snapshot_package(self.repo).digest)
                self.assertEqual(set(receipt["paths"]), {"PKGBUILD", ".SRCINFO", "demo.install"})
                self.assertFalse(receipt["started"])
                self.assertEqual(receipt["context"], {"last_modified": 1234567890, "version": "1-1"})
        self.execv.assert_not_called()

    def test_rejection_and_red_result_never_issue_receipt(self):
        for level in ("green", "white", "yellow", "red"):
            with self.subTest(level=level):
                self.reviewer.review.return_value = dataclasses.replace(self.result, level=level)
                self.confirm.return_value = False
                with self.assertRaises(cli.GateError):
                    cli.hook(self.args)
                self.assertFalse(cli.receipt_path(self.repo, self.session).exists())
        self.execv.assert_not_called()

    def test_cached_result_still_displays_skipped_package_reason_and_requires_consent(self):
        self.reviewer.review.return_value = dataclasses.replace(
            self.result, cached=True, cache_reason="上次审查未满 1 小时，AUR 提交和文件未变")
        cli.hook(self.args)
        self.assertIn("跳过 review: demo", self.output.getvalue())
        self.assertIn("未满 1 小时", self.output.getvalue())
        self.confirm.assert_called_once_with("demo", "green")

    def test_makepkg_without_session_receipt_cannot_start(self):
        with self.assertRaises(cli.GateError):
            self.gate()
        self.reviewer.review.assert_not_called()
        self.execv.assert_not_called()

    def test_first_makepkg_rechecks_full_snapshot_and_re_reviews_added_files(self):
        cli.hook(self.args)
        self.write("ignored-helper.sh", "printf 'new helper'\n")
        self.gate("--verifysource", "--skippgpcheck", "-f", "-Cc")
        self.assertEqual(self.reviewer.review.call_count, 2)
        reviewed = self.reviewer.review.call_args.args[0]
        self.assertIn("ignored-helper.sh", reviewed.files)
        self.assertTrue(self.receipt()["started"])
        self.execv.assert_called_once()
        executable, arguments = self.execv.call_args.args
        self.assertEqual(executable, str(self.makepkg))
        self.assertEqual(arguments[:5],
                         [str(self.makepkg), "--verifysource", "--skippgpcheck", "-f", "-Cc"])

    def test_unchanged_first_makepkg_uses_consent_without_re_review(self):
        cli.hook(self.args)
        self.gate()
        self.assertEqual(self.reviewer.review.call_count, 1)
        self.assertEqual(self.confirm.call_count, 1)
        self.assertTrue(self.receipt()["started"])
        self.execv.assert_called_once()

    def test_makepkg_outputs_override_shared_paths_with_current_session_directory(self):
        cli.hook(self.args)
        self.gate("--packagelist", "PKGDEST=/tmp/old-binaries", "SRCDEST=/tmp/old-sources",
                  "BUILDFILE=other-recipe")
        _, arguments = self.execv.call_args.args
        assignments = dict(arg.split("=", 1) for arg in arguments[1:]
                           if "=" in arg and not arg.startswith("-"))
        for name in ("PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST", "BUILDDIR"):
            self.assertEqual(assignments[name], str(self.repo))
        self.assertEqual(assignments["BUILDFILE"], "PKGBUILD")

    @unittest.skipUnless(shutil.which("makepkg") and os.geteuid() != 0,
                         "makepkg smoke test requires a non-root Arch user")
    def test_real_makepkg_packagelist_uses_reviewed_recipe_and_private_archive_directory(self):
        real_makepkg = shutil.which("makepkg")
        self.config_loader.return_value = self.config, real_makepkg
        self.write("PKGBUILD", "pkgname=demo\npkgver=1\npkgrel=1\narch=('any')\n"
                   "pkgdesc='benign test fixture'\nlicense=('MIT')\npackage() { :; }\n")
        marker = self.root / "unreviewed-recipe-executed"
        self.write("other-recipe", "touch " + shlex.quote(str(marker)) + "\n"
                   "pkgname=other\npkgver=9\npkgrel=1\narch=('any')\n")
        stale = self.root / "stale-archive-cache"
        stale.mkdir()
        configuration = self.root / "makepkg.conf"
        configuration.write_text(
            "CARCH=x86_64\nCHOST=x86_64-pc-linux-gnu\nPKGEXT='.pkg.tar.zst'\n"
            "SRCEXT='.src.tar.gz'\nPKGDEST=" + shlex.quote(str(stale)) + "\n"
            "BUILDFILE=other-recipe\n", encoding="utf-8",
        )
        cli.hook(self.args)
        processes = []
        def execute(_executable, arguments):
            process = subprocess.run(arguments, cwd=self.repo, text=True,
                                     capture_output=True, timeout=15)
            processes.append(process)
        self.execv.side_effect = execute
        self.gate("--config", str(configuration), "--packagelist",
                  "PKGDEST=" + str(stale), "BUILDFILE=other-recipe")
        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), str(self.repo / "demo-1-1-any.pkg.tar.zst"))
        self.assertFalse(marker.exists())
        self.assertEqual(list(stale.iterdir()), [])

    def test_downloaded_binary_sources_are_ignored_after_source_step(self):
        cli.hook(self.args)
        self.gate()
        (self.repo / "upstream.tar.gz").write_bytes(b"\x00\xffupstream archive")
        source = self.repo / "src" / "upstream"
        source.mkdir(parents=True)
        (source / "binary").write_bytes(b"\x00binary")
        self.gate("--packagelist")
        self.assertEqual(self.reviewer.review.call_count, 1)
        self.assertEqual(self.execv.call_count, 2)

    def test_original_install_script_change_requires_new_review_and_confirmation(self):
        cli.hook(self.args)
        self.gate()
        self.write("demo.install", "post_install() { printf 'changed'; }\n")
        self.gate("--packagelist")
        self.assertEqual(self.reviewer.review.call_count, 2)
        self.assertEqual(self.confirm.call_count, 2)
        self.assertIn("changed", self.reviewer.review.call_args.args[0].files["demo.install"])

    def test_updated_pkgver_is_reviewed_again_before_next_makepkg(self):
        cli.hook(self.args)
        self.gate()
        self.write("PKGBUILD", "pkgname=demo\npkgver=2\npkgrel=1\n")
        self.gate("--packagelist")
        self.assertEqual(self.reviewer.review.call_count, 2)
        self.assertEqual(self.confirm.call_count, 2)
        self.assertIn("pkgver=2", self.reviewer.review.call_args.args[0].files["PKGBUILD"])

    def test_newly_tracked_script_is_reviewed_after_source_download(self):
        cli.hook(self.args)
        self.gate()
        self.write("new.install", "post_install() { printf new; }\n")
        self.git("add", "new.install")
        self.gate()
        self.assertEqual(self.reviewer.review.call_count, 2)
        self.assertIn("new.install", self.reviewer.review.call_args.args[0].files)

    def test_removed_original_script_prevents_makepkg(self):
        cli.hook(self.args)
        self.gate()
        self.execv.reset_mock()
        (self.repo / "demo.install").unlink()
        with self.assertRaises(core.SnapshotError):
            self.gate()
        self.execv.assert_not_called()

    def test_remote_change_aborts_before_review_or_cached_result_can_be_used(self):
        self.remote.return_value = "f" * 40
        with self.assertRaises(cli.GateError):
            cli.hook(self.args)
        self.reviewer.review.assert_not_called()
        self.confirm.assert_not_called()
        self.execv.assert_not_called()

    def test_remote_verification_failure_cannot_be_ignored(self):
        self.remote.side_effect = cli.GateError("offline")
        with self.assertRaises(cli.GateError):
            cli.hook(self.args)
        self.reviewer.review.assert_not_called()
        self.confirm.assert_not_called()

    def test_mutation_while_user_confirms_invalidates_approval(self):
        def mutate(*_):
            self.write("PKGBUILD", "pkgname=changed-after-review\n")
            return True
        self.confirm.side_effect = mutate
        with self.assertRaises(cli.GateError):
            cli.hook(self.args)
        self.assertFalse(cli.receipt_path(self.repo, self.session).exists())
        self.execv.assert_not_called()

    def test_makepkg_re_review_denial_or_mutation_does_not_execute(self):
        cli.hook(self.args)
        self.write("demo.install", "post_install() { printf changed; }\n")
        self.confirm.return_value = False
        with self.assertRaises(cli.GateError):
            self.gate()
        self.execv.assert_not_called()
        self.confirm.side_effect = lambda *_: bool(self.write("PKGBUILD", "changed=again\n"))
        with self.assertRaises(cli.GateError):
            self.gate()
        self.execv.assert_not_called()

    def test_alternate_pkgbuild_flags_are_blocked(self):
        cli.hook(self.args)
        for args in (("-p", "other"), ("-fpother",), ("--pkgbuild=other",),
                     ("-D", "/tmp/other"), ("-fD/tmp/other",), ("--dir=/tmp/other",),
                     ("--d=/tmp/other",), ("--di", "/tmp/other")):
            with self.subTest(args=args), self.assertRaises(cli.GateError):
                self.gate(*args)
        self.execv.assert_not_called()

    def test_session_and_directory_must_match_exactly(self):
        bad_args = [argparse.Namespace(**vars(self.args))]
        bad_args[0].directory = str(self.root)
        bad_args.append(argparse.Namespace(**dict(vars(self.args), pkgbase="../demo")))
        bad_args.append(argparse.Namespace(**dict(vars(self.args), pkgbase="another")))
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        bad_args.append(argparse.Namespace(**dict(vars(self.args), directory=str(alias))))
        for args in bad_args:
            with self.subTest(args=args), self.assertRaises(cli.GateError):
                cli.hook(args)
        with mock.patch.dict(os.environ, {cli.SESSION_ENV: "bad-session"}):
            with self.assertRaises(cli.GateError):
                cli.hook(self.args)
        self.reviewer.review.assert_not_called()


class ConfirmationTests(unittest.TestCase):
    def test_noconfirm_and_piped_yes_cannot_replace_controlling_terminal(self):
        with mock.patch.object(cli.sys, "argv", ["yay", "-Syu", "--noconfirm"]), \
             mock.patch.object(cli.sys, "stdin", io.StringIO("yes\n")), \
             mock.patch("builtins.open", side_effect=OSError("no controlling terminal")) as opened, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cli.confirm("demo", "green"))
            opened.assert_called_once_with("/dev/tty", "r+", encoding="utf-8", buffering=1)

    def test_red_cannot_be_approved_even_with_a_terminal(self):
        with mock.patch("builtins.open") as opened, contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cli.confirm("demo", "red"))
            opened.assert_not_called()

    def test_yellow_requires_exact_package_name_and_green_defaults_no(self):
        for level, answer, approved in [
            ("yellow", "demo\n", True), ("yellow", "yes\n", False),
            ("yellow", "\n", False), ("green", "\n", False),
            ("green", "yes\n", True), ("white", "Y\n", True),
        ]:
            with self.subTest(level=level, answer=answer):
                tty = mock.MagicMock()
                tty.__enter__.return_value = tty
                tty.readline.return_value = answer
                with mock.patch("builtins.open", return_value=tty):
                    self.assertEqual(cli.confirm("demo", level), approved)


class YayOverrideTests(unittest.TestCase):
    def test_cli_flags_cannot_replace_guard_or_build_directory(self):
        for flag in ("--makepkg", "--makepkg=evil", "--builddir=/tmp/old",
                     "--mflags=-pother", "--save"):
            with self.subTest(flag=flag), self.assertRaises(cli.GateError):
                self.check_argv(["yay", "-Syu", flag])

    def test_noconfirm_does_not_disable_hook(self):
        self.check_argv(["yay", "-Syu", "--noconfirm"])

    def check_argv(self, arguments):
        process = mock.MagicMock()
        command = mock.MagicMock()
        command.read_bytes.return_value = b"\0".join(os.fsencode(arg) for arg in arguments) + b"\0"
        executable = mock.MagicMock()
        executable.resolve.return_value.name = "yay"
        process.joinpath.side_effect = {"cmdline": command, "exe": executable}.__getitem__
        with mock.patch.object(cli.os, "getppid", return_value=123), \
             mock.patch.object(cli, "Path", side_effect=lambda value: process if value == "/proc/123" else Path(value)):
            cli.reject_yay_overrides()


if __name__ == "__main__":
    unittest.main()
