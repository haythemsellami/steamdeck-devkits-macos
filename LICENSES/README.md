# LICENSES — third-party notices carried by this repo

License texts and notices for code/binaries we vend or redistribute. This
project itself is MIT (see `pyproject.toml`).

| File | Covers | Notes |
|---|---|---|
| `upstream-steamos-devkit-MIT.txt` | Valve/Collabora steamos-devkit (MIT) | The vendored snapshot in `src/steamos_devkit/_vendor/` (protocol reference + `devkit-utils/` device-side helpers). |
| `rsync-GPLv3.txt` | rsync 3.x (GPLv3) | Applies to the rsync binary bundled into the packaged .app by `scripts/bundle-rsync.sh`. Verbatim copy of `third-party-licenses/GPL-3.txt` from upstream steamos-devkit @ `a00ceb7d91ea44a0c3e714a91a06417d6e5cdb33` (Valve ships the same text with its Windows rsync bundle). rsync copyright notices: upstream `third-party-licenses/rsync-3.1.2-LICENSE.txt`. |

## rsync source offer (GPLv3)

The bundled binary is unmodified Homebrew rsync. Corresponding source:
<https://rsync.samba.org/> (upstream releases) and the exact build recipe in
the Homebrew formula,
<https://github.com/Homebrew/homebrew-core/blob/master/Formula/r/rsync.rb>.
This offer must accompany any distributed bundle (app resources and/or
installer text), per D4.

## Pending: embedded Python runtime

The packaged app embeds a python-build-standalone CPython runtime
(`scripts/embed-python.sh`). That distribution carries its own license notices
(the runtime tarball's `licenses/` entries); when the runtime ships in a
distributed build, add its license texts here (e.g.
`python-build-standalone-licenses/`) and record it in
`docs/packaging-spike.md`.
