# LICENSES — third-party notices carried by this repo

License texts and notices for code/binaries we vend or redistribute. This
project itself is MIT (see the root `LICENSE` file).

| File | Covers | Notes |
|---|---|---|
| `upstream-steamos-devkit-MIT.txt` | Valve/Collabora steamos-devkit (MIT) | The vendored snapshot in `src/steamos_devkit/_vendor/` (protocol reference + `devkit-utils/` device-side helpers). |
| `rsync-GPLv3.txt` | rsync 3.x (GPLv3) | Applies to the rsync binary bundled into the packaged .app by `scripts/bundle-rsync.sh`. Verbatim copy of `third-party-licenses/GPL-3.txt` from upstream steamos-devkit @ `a00ceb7d91ea44a0c3e714a91a06417d6e5cdb33` (Valve ships the same text with its Windows rsync bundle). rsync copyright notices: upstream `third-party-licenses/rsync-3.1.2-LICENSE.txt`. |
| `python-runtime/` | Embedded CPython runtime: CPython 3.12.15 (PSF) and pip 26.2.1 (MIT) + pip's vendored dependencies | Verbatim extracts from the pinned python-build-standalone tarball (`cpython-3.12.15+20261013-aarch64-apple-darwin-install_only.tar.gz`, sha256 `ba32036a…e4440`, see `scripts/embed-python.sh`): `CPython-3.12.15-LICENSE.txt` ← tarball `python/lib/python3.12/LICENSE.txt`; `pip-26.2.1/` ← tarball `python/lib/python3.12/site-packages/pip-26.2.1.dist-info/licenses/`. Applies to the runtime installed into the packaged .app by `scripts/embed-python.sh`. |

## rsync source offer (GPLv3)

The bundled binary is unmodified Homebrew rsync. Corresponding source:
<https://rsync.samba.org/> (upstream releases) and the exact build recipe in
the Homebrew formula,
<https://github.com/Homebrew/homebrew-core/blob/master/Formula/r/rsync.rb>.
This offer must accompany any distributed bundle (app resources and/or
installer text), per D4.

## Embedded Python runtime

The packaged app embeds a python-build-standalone CPython runtime
(`scripts/embed-python.sh`, pinned CPython 3.12.15+20261013 aarch64). Its
license notices, extracted verbatim from the runtime tarball, live in
`python-runtime/` and ship with any distributed build (closes the D4
follow-up).
