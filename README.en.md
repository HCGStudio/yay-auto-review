# yay-auto-review

English | [简体中文](README.md)

Review AUR packaging files with Codex **before yay executes a PKGBUILD**.
The plugin uses native Lua hooks in yay 13 or later, reports its findings,
and requires your confirmation before building. It covers AUR upgrades,
new installations, dependencies, and split packages.

| Level | Meaning | Confirmation |
| --- | --- | --- |
| Green | Well-known open-source software, verified official sources, no malicious scripts or other identified issues | Enter `y` |
| White | Lesser-known open-source software, verified official sources, no malicious scripts or other identified issues | Enter `y` |
| Yellow | No malicious behavior found, but other issues or uncertainty remain | Type the package base name |
| Red | Review refused, malicious behavior found, or review could not complete reliably | Build and installation are blocked |

Evidence is required for green/white classifications. Familiar package names
and a source hosted on GitHub alone do not establish official provenance.

## Install from AUR

```sh
yay -S yay-auto-review
codex login                 # If not already signed in
yay-auto-review enable      # Run as your regular user, without sudo
yay -Syu
```

The package uses `openai-codex` from Arch's official repositories, Python 3.11+,
Git, and yay 13+. The `yay-bin` and `yay-git` providers are supported.
System installation does not edit users' home directories; each user activates
the plugin with `enable`. The per-user loader follows the shared system Lua
plugin so upgrades take effect automatically.

The original `aur-auto-review` command remains an alias. Existing configuration
and cache paths keep their `aur-auto-review` names. See
[AUR packaging](packaging/aur/README.md) for build and maintenance details.

## Install from source

With yay, Python, Git, and an authenticated current Codex CLI already installed:

```sh
./install.sh
```

This installs under `~/.local`, activates the plugin for the current user, and
backs up existing yay configuration before changing it. Use `--prefix` to choose
another installation prefix. Launchers use Python `-I` isolation so an AUR
checkout cannot inject Python modules through the current directory or PYTHONPATH.

## Languages

English (`en`) and Simplified Chinese (`zh_CN`) are supported. Selection order:

1. `--lang` on the command line.
2. `AUR_AUTO_REVIEW_LANG`.
3. `language` in the configuration file.
4. `LC_ALL`, then `LC_MESSAGES`, then `LANG`.
5. English for `C`, `POSIX`, or unsupported locales.

```sh
yay-auto-review --lang en --help
yay-auto-review --lang zh_CN review /path/to/package --pkgbase package
AUR_AUTO_REVIEW_LANG=en yay -Syu
```

Create `~/.config/aur-auto-review/config.toml` (or the corresponding
`XDG_CONFIG_HOME` path) to configure persistent preferences:

```toml
language = "auto"
codex = "codex"
timeout_seconds = 300
makepkg = "/usr/bin/makepkg"
# model = "your-model-id"
```

Interface messages and Codex reports use the selected language. Cached reports
are separated by language. JSON level identifiers remain stable and untranslated.
Translations are UTF-8 JSON catalogs in `aur_auto_review/locales/`; English
messages are their keys. Missing or incompatible translations fall back to English.

## Cache and approval

A review is reused only when it is **less than one hour old**, the latest AUR
Git HEAD is unchanged, and the reviewed file content, executable bits, package
metadata, policy, model configuration, review scope, and report language match.
Remote verification failures never count as “no update”. Cache writes are atomic
and private; concurrent identical requests share one review.

Every skipped package is named with the reason for reuse. Cached results still
require fresh user confirmation. `--noconfirm`, piped input, and missing terminals
cannot bypass the review gate. Red reports continue to block installation.

The first snapshot includes all checkout files except Git metadata. After the
review, the makepkg guard checks the approved files again; edits trigger another
review and confirmation before execution. A new session directory prevents reuse
of old sources and binaries, including archives in a global `PKGDEST`.

## Scope and limits

This is static review of AUR packaging files. It does not audit all upstream
source code, downloaded archives, binaries, or transitive build dependencies.
Models can miss issues or produce false positives. Green/white is not a security
guarantee, and makepkg builds are not sandboxed by this plugin.

Symlinks, hardlinks, binary/non-UTF-8 packaging files, special files, and inputs
exceeding the review limits block the operation. Limits are 512 KiB per file,
2 MiB total, and 512 files. Generated/downloaded files are outside later manifest
checks. Review and exec are not an operating-system-atomic boundary.

The plugin rejects yay overrides that disable or persist its temporary settings
(`--makepkg`, `--builddir`, `--mflags`, `--save`) and prevents makepkg from selecting
another recipe or working directory. It protects only yay sessions loading this
plugin. Codex uses a neutral temporary directory, read-only sandbox, disabled
execution tools, existing login credentials, and live source verification.
Personal Codex configuration is not loaded.

Caches and retained build directories are under
`${XDG_CACHE_HOME:-~/.cache}/aur-auto-review/`. After a yay transaction has exited,
its `builds/<session>` and `receipts/<session>` directories may be removed to
recover disk space.

## Disable and test

Run `yay-auto-review disable` before uninstalling the package. It removes only
the managed activation stanza and preserves unrelated yay settings and history.

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary repositories and fake model responses. Where available,
they also exercise real Lua, yay configuration loading, and harmless makepkg
metadata commands. They do not install packages or call a real model.

## License

Copyright (c) 2026 aur-auto-review contributors.
Licensed under [GPL-3.0-only](LICENSE).
