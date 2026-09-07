"""Exercise the native yay boundary without downloading or installing packages."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "lua" / "yay-auto-review.lua"
LUA = shutil.which("lua5.1") or shutil.which("lua")


def lua_string(value):
    # JSON strings are compatible here with Lua strings except for \u escapes.
    return json.dumps(str(value), ensure_ascii=False)


@unittest.skipUnless(LUA, "Lua interpreter is not installed")
class LuaHookTests(unittest.TestCase):
    def run_hook(self, body, *, env=None, setup=""):
        harness = """
            local callback
            local runtime_env = {}
            local real_getenv = os.getenv
            os.getenv = function(key) return runtime_env[key] or real_getenv(key) end
            os.setenv = function(key, value) runtime_env[key] = value; return true end
            io.popen = function(command, mode)
                assert(command == "'yay-auto-review' 'session'")
                assert(mode == 'r')
                return {
                    read = function() return string.rep('a', 32) .. '\\n' end,
                    close = function() return true end,
                }
            end
            yay = {
                opt = {},
                abort = function(message) error('ABORT: ' .. message) end,
                create_autocmd = function(name, options)
                    assert(name == 'AURPreInstall')
                    assert(callback == nil)
                    callback = options.callback
                end,
            }
        """ + setup + "\ndofile(" + lua_string(PLUGIN) + ")\n" + """
            assert(yay.opt.makepkg_bin == 'yay-auto-review-makepkg')
            assert(yay.opt.redownload == 'all')
            assert(os.getenv('YAY_AUTO_REVIEW_SESSION') == string.rep('a', 32))
            assert(yay.opt.build_dir:match('/yay%-auto%-review/builds/' .. string.rep('a', 32) .. '$'))
            local event = {
                match = 'example',
                data = {base = 'example', dir = '/tmp/example',
                        version = '1:2.0-1', last_modified = 12345},
            }
        """ + body
        return subprocess.run(
            [LUA, "-"], input=harness, text=True, errors="replace", capture_output=True,
            env=dict(os.environ, LANG="en_US.UTF-8") if env is None else env, timeout=10,
        )

    def test_approvals_accept_only_explicit_success(self):
        for result in ('0', 'true, "exit", 0'):
            with self.subTest(result=result):
                process = self.run_hook(
                    "os.execute = function(_) return " + result + " end\ncallback(event)"
                )
                self.assertEqual(process.returncode, 0, process.stderr)

    def test_failures_cannot_fall_through(self):
        for result in ('1', '256', 'nil', 'false', 'true', 'true, "exit", 1',
                       'nil, "signal", 9', '"0"'):
            with self.subTest(result=result):
                process = self.run_hook(
                    "os.execute = function(_) return " + result + " end\ncallback(event)"
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("ABORT:", process.stderr)

    def test_event_values_are_passed_as_literal_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "argv.json"
            marker = temp_path / "injected"
            executable = temp_path / "yay-auto-review"
            executable.write_text(
                "#!/usr/bin/python3\nimport json, os, sys\n"
                "open(os.environ['TEST_ARGV_LOG'], 'w').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            directory = "/tmp/审阅 'quoted'; touch " + str(marker) + "; $(false) `false`\nend"
            version = "1:2'\"; $(false) `false`\\-1"
            env = dict(os.environ, PATH=str(temp_path) + os.pathsep + os.environ["PATH"],
                       TEST_ARGV_LOG=str(log))
            process = self.run_hook(
                "event.data.dir = " + lua_string(directory) + "\n"
                "event.data.version = " + lua_string(version) + "\ncallback(event)",
                env=env,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(log.read_text()), [
                "hook", "--pkgbase", "example", "--directory", directory,
                "--last-modified", "12345", "--version", version,
            ])

    def test_missing_reviewer_aborts(self):
        with tempfile.TemporaryDirectory() as temp:
            process = self.run_hook("callback(event)", env=dict(os.environ, PATH=temp))
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("ABORT:", process.stderr)

    def test_lua_uses_only_startup_lang_with_english_fallback(self):
        cases = [
            ({}, "missing AURPreInstall"),
            ({"LANG": ""}, "missing AURPreInstall"),
            ({"LANG": "C"}, "missing AURPreInstall"),
            ({"LANG": "POSIX"}, "missing AURPreInstall"),
            ({"LANG": "fr_FR.UTF-8"}, "missing AURPreInstall"),
            ({"LANG": "zh_CN.UTF-8", "LC_ALL": "C", "YAY_AUTO_REVIEW_LANG": "en"}, "缺少 AURPreInstall"),
            ({"LANG": "zh-Hans-CN.UTF-8@variant"}, "缺少 AURPreInstall"),
            ({"LANG": "zh_SG"}, "缺少 AURPreInstall"),
            ({"LANG": "en", "LC_ALL": "zh_CN", "LC_MESSAGES": "zh_CN", "YAY_AUTO_REVIEW_LANG": "zh_CN"}, "missing AURPreInstall"),
            ({"LC_ALL": "zh_CN", "LC_MESSAGES": "zh_CN", "YAY_AUTO_REVIEW_LANG": "zh_CN"}, "missing AURPreInstall"),
        ]
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"LANG", "LC_ALL", "LC_MESSAGES", "YAY_AUTO_REVIEW_LANG"}}
        for variables, message in cases:
            with self.subTest(variables=variables):
                process = self.run_hook("callback(nil)", env=dict(environment, **variables))
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(message, process.stderr)

    def test_lua_reads_lang_once_and_ignores_later_environment_changes(self):
        setup = """
            local getenv = os.getenv
            local lang_reads = 0
            os.getenv = function(key)
                if key == 'LANG' then
                    lang_reads = lang_reads + 1
                    assert(lang_reads == 1, 'LANG WAS READ AGAIN')
                end
                return getenv(key)
            end
        """
        for initial, later, message in (("en", "zh_CN", "missing AURPreInstall"),
                                        ("zh_CN", "en", "缺少 AURPreInstall")):
            with self.subTest(initial=initial):
                process = self.run_hook(
                    "assert(lang_reads == 1)\nos.setenv('LANG', " + lua_string(later) + ")\ncallback(nil)",
                    env=dict(os.environ, LANG=initial), setup=setup,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(message, process.stderr)
                self.assertNotIn("LANG WAS READ AGAIN", process.stderr)

    def test_lua_ignores_legacy_session_language_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "yay-auto-review" / "builds" / ("a" * 32)
            directory.mkdir(parents=True)
            (directory / ".language").write_text("zh_CN\n", encoding="ascii")
            process = self.run_hook("callback(nil)", env=dict(
                os.environ, LANG="en", XDG_CACHE_HOME=temporary,
            ))
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("missing AURPreInstall", process.stderr)

    def test_corrupt_metadata_aborts_before_shell(self):
        cases = [
            "event = nil", "event.data = nil", "event.match = 'different'",
            "event.data.dir = 'relative/path'", "event.data.base = ''",
            "event.data.version = 'bad' .. string.char(0) .. 'value'",
            "event.data.last_modified = -1", "event.data.last_modified = 0/0",
            "event.data.last_modified = math.huge", "event.data.last_modified = 1.1",
            "event.data.last_modified = '12345'",
            "yay.opt.makepkg_bin = 'makepkg'", "yay.opt.redownload = 'no'",
            "yay.opt.build_dir = '/tmp/unsafe'",
            "os.setenv('YAY_AUTO_REVIEW_SESSION', 'other')",
        ]
        for mutation in cases:
            with self.subTest(mutation=mutation):
                process = self.run_hook(
                    "os.execute = function(_) error('SHELL WAS CALLED') end\n"
                    + mutation + "\ncallback(event)"
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("ABORT:", process.stderr)
                self.assertNotIn("SHELL WAS CALLED", process.stderr)

    def test_session_setup_fails_closed(self):
        cases = [
            "os.setenv = nil",
            "os.setenv = function() return nil end",
            "io.popen = function() return nil end",
            "io.popen = function() return {read=function() return 'bad' end, close=function() return true end} end",
            "io.popen = function() return {read=function() return string.rep('a',32) end, close=function() return nil,'exit',1 end} end",
        ]
        for setup in cases:
            with self.subTest(setup=setup):
                process = self.run_hook("", setup=setup)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("ABORT:", process.stderr)

    def test_unsafe_cli_overrides_stop_before_session_creation(self):
        for argument in ("--save", "--makepkg", "--makepkg=/tmp/other",
                         "--builddir=/tmp/old", "--mflags=-pother"):
            with self.subTest(argument=argument):
                setup = """
                    io.open = function(path, mode)
                        assert(path == '/proc/self/cmdline' and mode == 'rb')
                        return {
                            read = function()
                                return table.concat({'renamed-yay-binary', '-Pg', ARGUMENT}, string.char(0)) .. string.char(0)
                            end,
                            close = function() return true end,
                        }
                    end
                    io.popen = function() error('SESSION WAS CREATED') end
                """.replace("ARGUMENT", lua_string(argument))
                process = self.run_hook("", setup=setup)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("unsupported override", process.stderr)
                self.assertNotIn("SESSION WAS CREATED", process.stderr)

    def test_missing_proc_arguments_fail_closed_before_session(self):
        for setup in ("io.open = function() return nil end",
                      "io.open = function() return {read=function() return '' end, close=function() end} end"):
            with self.subTest(setup=setup):
                process = self.run_hook("", setup=setup +
                                        "\nio.popen = function() error('SESSION WAS CREATED') end")
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("command-line overrides", process.stderr)
                self.assertNotIn("SESSION WAS CREATED", process.stderr)


@unittest.skipUnless(shutil.which("yay"), "yay is not installed")
class NativeYayConfigTests(unittest.TestCase):
    def test_installed_yay_accepts_native_plugin_without_installation(self):
        version = subprocess.run(
            ["yay", "--version"], text=True, capture_output=True, timeout=10,
        )
        if not version.stdout.startswith("yay v13."):
            self.skipTest("this smoke test requires yay v13")
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "yay"
            config.mkdir()
            binary_dir = Path(temp) / "bin"
            binary_dir.mkdir()
            helper = binary_dir / "yay-auto-review"
            helper.write_text(
                "#!/bin/sh\n[ \"$1\" = session ] || exit 1\n"
                "mkdir -p -- \"$XDG_CACHE_HOME/yay-auto-review/builds/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\n"
                "printf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8",
            )
            helper.chmod(0o755)
            (config / "init.lua").write_text(
                "dofile(" + lua_string(PLUGIN) + ")\n", encoding="utf-8",
            )
            result = subprocess.run(
                ["yay", "-Pg"], text=True, capture_output=True, timeout=10,
                env=dict(os.environ, LANG="en_US.UTF-8", XDG_CONFIG_HOME=temp, XDG_CACHE_HOME=temp,
                         PATH=str(binary_dir) + os.pathsep + os.environ["PATH"]),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(result.stdout)
            self.assertEqual(settings["makepkgbin"], "yay-auto-review-makepkg")
            self.assertEqual(settings["redownload"], "all")
            self.assertEqual(settings["buildDir"],
                             temp + "/yay-auto-review/builds/" + "a" * 32)

    def test_save_and_overrides_abort_without_changing_existing_config(self):
        version = subprocess.run(["yay", "--version"], text=True, capture_output=True, timeout=10)
        if not version.stdout.startswith("yay v13."):
            self.skipTest("this regression test requires yay v13")
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "yay"
            config.mkdir()
            settings = config / "config.json"
            original = json.dumps({"editor": "nano", "makepkgbin": "makepkg",
                                   "buildDir": str(Path(temp) / "original-build-dir")}, indent=2) + "\n"
            settings.write_text(original, encoding="utf-8")
            init = config / "init.lua"
            init.write_text("dofile(" + lua_string(PLUGIN) + ")\n", encoding="utf-8")
            original_init = init.read_bytes()
            for argument in ("--save", "--makepkg=/tmp/other", "--builddir=/tmp/old",
                             "--mflags=-pother"):
                with self.subTest(argument=argument):
                    result = subprocess.run(
                        ["yay", "-Pg", argument], text=True, capture_output=True, timeout=10,
                        env=dict(os.environ, LANG="en_US.UTF-8", XDG_CONFIG_HOME=temp, XDG_CACHE_HOME=temp),
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unsupported override", result.stderr)
                    self.assertEqual(settings.read_text(encoding="utf-8"), original)
                    self.assertEqual(init.read_bytes(), original_init)
                    self.assertFalse((Path(temp) / "yay-auto-review" / "builds").exists())


if __name__ == "__main__":
    unittest.main()
