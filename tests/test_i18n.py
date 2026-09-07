"""The process selects its interface and report language once from LANG alone."""

import ast
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from yay_auto_review import cli, core, i18n


ROOT = Path(__file__).resolve().parents[1]


class LocaleResolutionTests(unittest.TestCase):
    def test_lang_alone_selects_language_with_english_fallback(self):
        cases = [
            ({}, "en"),
            ({"LANG": ""}, "en"),
            ({"LANG": "en_US.UTF-8"}, "en"),
            ({"LANG": "zh_CN.UTF-8"}, "zh_CN"),
            ({"LANG": "zh_CN", "LC_MESSAGES": "en_US", "LC_ALL": "C"}, "zh_CN"),
            ({"LANG": "en", "LC_MESSAGES": "zh_CN", "LC_ALL": "zh_CN"}, "en"),
            ({"LANG": "zh_CN", "YAY_AUTO_REVIEW_LANG": "en"}, "zh_CN"),
            ({"LC_ALL": "zh_CN", "LC_MESSAGES": "zh_CN", "YAY_AUTO_REVIEW_LANG": "zh_CN"}, "en"),
            ({"LANG": "de_DE.UTF-8", "YAY_AUTO_REVIEW_LANG": "zh_CN"}, "en"),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(i18n.resolve_language(environ=env), expected)

    def test_locale_aliases_and_unsupported_fallback(self):
        for name in ("C", "POSIX", "C.UTF-8", "en_US.UTF-8", "en-GB", "en"):
            self.assertEqual(i18n.normalize_language(name), "en")
        for name in ("zh", "zh_CN.UTF-8", "zh-CN", "zh_SG", "zh_Hans", "zh-Hans-CN"):
            self.assertEqual(i18n.normalize_language(name), "zh_CN")
        for name in ("zh_TW.UTF-8", "zh_Hant", "fr_FR", "../../invalid", "auto", ""):
            self.assertIsNone(i18n.normalize_language(name))

    def test_language_switching_api_is_unavailable(self):
        self.assertFalse(hasattr(i18n, "set_language"))
        self.assertFalse(hasattr(i18n, "use_language"))
        with self.assertRaises(TypeError):
            core.ReviewConfig(language="zh_CN")
        with self.assertRaises(TypeError):
            core.build_prompt(snapshot_fixture(Path("/tmp/demo")), "zh_CN")

    def test_missing_catalog_and_message_fall_back_to_english(self):
        self.assertEqual(i18n._catalog("not_a_supported_locale"), {})
        with mock.patch.object(i18n, "_LANGUAGE", "zh_CN"):
            self.assertEqual(i18n.t("A new message for {name}", name="demo"), "A new message for demo")
            self.assertEqual(i18n.t("Codex returned an invalid {}", "summary"), "Codex 返回了无效的 summary")

    def test_chinese_catalog_covers_all_literal_python_messages(self):
        catalog = i18n._catalog("zh_CN")
        messages = set()
        for path in (ROOT / "yay_auto_review" / name for name in ("core.py", "cli.py", "installer.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "t" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    messages.add(node.args[0].value)
        self.assertEqual(messages - catalog.keys(), set())
        for message, translation in catalog.items():
            self.assertEqual(i18n._fields(message), i18n._fields(translation), message)


class StartupLanguageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config" / "yay-auto-review" / "config.toml"
        self.config.parent.mkdir(parents=True)

    def run_python(self, script, *, lang="en_US.UTF-8", args=(), extra_env=None):
        environment = dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"),
                           XDG_CACHE_HOME=str(self.root / "cache"), LC_ALL="C",
                           LC_MESSAGES="C", YAY_AUTO_REVIEW_LANG="", LANG=lang)
        environment.pop(cli.SESSION_ENV, None)
        if extra_env:
            environment.update(extra_env)
        return subprocess.run([sys.executable, "-c", script, *args], cwd=ROOT,
                              env=environment, capture_output=True, text=True, timeout=10)

    def invoke(self, args, *, lang="en_US.UTF-8", extra_env=None):
        return self.run_python(
            "import sys; from yay_auto_review import cli; raise SystemExit(cli.main(sys.argv[1:]))",
            args=args, lang=lang, extra_env=extra_env)

    def test_cli_help_uses_lang_and_has_no_language_option(self):
        for lang, expected in (("en_US.UTF-8", "usage:"), ("zh_CN.UTF-8", "用法"),
                               ("de_DE.UTF-8", "usage:"), ("", "usage:")):
            with self.subTest(lang=lang):
                process = self.invoke(["--help"], lang=lang)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn(expected, process.stdout)
                self.assertNotIn("--lang", process.stdout)
                if expected == "usage:":
                    self.assertNotRegex(process.stdout, r"[\u4e00-\u9fff]")

    def test_standard_argparse_errors_follow_lang(self):
        for lang, expected in (("en_US.UTF-8", "the following arguments are required"),
                               ("zh_CN.UTF-8", "必须提供以下参数")):
            with self.subTest(lang=lang):
                process = self.invoke(["review"], lang=lang)
                self.assertEqual(process.returncode, 2)
                self.assertIn(expected, process.stderr)

    def test_removed_lang_option_is_rejected(self):
        for args in (["--lang", "zh_CN", "session"], ["session", "--lang", "zh_CN"]):
            with self.subTest(args=args):
                process = self.invoke(args)
                self.assertEqual(process.returncode, 2)
                self.assertFalse((self.root / "cache" / "yay-auto-review" / "builds").exists())

    def test_legacy_config_and_other_environment_variables_do_not_override_lang(self):
        for lang, old_value, expected in (("en_US.UTF-8", '"zh_CN"', "en"),
                                          ("zh_CN.UTF-8", '"en"', "zh_CN"),
                                          ("en_US.UTF-8", '42', "en")):
            with self.subTest(lang=lang, old_value=old_value):
                self.config.write_text("language = " + old_value + "\n", encoding="utf-8")
                process = self.run_python(
                    "from yay_auto_review import cli; print(cli.load_config()[0].language)",
                    lang=lang, extra_env={"LC_ALL": "C", "LC_MESSAGES": "zh_CN",
                                          "YAY_AUTO_REVIEW_LANG": "zh_CN"})
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(process.stdout, expected + "\n")

    def test_language_and_review_config_remain_fixed_after_environment_changes(self):
        script = """
import json, os
from pathlib import Path
from yay_auto_review import core, i18n
before = core.ReviewConfig()
os.environ['LANG'] = 'en_US.UTF-8' if i18n.get_language() == 'zh_CN' else 'zh_CN.UTF-8'
os.environ['YAY_AUTO_REVIEW_LANG'] = os.environ['LANG']
after = core.ReviewConfig()
snapshot = core.Snapshot('demo', 'a' * 40, '', {}, {}, Path('/tmp/demo'))
print(json.dumps([i18n.get_language(), before.language, after.language,
                  i18n.t('Red'), core.build_prompt(snapshot)]))
"""
        for lang, language, translated, report in (("en_US.UTF-8", "en", "Red", "English"),
                                                    ("zh_CN.UTF-8", "zh_CN", "红色", "Simplified Chinese")):
            with self.subTest(lang=lang):
                process = self.run_python(script, lang=lang)
                self.assertEqual(process.returncode, 0, process.stderr)
                data = json.loads(process.stdout)
                self.assertEqual(data[:4], [language, language, language, translated])
                self.assertIn("details should be in " + report, data[4])

    def test_configuration_errors_follow_startup_lang(self):
        self.config.write_text('timeout_seconds = 0\n', encoding="utf-8")
        for lang, expected in (("en_US.UTF-8", "must be between"), ("zh_CN.UTF-8", "必须为")):
            with self.subTest(lang=lang):
                process = self.invoke(["review", str(self.root), "--pkgbase", "demo"], lang=lang)
                self.assertEqual(process.returncode, 1)
                self.assertIn(expected, process.stderr)
        self.config.write_text('codex = [\n', encoding="utf-8")
        process = self.invoke(["review", str(self.root), "--pkgbase", "demo"], lang="zh_CN.UTF-8")
        self.assertEqual(process.returncode, 1)
        self.assertIn("无法解析配置", process.stderr)

    def test_session_prints_only_token_without_language_handoff(self):
        self.config.write_text('language = "en"\n', encoding="utf-8")
        process = self.invoke(["session"], lang="zh_CN.UTF-8")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertRegex(process.stdout, r"\A[0-9a-f]{32}\n\Z")
        self.assertEqual(process.stderr, "")
        directory = self.root / "cache" / "yay-auto-review" / "builds" / process.stdout.strip()
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / ".language").exists())

    def test_makepkg_entrypoint_uses_startup_lang(self):
        for lang, expected in (("en_US.UTF-8", "No valid yay review session"),
                               ("zh_CN.UTF-8", "缺少有效的 yay 审查会话")):
            with self.subTest(lang=lang):
                process = self.run_python(
                    "from yay_auto_review import cli; raise SystemExit(cli.makepkg_main())", lang=lang)
                self.assertEqual(process.returncode, 1)
                self.assertIn("●", process.stderr)
                self.assertIn(expected, process.stderr)


def snapshot_fixture(directory):
    files, modes = {"PKGBUILD": "pkgname=demo\n"}, {"PKGBUILD": 0o644}
    return core.Snapshot("demo", "a" * 40, core.content_digest(files, modes), files, modes, directory)


def review_response(language):
    return {
        "level": "yellow", "summary": "Coverage is limited" if language == "en" else "审查覆盖有限",
        "findings": [], "evidence": [], "open_source": None,
        "well_known": None, "official_source": None, "malicious_scripts": False,
        "refused": False, "snapshot_fully_reviewed": True,
    }


class ReviewLanguageTests(unittest.TestCase):
    def test_cache_is_separate_by_language_and_reused_with_localized_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            snapshot = snapshot_fixture(directory)
            results = []
            with mock.patch.object(core.Reviewer, "_run_codex",
                                   side_effect=lambda _: review_response(i18n.get_language())) as run:
                for language in ("en", "zh_CN", "en", "zh_CN"):
                    with mock.patch.object(i18n, "_LANGUAGE", language):
                        reviewer = core.Reviewer(core.ReviewConfig(cache_dir=directory / "cache"))
                        results.append(reviewer.review(snapshot))
            first, translated, cached, cached_chinese = results
            self.assertEqual(run.call_count, 2)
            self.assertFalse(first.cached)
            self.assertFalse(translated.cached)
            self.assertTrue(cached.cached)
            self.assertTrue(cached_chinese.cached)
            self.assertEqual(cached.summary, "Coverage is limited")
            self.assertEqual(cached_chinese.summary, "审查覆盖有限")
            self.assertIn("1 hour", cached.cache_reason)
            self.assertIn("report language", cached.cache_reason)
            self.assertIn("1 小时", cached_chinese.cache_reason)

    def test_codex_prompt_requests_startup_language_without_changing_policy(self):
        snapshot = snapshot_fixture(Path("/tmp/demo"))
        for language, name in (("en", "English"), ("zh_CN", "Simplified Chinese")):
            with mock.patch.object(i18n, "_LANGUAGE", language):
                prompt = core.build_prompt(snapshot)
            self.assertIn("details should be in " + name, prompt)
            self.assertIn("UNTRUSTED DATA", prompt)
            self.assertIn("Keep JSON property names, level codes", prompt)
            payload = json.loads(prompt.split("UNTRUSTED_PACKAGE_JSON:\n", 1)[1])
            self.assertEqual(payload["files"][0]["content"], snapshot.files["PKGBUILD"])

    def test_error_and_snapshot_findings_follow_startup_language(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(i18n, "_LANGUAGE", "zh_CN"):
            directory = Path(temporary)
            snapshot = dataclasses.replace(snapshot_fixture(directory), issues=(
                "Repository has no .SRCINFO; package metadata cannot be cross-checked",
            ))
            reviewer = core.Reviewer(core.ReviewConfig(cache_dir=directory / "cache"))
            with mock.patch.object(reviewer, "_run_codex", return_value=review_response("zh_CN")):
                result = reviewer.review(snapshot)
            self.assertIn("仓库缺少 .SRCINFO", result.findings[0])
            with mock.patch.object(reviewer, "_run_codex", side_effect=core.ReviewError("test failure")):
                changed = dataclasses.replace(snapshot, commit="b" * 40)
                result = reviewer.review(changed)
            self.assertEqual(result.level, "red")
            self.assertIn("审查未能可靠完成", result.summary)

    def test_display_and_confirmation_localize_without_weakening_red_gate(self):
        result = core.ReviewResult("white", "example", cached=True, cache_reason="unchanged")
        for language, skipped in (("en", "Review skipped"), ("zh_CN", "跳过 review")):
            output = io.StringIO()
            with mock.patch.object(i18n, "_LANGUAGE", language), contextlib.redirect_stderr(output), \
                 mock.patch("builtins.open") as terminal:
                cli.display("demo", result)
                self.assertFalse(cli.confirm("demo", "red"))
                terminal.assert_not_called()
            self.assertIn("● demo — example", output.getvalue())
            self.assertIn(skipped, output.getvalue())


if __name__ == "__main__":
    unittest.main()
