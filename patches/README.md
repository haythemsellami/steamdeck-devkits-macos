# patches/ — quilt-style series over the vendored upstream core

`src/steamos_devkit/_vendor/` is a read-only snapshot of Valve's steamos-devkit
(`client/devkit_client/{__init__.py,proxy.py,captured_popen.py}` and
`client/devkit-utils/`), pinned by `_vendor/UPSTREAM_COMMIT`
(docs/decisions.md D1).

**Prefer wrappers over patches.** A patch is created only when a fix cannot
live outside the vendored files (host adapters and typed wrappers absorb almost
everything). Upstream arrives as squashed "code drops", so every patch must
re-apply cleanly onto a pristine copy of the pinned commit.

## Rules

- Files are named `NNNN-short-description.patch` and applied in lexical order
  by `scripts/sync-upstream.sh`.
- Paths inside a patch are relative to `_vendor/` with `a/` and `b/` prefixes
  (i.e. `-p1` from inside `_vendor/`).
- Never hand-edit `_vendor/` in a normal commit: edit, regenerate the patch,
  and commit the patch file instead.
- `devkit-utils/` is the device-side protocol surface — it is used verbatim and
  must never appear in a patch.

## Creating a patch

```bash
scripts/sync-upstream.sh                 # pristine _vendor at the pinned commit
cd src/steamos_devkit/_vendor
cp -r devkit_client devkit_client.orig   # pristine reference copy
$EDITOR devkit_client/__init__.py        # make the fix
diff -ruN devkit_client.orig devkit_client > ../../../patches/0001-my-fix.patch
rm -rf devkit_client.orig
cd ../../..
scripts/sync-upstream.sh                 # verify: re-copies and applies cleanly
```

## Bumping the upstream pin

```bash
scripts/sync-upstream.sh <new-ref>       # re-vendors, rewrites UPSTREAM_COMMIT
# fix any patches that no longer apply, then commit _vendor/ + patches/ together
```
