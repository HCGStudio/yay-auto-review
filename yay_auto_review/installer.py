"""Per-user activation for both source installs and distribution packages."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .cli import GateError
from .i18n import t

BEGIN = "-- BEGIN yay-auto-review (managed)"
END = "-- END yay-auto-review (managed)"


def _check_runtime() -> None:
    yay = subprocess.run(["yay", "--version"], capture_output=True, text=True, check=True, timeout=10)
    version = re.search(r"yay v(\d+)\.", yay.stdout)
    if not version or int(version[1]) < 13:
        raise GateError(t("yay >= 13.0.0 with native Lua hooks is required"))
    codex = subprocess.run(["codex", "exec", "--help"], capture_output=True, text=True, check=True, timeout=10)
    for option in ("--output-schema", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
        if option not in codex.stdout:
            raise GateError(t("Please update Codex CLI; this version lacks {option}", option=option))


def _configuration() -> tuple[Path, str]:
    if os.geteuid() == 0:
        raise GateError(t("Run activation as your regular user, without sudo"))
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not config_root.is_absolute():
        raise GateError(t("XDG_CONFIG_HOME must be an absolute path"))
    init = config_root / "yay" / "init.lua"
    before = init.read_text(encoding="utf-8") if init.exists() else ""
    if before.count(BEGIN) != before.count(END) or before.count(BEGIN) > 1:
        raise GateError(t("The managed markers in init.lua are incomplete; repair them before retrying"))
    return init, before


def _write_configuration(init: Path, before: str, after: str) -> None:
    if before == after:
        return
    if init.exists():
        backup = init.with_name(f"init.lua.bak.{time.time_ns()}")
        shutil.copy2(init, backup)
        print(t("Previous configuration backed up to: {path}", path=backup))
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(after, encoding="utf-8")


def _enable(init: Path, before: str, shared_plugin: Path) -> None:
    init.parent.mkdir(parents=True, exist_ok=True)
    plugin_dest = init.parent / "yay-auto-review.lua"
    # Keep only a loader in the home directory, so package upgrades update Lua.
    plugin_dest.write_text(
        "-- Managed loader; the shared plugin is updated by your package manager.\n"
        "dofile(" + json.dumps(str(shared_plugin), ensure_ascii=False) + ")\n",
        encoding="utf-8",
    )
    block = BEGIN + "\ndofile(" + json.dumps(str(plugin_dest), ensure_ascii=False) + ")\n" + END
    if BEGIN in before:
        after = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, before, flags=re.S)
    else:
        after = before + ("\n" if before and not before.endswith("\n") else "") + block + "\n"
    _write_configuration(init, before, after)
    print(t("Review is enabled in {path}", path=init))
    print(t("Run yay -Syu as usual. Reviews use your current Codex login."))


def enable(prefix: Path | None = None) -> None:
    """Activate an existing system/wheel/source installation for this user."""
    source = Path(__file__).resolve().parent
    if prefix is None:
        prefix = (source.parents[3]
                  if source.parent.name == "python" and source.parent.parent.name == "yay-auto-review"
                  and source.parents[2].name == "share" else Path(sys.prefix))
    prefix = prefix.expanduser().absolute()
    plugin = prefix / "share" / "yay-auto-review" / "yay-auto-review.lua"
    if not plugin.is_file():
        raise GateError(t("Shared Lua plugin not found at {path}; install yay-auto-review first", path=plugin))
    for name in ("yay-auto-review", "yay-auto-review-makepkg"):
        if not os.access(prefix / "bin" / name, os.X_OK):
            raise GateError(t("Required launcher is missing: {path}", path=prefix / "bin" / name))
    _check_runtime()
    init, before = _configuration()
    _enable(init, before, plugin)


def disable() -> None:
    """Remove only our activation stanza; preserve all other yay settings."""
    init, before = _configuration()
    if BEGIN not in before:
        print(t("Review is already disabled; no managed activation stanza was found"))
        return
    after = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", before, flags=re.S)
    _write_configuration(init, before, after)
    print(t("Review is disabled. Other yay settings and review history were preserved."))


def install(prefix: Path) -> None:
    prefix = prefix.expanduser().absolute()
    source = Path(__file__).resolve().parent
    root = source.parent
    plugin = root / "lua" / "yay-auto-review.lua"
    if not plugin.is_file():
        raise GateError(t("Run ./install.sh from the source checkout; for a packaged install, run yay-auto-review enable"))
    _check_runtime()
    init, before = _configuration()
    shared = prefix / "share" / "yay-auto-review"
    library = shared / "python" / "yay_auto_review"
    binaries = prefix / "bin"
    library.mkdir(parents=True, exist_ok=True)
    binaries.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.py"):
        shutil.copy2(path, library / path.name)
    shutil.copytree(source / "locales", library / "locales", dirs_exist_ok=True)
    for name, function in (("yay-auto-review", "main"),
                           ("yay-auto-review-makepkg", "makepkg_main")):
        # -I excludes the checkout and PYTHONPATH from module discovery.
        script = (f"#!{sys.executable} -I\n"
                  f"import sys\nsys.path.insert(0, {str(library.parent)!r})\n"
                  f"from yay_auto_review.cli import {function}\n"
                  f"raise SystemExit({function}())\n")
        dest = binaries / name
        dest.write_text(script, encoding="utf-8")
        dest.chmod(0o755)
    lua = plugin.read_text(encoding="utf-8")
    lua = lua.replace('local command = "yay-auto-review"',
                      "local command = " + json.dumps(str(binaries / "yay-auto-review"), ensure_ascii=False))
    lua = lua.replace('local guard = "yay-auto-review-makepkg"',
                      "local guard = " + json.dumps(str(binaries / "yay-auto-review-makepkg"), ensure_ascii=False))
    plugin_dest = shared / "yay-auto-review.lua"
    plugin_dest.write_text(lua, encoding="utf-8")
    shutil.copy2(root / "config.example.toml", shared / "config.example.toml")
    print(t("Programs installed to {path}", path=binaries))
    _enable(init, before, plugin_dest)
