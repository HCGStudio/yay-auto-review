"""yay hooks and the last gate before makepkg is allowed to source a recipe."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

from .core import ReviewConfig, Reviewer, SnapshotError, snapshot_package

SESSION_ENV = "AUR_AUTO_REVIEW_SESSION"
LABELS = {"green": "绿色", "white": "白色", "yellow": "黄色", "red": "红色"}
COLORS = {"green": "32", "white": "37", "yellow": "33", "red": "31"}
NAME = re.compile(r"[a-z0-9][a-z0-9@._+\-]*\Z")
SESSION = re.compile(r"[0-9a-f]{32}\Z")


class GateError(Exception):
    pass


def clean_text(value: object) -> str:
    # Do not let package names/model output inject terminal escape sequences,
    # carriage returns or bidi controls into an approval prompt.
    import unicodedata
    return "".join(c if not unicodedata.category(c).startswith("C") else " "
                   for c in str(value))


def default_cache() -> Path:
    path = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    if not path.is_absolute():
        raise GateError("XDG_CACHE_HOME 必须是绝对路径")
    return path / "aur-auto-review"


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise GateError(f"不安全的缓存目录: {path}")
    path.chmod(0o700)
    return path


def load_config() -> tuple[ReviewConfig, str]:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not root.is_absolute():
        raise GateError("XDG_CONFIG_HOME 必须是绝对路径")
    path = root / "aur-auto-review" / "config.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    allowed = {"codex", "model", "timeout_seconds", "cache_dir", "makepkg"}
    if data.keys() - allowed:
        raise GateError("配置含有未知字段: " + ", ".join(sorted(data.keys() - allowed)))
    for key in ("codex", "model", "cache_dir", "makepkg"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise GateError(f"配置 {key} 必须为非空字符串")
    timeout = data.get("timeout_seconds", 300)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise GateError("timeout_seconds 必须为 1–3600 秒")
    cache = Path(data.get("cache_dir", str(default_cache()))).expanduser()
    if not cache.is_absolute():
        raise GateError("cache_dir 必须是绝对路径")
    return ReviewConfig(codex_command=(data.get("codex", "codex"),),
                        model=data.get("model"), timeout_seconds=timeout,
                        cache_dir=cache), data.get("makepkg", "/usr/bin/makepkg")


def session_id() -> str:
    session = os.environ.get(SESSION_ENV, "")
    if not SESSION.fullmatch(session):
        raise GateError("缺少有效的 yay 审查会话；请通过已启用插件的 yay 安装")
    return session


def session_directory(session: str) -> Path:
    return default_cache() / "builds" / session


def new_session() -> str:
    private_dir(default_cache())
    private_dir(default_cache() / "builds")
    session = secrets.token_hex(16)
    session_directory(session).mkdir(mode=0o700)
    return session


def check_directory(directory: Path, pkgbase: str, session: str) -> Path:
    if not NAME.fullmatch(pkgbase):
        raise GateError("非法的 AUR pkgbase")
    expected = session_directory(session) / pkgbase
    if directory.is_symlink() or directory.resolve() != expected.absolute():
        raise GateError("构建目录被覆盖或重定向；插件要求使用本次会话的独立构建目录")
    if expected.parent.is_symlink() or not expected.parent.is_dir():
        raise GateError("审查会话目录不存在或不安全")
    return directory.resolve()


def reject_yay_overrides() -> None:
    """Lua options apply before CLI flags; reject a command-line guard bypass.

    Linux /proc is available on the supported Arch Linux platform. Walk through
    io.popen/os.execute's shell to the actual yay parent. Other ancestors are not
    trusted as sources of yay flags (e.g. a terminal shell's command string).
    """
    pid = os.getppid()
    for _ in range(12):
        try:
            proc = Path(f"/proc/{pid}")
            args = proc.joinpath("cmdline").read_bytes().split(b"\0")
            exe = proc.joinpath("exe").resolve().name
            if exe in {"yay", "yay-bin", "yay-git"} or (args and Path(os.fsdecode(args[0])).name in {"yay", "yay-bin", "yay-git"}):
                for raw in args[1:]:
                    arg = os.fsdecode(raw)
                    if arg == "--":
                        break
                    if arg.split("=", 1)[0] in {"--makepkg", "--builddir", "--mflags", "--save"}:
                        raise GateError(f"插件启用时不允许 {arg.split('=', 1)[0]}；它会覆盖审查隔离设置")
                return
            stat = proc.joinpath("stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
            if pid <= 1:
                return
        except (OSError, ValueError):
            raise GateError("无法检查 yay 启动参数，拒绝在未知配置下安装") from None


def remote_head(pkgbase: str) -> str:
    if not NAME.fullmatch(pkgbase):
        raise GateError("非法的 AUR pkgbase")
    # Ignore checkout-local and user URL rewriting/credential helpers. Never use
    # the untrusted repository's configured origin as proof of AUR freshness.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "protocol.allow=never",
             "-c", "protocol.https.allow=always", "ls-remote", "--exit-code",
             f"https://aur.archlinux.org/{pkgbase}.git", "HEAD"],
            cwd="/", env=env, capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        raise GateError("无法向 AUR 核实最新提交；本次不使用缓存，也不继续安装") from None
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != "HEAD" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
        raise GateError("AUR 返回的 Git HEAD 无效")
    return fields[0]


def display(pkgbase: str, result) -> None:
    label = LABELS[result.level]
    if sys.stderr.isatty() and "NO_COLOR" not in os.environ:
        label = f"\033[{COLORS[result.level]}m{label}\033[0m"
    print(f"\n[{label}] {clean_text(pkgbase)} — {clean_text(result.summary)}", file=sys.stderr)
    if result.cached:
        print(f"  跳过 review: {clean_text(pkgbase)}；{clean_text(result.cache_reason)}", file=sys.stderr)
    for finding in result.findings:
        print(f"  • {clean_text(finding)}", file=sys.stderr)
    for evidence in result.evidence:
        print(f"  依据: {clean_text(evidence.detail)}", file=sys.stderr)
        for ref in evidence.references:
            print(f"    {clean_text(ref)}", file=sys.stderr)


def confirm(pkgbase: str, level: str) -> bool:
    if level == "red":
        print("红色结果：已阻止本次构建和安装。修复问题后重新 review。", file=sys.stderr)
        return False
    try:
        # yay may pipe stdin or use --noconfirm. Neither is consent to bypass
        # this gate. Always read the human decision from the controlling TTY.
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as tty:
            if level == "yellow":
                tty.write(f"存在其他问题，输入包名 {clean_text(pkgbase)} 才继续，回车取消: ")
                return tty.readline().strip() == pkgbase
            tty.write(f"允许构建并安装 {clean_text(pkgbase)}? [y/N] ")
            return tty.readline().strip().lower() in {"y", "yes"}
    except OSError:
        print("无交互终端，无法确认审查结果；已阻止安装。", file=sys.stderr)
        return False


def receipt_path(directory: Path, session: str) -> Path:
    root = private_dir(default_cache() / "receipts" / session)
    key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
    return root / f"{key}.json"


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_receipt(snapshot, session: str, context: dict, started: bool = False) -> None:
    atomic_json(receipt_path(snapshot.directory, session), {
        "session": session, "pkgbase": snapshot.pkgbase,
        "directory": str(snapshot.directory), "commit": snapshot.commit,
        "digest": snapshot.digest, "paths": sorted(snapshot.files),
        "context": context, "approved_at": time.time(), "started": started,
    })


def review_and_confirm(snapshot, config: ReviewConfig, context: dict):
    config = dataclasses.replace(config, cache_context=json.dumps(context, sort_keys=True))
    print(f"正在审查 {clean_text(snapshot.pkgbase)} ({snapshot.commit[:12]})…", file=sys.stderr, flush=True)
    result = Reviewer(config).review(snapshot)
    display(snapshot.pkgbase, result)
    if not confirm(snapshot.pkgbase, result.level):
        raise GateError("未获用户确认，已取消安装")
    return result


def hook(args) -> int:
    session = session_id()
    reject_yay_overrides()
    directory = check_directory(Path(args.directory), args.pkgbase, session)
    config, _ = load_config()
    context = {"last_modified": args.last_modified, "version": args.version}
    snapshot = snapshot_package(directory, args.pkgbase)
    head = remote_head(args.pkgbase)
    if snapshot.commit != head:
        raise GateError("本地提交不是 AUR 最新提交；请重新运行 yay 下载更新后的内容")
    review_and_confirm(snapshot, config, context)
    # Codex can take minutes; detect changes during review/confirmation too.
    after = snapshot_package(directory, args.pkgbase)
    if after.digest != snapshot.digest or after.commit != snapshot.commit:
        raise GateError("审查或确认期间文件发生变化，请重新运行 yay")
    write_receipt(snapshot, session, context)
    return 0


def read_receipt(directory: Path, session: str) -> dict:
    path = receipt_path(directory, session)
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise GateError("审查确认记录不安全")
        data = json.loads(path.read_text())
        if (data["session"] != session or data["directory"] != str(directory)
                or not NAME.fullmatch(data["pkgbase"])
                or not isinstance(data["paths"], list)
                or not data["paths"] or not all(isinstance(p, str) for p in data["paths"])
                or type(data["started"]) is not bool
                or not isinstance(data["context"], dict)):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise GateError("没有本次会话的有效用户确认，拒绝执行 makepkg") from None


def tracked_paths(directory: Path) -> set[str]:
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "ls-files", "-z"],
                            cwd=directory, capture_output=True, timeout=15, check=True)
    return {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}


def makepkg_gate(argv: list[str]) -> int:
    session = session_id()
    reject_yay_overrides()
    directory = Path.cwd().resolve()
    receipt = read_receipt(directory, session)
    check_directory(directory, receipt["pkgbase"], session)
    # A different PKGBUILD/config path would fall outside the approved files.
    # makepkg's --config is trusted user config and cannot be supplied by AUR,
    # but alternate recipes (-p/--pkgbuild) must be refused, including clusters.
    for arg in argv:
        if arg == "--":
            break
        if (arg.startswith("--pkgbuild") or arg.split("=", 1)[0] in {"--d", "--di", "--dir"}
                or (arg.startswith("-") and not arg.startswith("--") and any(c in arg[1:] for c in "pD"))):
            raise GateError("不允许通过 makepkg -p/-D/--dir 改用未审查脚本或目录")
    config, makepkg = load_config()
    paths = set(receipt["paths"]) | tracked_paths(directory) if receipt["started"] else None
    snapshot = snapshot_package(directory, receipt["pkgbase"], paths=paths)
    if snapshot.commit != receipt["commit"] or snapshot.digest != receipt["digest"]:
        print("审查后的包装文件已改变，执行 makepkg 前重新 review 并确认。", file=sys.stderr)
        if snapshot.commit != remote_head(snapshot.pkgbase):
            raise GateError("AUR 提交已改变，请重新运行 yay")
        review_and_confirm(snapshot, config, receipt["context"])
        again = snapshot_package(directory, snapshot.pkgbase, paths=paths)
        if again.digest != snapshot.digest or again.commit != snapshot.commit:
            raise GateError("重新审查期间文件发生变化，已停止")
    write_receipt(snapshot, session, receipt["context"], started=True)
    executable = shutil.which(makepkg)
    if not executable or Path(executable).resolve() == Path(sys.argv[0]).resolve() or Path(executable).name == "aur-auto-review-makepkg":
        raise GateError("makepkg 配置无效或递归指向审查插件")
    # makepkg applies these variable assignments AFTER makepkg.conf and any
    # user assignments. Keep both downloads and archives in this transaction:
    # a global PKGDEST must not make yay reuse binaries from an older recipe.
    locations = [f"{name}={directory}" for name in
                 ("PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST", "BUILDDIR")]
    os.execv(executable, [executable, *argv, *locations, "BUILDFILE=PKGBUILD"])
    return 0  # pragma: no cover (execv does not return)


def makepkg_main() -> int:
    return run_safely(lambda: makepkg_gate(sys.argv[1:]))


def run_safely(action) -> int:
    try:
        return action()
    except KeyboardInterrupt:
        print("\n审查已取消，停止安装。", file=sys.stderr)
        return 130
    except (GateError, SnapshotError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[红色] {clean_text(exc)}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在 yay 构建 AUR 软件包前调用 Codex 审查")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("session", help="为 yay 创建独立审查会话（内部接口）")
    gate = sub.add_parser("hook", help="yay AURPreInstall 钩子（内部接口）")
    gate.add_argument("--pkgbase", required=True)
    gate.add_argument("--directory", required=True)
    gate.add_argument("--last-modified", type=int, required=True)
    gate.add_argument("--version", required=True)
    review = sub.add_parser("review", help="只审查本地 AUR Git 仓库，不安装")
    review.add_argument("directory", type=Path)
    review.add_argument("--pkgbase", required=True)
    review.add_argument("--json", action="store_true", help="输出结构化结果")
    installer = sub.add_parser("install", help="安装当前源码和 yay Lua 插件到用户目录")
    installer.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    args = parser.parse_args(argv)

    def dispatch() -> int:
        if args.command == "session":
            print(new_session())
            return 0
        if args.command == "hook":
            return hook(args)
        if args.command == "install":
            from .installer import install
            install(args.prefix)
            return 0
        config, _ = load_config()
        snapshot = snapshot_package(args.directory, args.pkgbase)
        if snapshot.commit != remote_head(args.pkgbase):
            raise GateError("本地提交不是 AUR 最新提交，请先更新仓库")
        result = Reviewer(config).review(snapshot)
        if args.json:
            print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
        else:
            display(args.pkgbase, result)
        return 1 if result.level == "red" else 0

    return run_safely(dispatch)
