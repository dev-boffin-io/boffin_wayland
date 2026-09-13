# Building a custom Termux bootstrap for com.boffin.wayland

This replaces `BootstrapManager`'s current approach (downloading the
*official* Termux bootstrap, built for `com.termux`, then text-patching a
few hardcoded paths afterward - see `_patch_hardcoded_termux_paths()` in
main.py) with a bootstrap built from source with the **correct package
name from the start**. That fixes the deeper issue the text-patch
couldn't reach: paths compiled into ELF binaries (apt/dpkg and friends),
not just plain-text config files.

**This must be run on your own machine.** It needs
Docker, a good amount of disk space (20-50GB recommended - package
sources, build artifacts, and Docker layers add up), and time (can be
1-4+ hours depending on your CPU - it's compiling dozens of packages from
source, not just downloading them).

## Prerequisites

- Docker installed and running (`docker --version` to check)
- ~50GB free disk space to be safe
- A few hours of uninterrupted time (it can run unattended once started)
- git

## Step 1 — Clone termux-packages

```bash
git clone https://github.com/termux/termux-packages.git
cd termux-packages
```

## Step 2 — Set the custom package name

Edit `scripts/properties.sh` and find the `TERMUX_APP_PACKAGE` line.
Change it to:

```bash
TERMUX_APP_PACKAGE="com.boffin.wayland"
```

This is the single variable that controls what path (`/data/data/<this>/files/usr`)
every package in the bootstrap gets built to expect - both in plain-text
config files (what our old patch fixed) and, critically, **compiled into
the binaries themselves** (what the old patch couldn't fix).

## Step 3 — Enter the Docker build environment

```bash
./scripts/run-docker.sh
```

This drops you into a shell inside the build container
(`builder@<container-id>:~/termux-packages$`). Everything from here on
happens *inside* that container.

## Step 4 — Build the bootstrap

```bash
./scripts/build-bootstraps.sh --architectures aarch64
```

(Drop `--architectures aarch64` if you want all 4 architectures - but
since our own `buildozer.spec` only targets `arm64-v8a`/`armeabi-v7a`,
aarch64-only is enough to start, and much faster.)

If you change `TERMUX_APP_PACKAGE` again later, run
`./scripts/run-docker.sh ./clean.sh` first (or pass `-f` to force a
rebuild) - otherwise stale build artifacts for the old package name can
leak through.

This step is the long one. It builds bash, coreutils, dpkg, apt, ncurses,
readline, openssl, libandroid-support, and everything else in the default
bootstrap package list **from source**, for your custom package name.

## Step 5 — Collect the output

When it finishes, you'll have `bootstrap-aarch64.zip` (and others if you
built multiple architectures) in the `termux-packages` directory itself.

```bash
ls -la bootstrap-*.zip
```

## Step 6 — Bring it back into Boffin-Wayland

Copy the zip into this project at:

```
python/assets/bootstrap/bootstrap-aarch64.zip
```

(create the `bootstrap` directory if it doesn't exist). `BootstrapManager`
has been updated (see the accompanying `main.py` changes) to check for
this bundled file first and use it directly - no network download, no
GitHub API lookup, no checksum verification needed (it's bundled in the
APK itself, already covered by APK signing) - and since the paths are
correct from build time, `_patch_hardcoded_termux_paths()` becomes a
no-op safety net rather than a required fix.

Remember to also add the new asset path to `buildozer.spec`'s
`source.include_patterns` (already done in the accompanying diff -
`assets/bootstrap/*`), the same way `assets/busybox/*` was added earlier.

## Known follow-up issues to watch for

- The GitHub issue we found while researching this
  (termux/termux-packages#24396) reports that after a custom-package-name
  build, `apt` works but `pkg` (Termux's own wrapper script) can still
  reference `/data/data/com.termux/...` in one spot
  (`termux-setup-package-manager`). Check for this after your first build;
  our own app doesn't currently rely on `pkg` (only bash/coreutils/apt
  directly), so this may not matter to us, but worth knowing about if you
  add it to the terminal workflow later.
- Package list: `build-bootstraps.sh`'s default package set is defined in
  the script itself. If you want additional tools baked into the
  bootstrap by default (rather than installed later via `apt`), pass
  `-a <package>` / `--add <package>` - check `build-bootstraps.sh --help`
  inside the Docker container for the exact syntax.
- This produces packages compiled specifically for `com.boffin.wayland` -
  they are **not compatible** with the official Termux apt repos/mirrors
  (per termux-packages' own wiki warning). If you ever want `apt install`
  to pull *new* packages beyond what's baked into this bootstrap, you'd
  need to host your own apt repository built the same way - a separate,
  larger undertaking not covered here.
