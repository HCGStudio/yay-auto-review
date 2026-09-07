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
from .i18n import localized_argparse, t

SESSION_ENV = "YAY_AUTO_REVIEW_SESSION"
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
        raise GateError(t('XDG_CACHE_HOME must be an absolute path'))
    return path / "yay-auto-review"


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise GateError(t('Unsafe cache directory: {}', path))
    path.chmod(0o700)
    return path


def config_data() -> dict:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not root.is_absolute():
        raise GateError(t('XDG_CONFIG_HOME must be an absolute path'))
    path = root / "yay-auto-review" / "config.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise GateError(t("Cannot parse configuration {}; check TOML syntax", path)) from exc
    except (OSError, UnicodeError) as exc:
        raise GateError(t("Cannot read UTF-8 configuration {}", path)) from exc


def load_config() -> tuple[ReviewConfig, str]:
    data = config_data()
    # Ignore the obsolete setting in existing configs; only startup LANG
    # selects the language, so upgrades do not require manual config cleanup.
    data.pop("language", None)
    allowed = {"codex", "model", "timeout_seconds", "cache_dir", "makepkg"}
    if data.keys() - allowed:
        raise GateError(t('Unknown configuration fields: ') + ", ".join(sorted(data.keys() - allowed)))
    for key in ("codex", "model", "cache_dir", "makepkg"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise GateError(t('Configuration {} must be a nonempty string', key))
    timeout = data.get("timeout_seconds", 300)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise GateError(t('timeout_seconds must be between 1 and 3600 seconds'))
    cache = Path(data.get("cache_dir", str(default_cache()))).expanduser()
    if not cache.is_absolute():
        raise GateError(t('cache_dir must be an absolute path'))
    return ReviewConfig(codex_command=(data.get("codex", "codex"),),
                        model=data.get("model"), timeout_seconds=timeout,
                        cache_dir=cache), data.get("makepkg", "/usr/bin/makepkg")


def session_id() -> str:
    session = os.environ.get(SESSION_ENV, "")
    if not SESSION.fullmatch(session):
        raise GateError(t('No valid yay review session; install through yay with the plugin enabled'))
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
        raise GateError(t('Invalid AUR pkgbase'))
    expected = session_directory(session) / pkgbase
    if directory.is_symlink() or directory.resolve() != expected.absolute():
        raise GateError(t("Build directory was overridden or redirected; the plugin requires this session's isolated build directory"))
    if expected.parent.is_symlink() or not expected.parent.is_dir():
        raise GateError(t('Review session directory is missing or unsafe'))
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
                        raise GateError(t('{} is not allowed while the plugin is enabled; it would override review isolation', arg.split('=', 1)[0]))
                return
            stat = proc.joinpath("stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
            if pid <= 1:
                return
        except (OSError, ValueError):
            raise GateError(t('Cannot inspect yay arguments; refusing installation with unknown configuration')) from None


def remote_head(pkgbase: str) -> str:
    if not NAME.fullmatch(pkgbase):
        raise GateError(t('Invalid AUR pkgbase'))
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
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise GateError(t('Cannot verify the latest commit with AUR; cached review and installation are blocked')) from None
    # AUR can advertise the same HEAD more than once. Require every record to
    # be a valid HEAD and every advertised commit to agree before trusting it.
    heads = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or fields[1] != "HEAD" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
            raise GateError(t('AUR returned an invalid Git HEAD'))
        heads.add(fields[0])
    if len(heads) != 1:
        raise GateError(t('AUR returned an invalid Git HEAD'))
    return heads.pop()


def status_dot(level: str) -> str:
    """Color only the status marker; redirected output stays escape-free."""
    dot = "●"
    if sys.stderr.isatty() and "NO_COLOR" not in os.environ:
        dot = f"\033[{COLORS[level]}m{dot}\033[0m"
    return dot


def display(pkgbase: str, result) -> None:
    print(f"\n{status_dot(result.level)} {clean_text(pkgbase)} — {clean_text(result.summary)}", file=sys.stderr)
    if result.cached:
        print(t('  Review skipped: {}; {}', clean_text(pkgbase), clean_text(result.cache_reason)), file=sys.stderr)
    for finding in result.findings:
        print(f"  • {clean_text(finding)}", file=sys.stderr)
    for evidence in result.evidence:
        print(t('  Evidence: {}', clean_text(evidence.detail)), file=sys.stderr)
        for ref in evidence.references:
            print(f"    {clean_text(ref)}", file=sys.stderr)


def confirm(pkgbase: str, level: str) -> bool:
    if level == "red":
        print(t('This build and installation are blocked. Fix the issues and review again.'), file=sys.stderr)
        return False
    try:
        # yay may pipe stdin or use --noconfirm. Neither is consent to bypass
        # this gate. Always read the human decision from the controlling TTY.
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as tty:
            if level == "yellow":
                tty.write(t('Other issues remain. Type the package name {} to continue, or press Enter to cancel: ', clean_text(pkgbase)))
                return tty.readline().strip() == pkgbase
            tty.write(t('Allow building and installing {}? [y/N] ', clean_text(pkgbase)))
            return tty.readline().strip().lower() in {"y", "yes"}
    except OSError:
        print(t('No interactive terminal is available to confirm the review; installation is blocked.'), file=sys.stderr)
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
    print(t('Reviewing {} ({})…', clean_text(snapshot.pkgbase), snapshot.commit[:12]), file=sys.stderr, flush=True)
    result = Reviewer(config).review(snapshot)
    display(snapshot.pkgbase, result)
    if not confirm(snapshot.pkgbase, result.level):
        raise GateError(t('User confirmation was not granted; installation cancelled'))
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
        raise GateError(t('Local commit is not the latest AUR commit; rerun yay to download the updated files'))
    review_and_confirm(snapshot, config, context)
    # Codex can take minutes; detect changes during review/confirmation too.
    after = snapshot_package(directory, args.pkgbase)
    if after.digest != snapshot.digest or after.commit != snapshot.commit:
        raise GateError(t('Files changed during review or confirmation; rerun yay'))
    write_receipt(snapshot, session, context)
    return 0


def read_receipt(directory: Path, session: str) -> dict:
    path = receipt_path(directory, session)
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise GateError(t('Unsafe review approval receipt'))
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
        raise GateError(t('No valid user approval for this session; refusing to execute makepkg')) from None


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
            raise GateError(t('makepkg -p/-D/--dir cannot select an unreviewed script or directory'))
    config, makepkg = load_config()
    paths = set(receipt["paths"]) | tracked_paths(directory) if receipt["started"] else None
    snapshot = snapshot_package(directory, receipt["pkgbase"], paths=paths)
    if snapshot.commit != receipt["commit"] or snapshot.digest != receipt["digest"]:
        print(t('Packaging files changed after review; reviewing and confirming again before makepkg.'), file=sys.stderr)
        if snapshot.commit != remote_head(snapshot.pkgbase):
            raise GateError(t('AUR commit changed; rerun yay'))
        review_and_confirm(snapshot, config, receipt["context"])
        again = snapshot_package(directory, snapshot.pkgbase, paths=paths)
        if again.digest != snapshot.digest or again.commit != snapshot.commit:
            raise GateError(t('Files changed during the repeated review; stopped'))
    write_receipt(snapshot, session, receipt["context"], started=True)
    executable = shutil.which(makepkg)
    if not executable or Path(executable).resolve() == Path(sys.argv[0]).resolve() or Path(executable).name == "yay-auto-review-makepkg":
        raise GateError(t('Invalid makepkg configuration or recursive reference to the review plugin'))
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
        print(t('\nReview cancelled; installation stopped.'), file=sys.stderr)
        return 130
    except (GateError, SnapshotError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{status_dot('red')} {clean_text(exc)}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    return run_safely(lambda: _main(argv))


def _main(argv: list[str] | None = None) -> int:
    with localized_argparse():
        return _parse_and_dispatch(argv)


def _parse_and_dispatch(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=t('Review AUR packages with Codex before yay builds them'))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("session", help=t('Create an isolated yay review session (internal interface)'))
    gate = sub.add_parser("hook", help=t('yay AURPreInstall hook (internal interface)'))
    gate.add_argument("--pkgbase", required=True)
    gate.add_argument("--directory", required=True)
    gate.add_argument("--last-modified", type=int, required=True)
    gate.add_argument("--version", required=True)
    review = sub.add_parser("review", help=t('Review a local AUR Git repository without installing'))
    review.add_argument("directory", type=Path)
    review.add_argument("--pkgbase", required=True)
    review.add_argument("--json", action="store_true", help=t('Print structured JSON results'))
    installer = sub.add_parser("install", help=t('Install the current source and yay Lua plugin to a user directory'))
    installer.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    enable = sub.add_parser("enable", help=t("Enable the installed yay plugin for the current user"))
    enable.add_argument("--prefix", type=Path, default=None)
    sub.add_parser("disable", help=t("Disable the yay plugin for the current user"))
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
        if args.command == "enable":
            from .installer import enable
            enable(args.prefix)
            return 0
        if args.command == "disable":
            from .installer import disable
            disable()
            return 0
        config, _ = load_config()
        snapshot = snapshot_package(args.directory, args.pkgbase)
        if snapshot.commit != remote_head(args.pkgbase):
            raise GateError(t('Local commit is not the latest AUR commit; update the repository first'))
        result = Reviewer(config).review(snapshot)
        if args.json:
            print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
        else:
            display(args.pkgbase, result)
        return 1 if result.level == "red" else 0

    return run_safely(dispatch)
