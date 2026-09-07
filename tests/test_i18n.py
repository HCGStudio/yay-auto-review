"""Locale selection and review language are independent of the host locale."""

import ast
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from yay_auto_review import cli, core, i18n


class LocaleResolutionTests(unittest.TestCase):
    def test_posix_precedence_and_empty_values(self):
        cases = [
            ({}, "en"),
            ({"LANG": "zh_CN.UTF-8"}, "zh_CN"),
            ({"LANG": "zh_CN", "LC_MESSAGES": "en_US.UTF-8"}, "en"),
            ({"LANG": "en_US", "LC_MESSAGES": "zh_CN", "LC_ALL": "C"}, "en"),
            ({"LANG": "en", "LC_MESSAGES": "C", "LC_ALL": "zh_CN.UTF-8"}, "zh_CN"),
            ({"LANG": "zh_CN", "LC_MESSAGES": "", "LC_ALL": ""}, "zh_CN"),
            ({"LANG": "zh_CN", "LC_ALL": "de_DE.UTF-8"}, "en"),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(i18n.resolve_language(environ=env), expected)

    def test_application_environment_config_and_cli_precedence(self):
        env = {"LANG": "en_US.UTF-8"}
        self.assertEqual(i18n.resolve_language("zh_CN", environ=env), "zh_CN")
        env["YAY_AUTO_REVIEW_LANG"] = "en"
        self.assertEqual(i18n.resolve_language("zh_CN", environ=env), "en")
        self.assertEqual(i18n.resolve_language("en", override="zh_CN", environ=env), "zh_CN")
        env["YAY_AUTO_REVIEW_LANG"] = "auto"
        self.assertEqual(i18n.resolve_language("zh_CN", environ=env), "zh_CN")
        env["YAY_AUTO_REVIEW_LANG"] = "fr_FR"
        self.assertEqual(i18n.resolve_language("zh_CN", environ=env), "en")

    def test_locale_aliases_and_unsupported_fallback(self):
        for name in ("C", "POSIX", "C.UTF-8", "en_US.UTF-8", "en-GB", "en"):
            self.assertEqual(i18n.normalize_language(name), "en")
        for name in ("zh", "zh_CN.UTF-8", "zh-CN", "zh_SG", "zh_Hans", "zh-Hans-CN"):
            self.assertEqual(i18n.normalize_language(name), "zh_CN")
        for name in ("zh_TW.UTF-8", "zh_Hant", "fr_FR", "../../invalid", "auto", ""):
            self.assertIsNone(i18n.normalize_language(name))

    def test_context_language_is_restored(self):
        with i18n.use_language("en"):
            self.assertEqual(i18n.t("Red"), "Red")
            with i18n.use_language("zh_CN"):
                self.assertEqual(i18n.t("Red"), "红色")
            self.assertEqual(i18n.t("Red"), "Red")

    def test_missing_catalog_and_message_fall_back_to_english(self):
        self.assertEqual(i18n._catalog("not_a_supported_locale"), {})
        with i18n.use_language("zh_CN"):
            self.assertEqual(i18n.t("A new message for {name}", name="demo"), "A new message for demo")
            self.assertEqual(i18n.t("Codex returned an invalid {}", "summary"), "Codex 返回了无效的 summary")

    def test_chinese_catalog_covers_all_literal_python_messages(self):
        root = Path(__file__).resolve().parents[1] / "yay_auto_review"
        catalog = i18n._catalog("zh_CN")
        messages = set(cli.LABELS.values())
        for path in (root / "core.py", root / "cli.py", root / "installer.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "t" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    messages.add(node.args[0].value)
        self.assertEqual(messages - catalog.keys(), set())
        for message, translation in catalog.items():
            self.assertEqual(i18n._fields(message), i18n._fields(translation), message)


class ConfigAndCliLanguageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config" / "yay-auto-review" / "config.toml"
        self.config.parent.mkdir(parents=True)
        environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "LANG": "en_US.UTF-8", "LC_ALL": "", "LC_MESSAGES": "", "YAY_AUTO_REVIEW_LANG": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        language = i18n.use_language("en")
        language.__enter__()
        self.addCleanup(language.__exit__, None, None, None)

    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), errors.getvalue()

    def test_config_language_controls_review_config_and_errors(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        config, _ = cli.load_config()
        self.assertEqual(config.language, "zh_CN")
        self.config.write_text('language = "zh_CN"\ntimeout_seconds = 0\n', encoding="utf-8")
        with self.assertRaisesRegex(cli.GateError, "必须为"):
            cli.load_config()

    def test_invalid_config_language_fails_closed(self):
        for value in ('"de"', '42', '["en"]', '{ x = "en" }'):
            self.config.write_text("language = " + value + "\n", encoding="utf-8")
            code, _, error = self.invoke(["session"])
            self.assertEqual(code, 1)
            self.assertIn("language must be", error)
            self.assertFalse((self.root / "cache" / "yay-auto-review" / "builds").exists())

    def test_config_and_locale_translate_help_and_standard_argparse_text(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        code, output, error = self.invoke(["--help"])
        self.assertEqual(code, 0, error)
        self.assertIn("用法", output)
        self.assertIn("显示此帮助信息并退出", output)
        self.assertIn("为当前用户启用", output)
        code, _, error = self.invoke(["review"])
        self.assertEqual(code, 2)
        self.assertIn("必须提供以下参数", error)
        self.assertNotIn("the following arguments are required", error)

    def test_cli_overrides_environment_before_or_after_subcommand(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"YAY_AUTO_REVIEW_LANG": "zh_CN"}):
            for args in (["--lang", "en", "--help"], ["review", "--lang", "en", "--help"]):
                code, output, error = self.invoke(args)
                self.assertEqual(code, 0, error)
                self.assertIn("usage:", output)
                self.assertIn("show this help message", output)
                self.assertNotIn("用法", output)

    def test_invalid_cli_language_is_an_argument_error(self):
        code, _, errors = self.invoke(["--lang", "not-a-language", "session"])
        self.assertEqual(code, 2)
        self.assertIn("language must be", errors)

    def test_cli_override_localizes_invalid_config_errors(self):
        self.config.write_text('language = "de"\n', encoding="utf-8")
        code, _, error = self.invoke(["--lang", "zh_CN", "session"])
        self.assertEqual(code, 1)
        self.assertIn("language 必须为", error)

    def test_cli_override_localizes_configuration_syntax_error(self):
        self.config.write_text('language = [\n', encoding="utf-8")
        code, _, error = self.invoke(["--lang", "zh_CN", "session"])
        self.assertEqual(code, 1)
        self.assertIn("无法解析配置", error)

    def test_cli_override_survives_config_load_and_json_codes_stay_stable(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        snapshot = snapshot_fixture(self.root)
        languages = []
        def review(_self, _snapshot):
            languages.append(_self.config.language)
            return core.ReviewResult("yellow", "Coverage is limited")
        with mock.patch.object(cli, "snapshot_package", return_value=snapshot), \
             mock.patch.object(cli, "remote_head", return_value=snapshot.commit), \
             mock.patch.object(core.Reviewer, "review", review):
            code, output, errors = self.invoke([
                "review", str(self.root), "--pkgbase", "demo", "--json", "--lang", "en",
            ])
        self.assertEqual(code, 0, errors)
        self.assertEqual(languages, ["en"])
        data = json.loads(output)
        self.assertEqual(data["level"], "yellow")
        self.assertIn("summary", data)

    def test_session_publishes_selected_config_language_without_changing_stdout(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        code, output, error = self.invoke(["session"])
        self.assertEqual(code, 0, error)
        session = output.strip()
        self.assertRegex(session, r"^[0-9a-f]{32}$")
        language_file = cli.session_directory(session) / ".language"
        self.assertEqual(language_file.read_text(), "zh_CN\n")
        self.assertEqual(language_file.stat().st_mode & 0o777, 0o600)

    def test_makepkg_entrypoint_selects_config_language_before_gate(self):
        self.config.write_text('language = "zh_CN"\n', encoding="utf-8")
        def gate(_args):
            self.assertEqual(i18n.get_language(), "zh_CN")
            raise cli.GateError(i18n.t("Invalid AUR pkgbase"))
        output = io.StringIO()
        with mock.patch.object(cli, "makepkg_gate", side_effect=gate), contextlib.redirect_stderr(output):
            self.assertEqual(cli.makepkg_main(), 1)
        self.assertIn("[红色]", output.getvalue())
        self.assertIn("非法的 AUR pkgbase", output.getvalue())


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
            english = core.Reviewer(core.ReviewConfig(cache_dir=directory / "cache", language="en"))
            chinese = core.Reviewer(core.ReviewConfig(cache_dir=directory / "cache", language="zh_CN"))
            with mock.patch.object(core.Reviewer, "_run_codex",
                                   side_effect=lambda _: review_response(i18n.get_language())) as run:
                first = english.review(snapshot)
                translated = chinese.review(snapshot)
                cached = english.review(snapshot)
                cached_chinese = chinese.review(snapshot)
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

    def test_codex_prompt_requests_report_language_without_changing_policy(self):
        snapshot = snapshot_fixture(Path("/tmp/demo"))
        for language, name in (("en", "English"), ("zh_CN", "Simplified Chinese")):
            prompt = core.build_prompt(snapshot, language)
            self.assertIn("details should be in " + name, prompt)
            self.assertIn("UNTRUSTED DATA", prompt)
            self.assertIn("Keep JSON property names, level codes", prompt)
            payload = json.loads(prompt.split("UNTRUSTED_PACKAGE_JSON:\n", 1)[1])
            self.assertEqual(payload["files"][0]["content"], snapshot.files["PKGBUILD"])

    def test_error_and_snapshot_findings_follow_report_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            snapshot = dataclasses.replace(snapshot_fixture(directory), issues=(
                "Repository has no .SRCINFO; package metadata cannot be cross-checked",
            ))
            reviewer = core.Reviewer(core.ReviewConfig(cache_dir=directory / "cache", language="zh_CN"))
            with i18n.use_language("en"), mock.patch.object(reviewer, "_run_codex", return_value=review_response("zh_CN")):
                result = reviewer.review(snapshot)
            self.assertIn("仓库缺少 .SRCINFO", result.findings[0])
            with mock.patch.object(reviewer, "_run_codex", side_effect=core.ReviewError("test failure")):
                changed = dataclasses.replace(snapshot, commit="b" * 40)
                result = reviewer.review(changed)
            self.assertEqual(result.level, "red")
            self.assertIn("审查未能可靠完成", result.summary)

    def test_display_and_confirmation_localize_without_weakening_red_gate(self):
        result = core.ReviewResult("white", "example", cached=True, cache_reason="unchanged")
        for language, label, skipped in (("en", "White", "Review skipped"), ("zh_CN", "白色", "跳过 review")):
            output = io.StringIO()
            with i18n.use_language(language), contextlib.redirect_stderr(output), mock.patch("builtins.open") as terminal:
                cli.display("demo", result)
                self.assertFalse(cli.confirm("demo", "red"))
                terminal.assert_not_called()
            self.assertIn("[" + label + "]", output.getvalue())
            self.assertIn(skipped, output.getvalue())


if __name__ == "__main__":
    unittest.main()
