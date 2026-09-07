"""Dependency-free, per-user installation; preserve unrelated yay settings."""

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


BEGIN = "-- BEGIN aur-auto-review (managed)"
END = "-- END aur-auto-review (managed)"


def install(prefix: Path) -> None:
    prefix = prefix.expanduser().absolute()
    source = Path(__file__).resolve().parent
    root = source.parent
    plugin = root / "lua" / "aur-auto-review.lua"
    if not plugin.is_file():
        raise GateError("请在源码目录运行 ./install.sh；wheel 安装请按 README 手动接入 Lua 插件")
    yay = subprocess.run(["yay", "--version"], capture_output=True, text=True, check=True, timeout=10)
    version = re.search(r"yay v(\d+)\.", yay.stdout)
    if not version or int(version[1]) < 13:
        raise GateError("需要 yay >= 13.0.0 的原生 Lua 钩子")
    codex = subprocess.run(["codex", "exec", "--help"], capture_output=True, text=True, check=True, timeout=10)
    for option in ("--output-schema", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
        if option not in codex.stdout:
            raise GateError(f"请更新 Codex CLI；当前版本缺少 {option}")
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not config_root.is_absolute():
        raise GateError("XDG_CONFIG_HOME 必须是绝对路径")
    yay_config = config_root / "yay"
    init = yay_config / "init.lua"
    before = init.read_text(encoding="utf-8") if init.exists() else ""
    if before.count(BEGIN) != before.count(END) or before.count(BEGIN) > 1:
        raise GateError("init.lua 的插件托管标记不完整，请先修复后重试")

    library = prefix / "share" / "aur-auto-review" / "python" / "aur_auto_review"
    binaries = prefix / "bin"
    library.mkdir(parents=True, exist_ok=True)
    binaries.mkdir(parents=True, exist_ok=True)
    yay_config.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.py"):
        shutil.copy2(path, library / path.name)
    for name, function in (("aur-auto-review", "main"), ("aur-auto-review-makepkg", "makepkg_main")):
        # -I excludes the current working directory and PYTHONPATH: neither
        # may inject Python modules from an AUR checkout into the gate itself.
        script = (f"#!{sys.executable} -I\n"
                  f"import sys\nsys.path.insert(0, {str(library.parent)!r})\n"
                  f"from aur_auto_review.cli import {function}\n"
                  f"raise SystemExit({function}())\n")
        dest = binaries / name
        dest.write_text(script, encoding="utf-8")
        dest.chmod(0o755)
    lua = plugin.read_text(encoding="utf-8")
    lua = lua.replace('local command = "aur-auto-review"',
                      "local command = " + json.dumps(str(binaries / "aur-auto-review"), ensure_ascii=False))
    lua = lua.replace('local guard = "aur-auto-review-makepkg"',
                      "local guard = " + json.dumps(str(binaries / "aur-auto-review-makepkg"), ensure_ascii=False))
    plugin_dest = yay_config / "aur-auto-review.lua"
    plugin_dest.write_text(lua, encoding="utf-8")
    block = BEGIN + "\ndofile(" + json.dumps(str(plugin_dest), ensure_ascii=False) + ")\n" + END
    if BEGIN in before:
        after = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, before, flags=re.S)
    else:
        after = before + ("\n" if before and not before.endswith("\n") else "") + block + "\n"
    if before != after:
        if init.exists():
            backup = init.with_name(f"init.lua.bak.{time.time_ns()}")
            shutil.copy2(init, backup)
            print(f"已备份原配置: {backup}")
        init.write_text(after, encoding="utf-8")
    print(f"已安装到 {binaries}，并启用 {plugin_dest}")
    print("现在直接运行 yay -Syu 即可。审查会使用当前 Codex 登录状态。")
