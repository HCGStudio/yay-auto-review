# yay-auto-review AUR package

The stable package is named `yay-auto-review`, matching the GitHub repository.
It builds the Python wheel from a versioned upstream source archive and verifies
its SHA-256 checksum. The package depends on `openai-codex` from Arch's `extra`
repository, yay 13 or later, Python 3.11 or later, and Git.

## Install and enable

After the AUR package has been published:

```sh
yay -S yay-auto-review
codex login
/usr/bin/yay-auto-review enable
yay -Syu
```

Run `enable` as your regular user. The pacman installation only installs system
files and prints activation instructions; each user enables the plugin in their
own yay configuration. The existing `aur-auto-review` command remains available.
`yay-auto-review disable` removes the managed yay integration.

The installed launchers use Python's `-I` isolation flag. The wheel includes
translation resources. The Lua plugin under `/usr/share/aur-auto-review/` uses
absolute system launcher paths so old per-user installs do not shadow it.
Language selection uses `AUR_AUTO_REVIEW_LANG=en` / `zh_CN` / `auto`, or the
standard locale environment when set to `auto`.

## Build and verify

From this directory, after installing the listed build and test dependencies:

```sh
makepkg --verifysource
makepkg --cleanbuild
makepkg --printsrcinfo > .SRCINFO
```

The `check()` function runs the test suite, including the real Lua interpreter
and yay configuration smoke tests. Tests do not install packages or call the
real Codex model. Inspect the built package with `pacman -Qip` and `pacman -Qlp`.
Build outputs belong outside the source repository.

## Publish an update

1. Publish and verify the upstream version tag on GitHub.
2. Update `pkgver`, reset `pkgrel` to 1, and replace `sha256sums` with the checksum
   of the published source archive. Never publish a placeholder or `SKIP`.
3. Run the build and verification commands above and regenerate `.SRCINFO`.
4. In a separate clone of
   `ssh://aur@aur.archlinux.org/yay-auto-review.git`, commit `PKGBUILD`,
   `.SRCINFO`, `yay-auto-review.install`, and the upstream GPL `LICENSE`.
   Push its `master` branch to AUR.

AUR publishing requires an AUR account with its SSH public key registered.
See the [AUR submission guidelines](https://wiki.archlinux.org/title/AUR_submission_guidelines)
and [Python package guidelines](https://wiki.archlinux.org/title/Python_package_guidelines).
