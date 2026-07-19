# Boffin-Wayland — Step 1: C++ PTY core + Kivy bridge

This is the first working slice of the project: a real pseudo-terminal
implemented in C++ (NDK), exposed to Python via `ctypes`, and rendered by a
minimal Kivy UI. Package name `com.boffin.wayland`, custom `PREFIX`/`HOME`
paths — nothing shares state with a real Termux/Termux:X11 install.

## Folder layout

```
boffin_wayland/
├── android_src/
│   └── com/boffin/wayland/
│       └── LorieSurfaceView.java  # native X11-display SurfaceView (Java side)
├── cpp/
│   ├── pty_core.h / .cpp     # PTY/shell backend
│   ├── lorie_bridge.h / .cpp # JNI bridge: ANativeWindow capture + smoke test
│   ├── CMakeLists.txt        # builds libptycore.so + liblorie_bridge.so
│   └── build_native.sh       # cross-compiles both for all Android ABIs
├── python/
│   ├── main.py           # Kivy app: bootstrap UI, PtyCore bridge, TerminalView, LorieBridge
│   ├── vt100_parser.py   # VT100/ANSI state machine + character-grid Screen
│   ├── assets/fonts/     # drop a monospace .ttf here (see "Fonts" below)
│   └── assets/busybox/   # bundled static busybox-aarch64 binary (see "BusyBox" below)
├── buildozer.spec
└── README.md
```

## Build order

1. **Build the native library first** (needs Android NDK r25b + cmake + ninja):
   ```bash
   cd cpp
   export ANDROID_NDK_HOME=/path/to/Android/Sdk/ndk/25.2.9519653
   chmod +x build_native.sh
   ./build_native.sh
   ```
   This produces `boffin_wayland/libs/arm64-v8a/libptycore.so` (and
   `armeabi-v7a`, `x86_64`).

2. **Build the APK** with buildozer (from the `boffin_wayland/` root, where
   `buildozer.spec` lives):
   ```bash
   pip install buildozer
   buildozer android debug
   ```
   `buildozer.spec`'s `android.add_libs_*` keys tell p4a to copy the
   prebuilt `.so` straight into the APK's native lib folder, so
   `ctypes.CDLL("libptycore.so")` resolves it at runtime without a custom
   p4a recipe.

3. **On first launch, the app now bootstraps its own `PREFIX` automatically**
   (step 2 — see below) — no manual rootfs setup needed as long as the
   device has internet access on first run.

## What actually works right now

- **BootstrapManager**: on first launch, checks for `PREFIX/bin/bash` or
  `/sh`; if missing, detects the device CPU arch, looks up the *latest*
  `bootstrap-*` release from `termux/termux-packages` on GitHub, downloads
  the matching `bootstrap-<arch>.zip`, verifies its SHA-256 when published,
  extracts it into `PREFIX` (recreating `SYMLINKS.txt` symlinks), and
  `chmod +x`'s the binaries. The PTY core only spawns after this succeeds.
- A real child process forked onto a real PTY, with `PREFIX`/`HOME`/`PATH`
  pointed entirely at `com.boffin.wayland`'s own data directory.
- **`vt100_parser.py`**: a real VT100/xterm-subset state machine. `Screen`
  holds a rows x cols grid of `Cell`s (char + fg/bg color + bold/underline/
  reverse), cursor state, scroll region, an **alternate screen buffer**
  (nano/vim/htop), and now a **scrollback buffer** (2000 lines by default)
  for the primary screen — intentionally frozen while an app owns the alt
  screen, matching real terminals. `VTParser.feed(bytes)` decodes UTF-8
  incrementally and drives it all.
- **Fine-grained dirty tracking**: `Screen` reports exactly which row
  indices changed (`dirty_rows`), a cursor-only flag (`dirty_cursor`), a
  structural full-redraw flag (`dirty_all` — resize/erase/scroll/alt-
  toggle), and a scrollback-viewport flag (`view_dirty`), all read
  atomically via `pop_dirty()`.
- **Canvas-based `TerminalView`** (replaces the old markup-`Label`
  renderer): each row is one `kivy.graphics.InstructionGroup`; runs of
  same-styled cells are drawn as a background `Rectangle` (when a bg color
  is set) + a tinted glyph texture (cached, keyed by `(text, bold)`) + an
  optional underline `Rectangle`. **This is what makes per-cell background
  colors and reverse video render correctly** — nano's status bar, htop's
  highlighted process row, `less`'s search-match highlighting, etc.
  Redraws are selective: only `InstructionGroup`s for dirty rows get
  rebuilt; a lone cursor move or a single-line edit doesn't touch the rest
  of the screen. Still throttled to `REDRAW_HZ` (20/sec default) via
  `Clock.schedule_interval`, so heavy scrolling output can't outrun the UI
  thread even when it does trigger a full-frame rebuild.
- **Scrollback UI**: mouse wheel and touch-drag call `Screen.scroll_view()`;
  typing anything calls `Screen.scroll_to_bottom()` first, so you're always
  snapped back to live output the moment you start typing — matching how
  real terminal emulators behave.
- **Automatic monospace font**: if nothing is bundled under
  `python/assets/fonts/` and no known on-device system font is found,
  `main.py` downloads JetBrains Mono (SIL OFL-1.1, free for any use)
  directly from its GitHub repo on first launch and caches it on disk —
  same "check once, fetch once" pattern as `BootstrapManager`. You can
  still drop your own `.ttf` into `assets/fonts/` to skip the download.
- Real keyboard input (arrows, Home/End/PgUp/PgDn, Ctrl+`<letter>`, typed
  Unicode text) writes straight to the PTY — no "type a line, press
  Enter" step.
- Window resize recomputes rows/cols from pixel size and calls both
  `Screen.resize()` and `pty.resize()` (native `ioctl TIOCSWINSZ`).
- Clean process teardown (`pty_terminate`) on app exit.

## BusyBox

Right after the Termux bootstrap succeeds, `BusyboxManager` installs a
bundled static BusyBox binary (`python/assets/busybox/busybox-aarch64`,
BusyBox v1.36.1) into `PREFIX/bin/busybox`, then asks the binary itself
which applets it supports (`busybox --list` - not a hardcoded list, so this
stays correct if you swap in a different BusyBox build later) and creates
a symlink for each applet name in `PREFIX/bin/` **only if that name doesn't
already exist there**. This fills in extra commands the minimal Termux
bootstrap doesn't ship (`awk`, `sed`, `vi`, `xxd`, `tar`, ...) without ever
overwriting or shadowing the bootstrap's own real binaries (`bash`, `apt`,
`dpkg`, etc. stay exactly as Termux provides them) - verified with
standalone tests (existing binaries never get clobbered, re-running links
zero new symlinks, non-aarch64 devices are skipped cleanly rather than
crashing on a wrong-arch exec).

Not fatal if it fails or is skipped (e.g. on a non-aarch64 device, or if
the bundled binary is missing from a given build) - bash/sh from the
Termux bootstrap already work fine on their own; BusyBox just adds extra
commands on top. A status line explains what happened either way.

To bundle a different/updated BusyBox build, drop the binary at
`python/assets/busybox/busybox-aarch64` (must stay statically linked -
Termux's own bionic-linked libraries aren't guaranteed to satisfy a
dynamically-linked BusyBox) and it'll be picked up automatically; no code
changes needed unless you're adding a second architecture (in which case
extend `BusyboxManager.BUNDLED_ARCH`/`BUNDLED_FILENAME` to pick per-arch).

## Fonts

`main.py` looks for a font in this order: `python/assets/fonts/*.ttf` (drop
one there yourself to skip the network step) → a couple of common Android
system paths → **download JetBrains Mono automatically** on first launch →
finally Kivy's built-in default font (not monospace; columns will drift
slightly if you ever end up here, e.g. fully offline with no system font).

## Debugging a blank terminal / extra-keys toolbar

An earlier revision replaced the terminal's text rendering with a hand-
built "raw `CoreLabel` texture per run, drawn via `canvas` `Rectangle`s"
renderer. On a real device this showed a completely blank terminal - no
crash, no exception, just nothing visible - while the exact same textures
tested fine in a desktop GL context. The likely cause, confirmed by
inspecting the actual texture sizes involved: `CoreLabel` produces a
tightly-sized (non-power-of-two) texture matching the rendered text's
pixel dimensions (e.g. 76x17 for "bash-5.2$ ls"), and that renderer then
force-*stretched* it via `Rectangle(size=(run_w, char_h))` to an unrelated
target size to fill the fixed-width terminal cell grid. Non-uniform
stretching of an NPOT texture is a known rough edge on some Android GLES
driver/filtering combinations, even though it's completely fine on a
desktop OpenGL implementation (which is why every earlier sandbox test of
this code looked correct).

**Fix**: text rendering now uses a real `kivy.uix.Label(markup=True)` -
the exact same code path every `Button`/`Label` elsewhere in this app
already renders successfully with - which always draws its texture at its
own native size (never force-stretched), sidestepping that whole class of
driver quirk. Per-cell background colors (still needed for nano's status
bar, htop's highlighted rows, reverse video - the reason the raw-texture
renderer was attempted in the first place) are now handled by a *separate*
`InstructionGroup` of plain solid-color `Rectangle`s (grouped only by
background color, no texture involved at all) drawn behind the `Label`.

Trade-off: this rebuilds the whole label text + background rect list on
every dirty redraw tick instead of only touched rows. At the `REDRAW_HZ`
throttle (20/sec default) this is still cheap enough not to matter in
practice; reintroducing per-row diffing on top of this safer foundation is
a reasonable future optimization once basic rendering is confirmed
rock-solid across more real devices.

Two related hardening fixes landed alongside this (kept from the previous
revision, still valuable regardless of the above):
- `TerminalView._redraw()` and the PTY reader loop are wrapped so that any
  exception gets fed straight into the terminal's own screen buffer as
  visible red (`\x1b[31m`) text with a full traceback - no `adb logcat`
  needed to see what broke, for whatever the *next* rendering surprise
  turns out to be.
- `TerminalView.start()` defers its actual startup (grid sizing + PTY
  spawn) by one frame via `Clock.schedule_once`, instead of running
  synchronously the instant the widget is added to the layout - at that
  exact synchronous moment `self.size` is still Kivy's default widget size
  (not the real allocated area yet, since `BoxLayout` only assigns that on
  its next layout pass), so starting immediately could size the very
  first grid/PTY window wrong.

**Extra-keys toolbar**: a horizontally-scrolling row of buttons above the
terminal - ESC, TAB, CTRL, ALT, arrows, Home, End, PgUp, PgDn, and a few
common symbols (`/ - | ~`) - because soft keyboards generally don't send
real key-down events for these, so without it Ctrl+C, Tab-completion, and
Esc (needed for vim/nano) would be unreachable. CTRL and ALT are "sticky":
tap once to arm, then the *next* character typed (from the toolbar or the
soft keyboard) is sent as Ctrl+<char> or ESC+<char> respectively, then it
auto-disarms - the standard pattern Termux's own extra-keys row uses,
since soft keyboards don't deliver real held-modifier events either.
Verified with tests: CTRL+'c' sends `\x03`, ALT+'x' sends `ESC x`, arming
one modifier cancels the other, normal typing is unaffected when nothing's
armed. (The arrow-key glyphs may show as tofu/[] boxes if the active font
doesn't include those Unicode arrow characters - cosmetic only, the keys
still work; swap in a font with better symbol coverage if this bothers
you.)

## X11/Wayland display - architecture decision (read before building further)

This is the biggest remaining milestone, so the decisions made here matter
more than any previous step:

- **What we're building toward is NOT a wlroots-style Wayland compositor.**
  The real Termux:X11 (which we're using as our reference architecture) is
  actually a full X.Org X server ported to Android via a custom
  Android-specific DDX (device-dependent X layer) called **"Xlorie"**. It
  originally used XWayland but moved to a direct XCB-based approach.
  wlroots' usual backends assume `/dev/dri` (DRM/KMS) + GBM, which stock
  Android does not expose, so a genuine wlroots compositor would need a
  from-scratch Android GBM/DRM-less backend - effectively its own research
  project. Reusing the proven Xlorie-style architecture (custom DDX writing
  straight to `ANativeWindow`) is the realistic path.
- **License decision (made and locked in): we're vendoring Xlorie.**
  `termux/termux-x11` is **GPLv3-licensed**. Vendoring/linking its DDX code
  into Boffin-Wayland's native binaries makes the *entire combined app*
  subject to GPLv3 - full source must be made available on distribution.
  This project has accepted that tradeoff for faster progress over writing
  an independent DDX from scratch. **Before any public release, add a
  top-level `LICENSE` file (GPLv3 text) and a `NOTICE`/attribution section
  crediting the termux-x11 project**, and keep that in mind for any
  distribution channel that wouldn't be comfortable with copyleft
  obligations (e.g. a closed-source rebrand is no longer an option once
  Xlorie code actually lands in this repo - it hasn't yet, only our own
  from-scratch bridge code below has, which we own outright).

### What's been built so far: the Native SurfaceView + JNI bridge (foundation)

Before writing a single line of X server code, we needed to prove the
underlying pipeline (Java View → JNI → `ANativeWindow` → hardware buffer →
SurfaceFlinger) actually works on this project's own package/build setup.
That's what this step delivers - **no X server yet**, just the verified
plumbing it will render into:

- `android_src/com/boffin/wayland/LorieSurfaceView.java` - a plain Java
  `SurfaceView` (deliberately not routed through Kivy/pyjnius callbacks,
  since surface lifecycle and input delivery are latency-sensitive).
  Forwards `surfaceCreated`/`surfaceChanged`/`surfaceDestroyed` and touch/
  key events straight to `native` methods.
- `cpp/lorie_bridge.h` / `.cpp` - the JNI implementation. Captures the
  `ANativeWindow` behind the Java view via `ANativeWindow_fromSurface()`,
  and as a **smoke test**, fills it with a solid dark-slate-blue color
  (`#2E3440`) whenever the surface is created or resized - a color
  distinct from both Kivy's own background and plain black, so seeing it
  appear is unambiguous confirmation this is really the native surface.
  Touch/key events are logged via logcat for now. Also exposes the public
  C API (`boffin_lorie_get_window()`, `boffin_lorie_set_window_event_callback()`)
  that the future X server DDX will call instead of the smoke test.
- `python/main.py`'s new `LorieBridge` class - pyjnius glue. `show()`
  instantiates `LorieSurfaceView` and attaches it on top of Kivy's view via
  `activity.addContentView()` (wrapped in a `Runnable` via
  `PythonJavaClass`, since this must run on the UI thread); `hide()` removes
  it. Wired to an "X11 Display (experimental)" toggle button above the
  terminal. `LorieBridge.available()` detects when pyjnius/Android aren't
  present (e.g. testing `main.py`'s Kivy logic elsewhere) and shows a clear
  message instead of crashing.
- Both `lorie_bridge.cpp` (against stub JNI/Android headers) and
  `LorieSurfaceView.java` (brace/paren balance) were syntax-checked in this
  environment; a real build/device test still needs the actual Android
  NDK/SDK toolchain (see "How to test this step" below).

### How to test this step

1. Run `cpp/build_native.sh` (now builds **both** `libptycore.so` and
   `liblorie_bridge.so` per ABI - see the updated script).
2. `buildozer android debug` (picks up `android_src/` via
   `buildozer.spec`'s `android.add_src` - **this line is the most
   version-fragile part of the whole pipeline**; see the comment next to
   it in `buildozer.spec` if the Java class doesn't get compiled in).
3. On a real device, tap "X11 Display (experimental)". You should see a
   solid dark-slate rectangle cover the terminal. That confirms the bridge
   works. Tap again to hide it and return to the terminal.

### Step-by-step plan from here

1. ✅ Native SurfaceView + JNI bridge (this step).
2. Decide on and vendor an initial X.Org `xserver` source tree + start
   adapting/porting Xlorie's DDX (or writing our own against the same
   `ANativeWindow` target) so it renders through `boffin_lorie_get_window()`
   instead of the smoke-test fill.
3. Wire `LorieSurfaceView`'s touch/key JNI callbacks into the X server's
   input subsystem (replacing the current logcat-only logging).
4. Run the X server as its own process (can reuse `PtyCore`'s
   `forkpty`-style spawn pattern) and set `DISPLAY` so shell-launched GUI
   apps connect to it.
5. Add X11 desktop packages (e.g. `xfce4`, `x11-repo`) to
   `BootstrapManager` or a follow-up installer step.
6. (Later) Investigate GPU-accelerated GL passthrough (`virglrenderer` or
   similar) - the smoke-test fill above only proves 2D buffer blitting
   works, not accelerated GL client rendering.

## What this step deliberately does NOT include yet (be aware before you rely on it)

- **No double-width/double-height character support** (DECDHL/DECDWL),
  mouse reporting, or bracketed paste.
- **Scrollback is primary-screen only and capped at 2000 lines** (no
  persistence across app restarts, no search).
- **No Wayland/X11 display server.** Termux:X11 is a large, separate
  subsystem (its own rendering surface + a full X server binary). Bundling
  an equivalent is a bigger follow-up task on its own.
- **Bootstrap and the font download both require network access on first
  run.** If the device is offline, bootstrap shows a clear error (and the
  terminal won't start), while the font step just falls back gracefully to
  Kivy's default font rather than failing the whole app.
- **No offline/bundled bootstrap fallback** yet — shipping a rootfs zip
  inside the APK itself (like real Termux does via `libtermux-bootstrap.so`)
  is a reasonable follow-up if offline-first matters to you.

Happy to move on to vendoring the Xlorie DDX and getting the X server
rendering through this bridge, wiring real input, or the offline/bundled
bootstrap — just say which one.
