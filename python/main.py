"""
Boffin-Wayland - Kivy frontend
==============================

This is the Python "brain" that talks to the C++ PTY core (libptycore.so)
through ctypes and renders terminal I/O with Kivy widgets.

STEP 2 - BootstrapManager: downloads/extracts a real Termux rootfs into our
own isolated PREFIX before the PTY core is ever spawned. See BootstrapManager.

STEP 3 - vt100_parser.py: a real VT100/xterm-subset state machine (Screen +
VTParser) turns the PTY's raw byte stream into a character grid with colors,
cursor state, and an alternate-screen buffer.

STEP 4 (this revision):
  * TerminalView now renders with raw Kivy canvas instructions (Color +
    Rectangle, one small texture per run of same-styled characters) instead
    of a single markup Label, so per-cell BACKGROUND colors and reverse
    video finally render correctly (nano's status bar, htop's highlighted
    rows, etc.).
  * Screen gained a scrollback buffer (deque, default 2000 lines) that
    captures lines scrolled off the top of the *primary* screen. Mouse
    wheel / touch-drag scrolls through it. It's intentionally frozen/unused
    while an app is using the alternate screen buffer (nano/vim/htop),
    matching real terminal behavior.
  * Redraws are "selective": Screen tracks which row indices actually
    changed (dirty_rows) plus a cursor-only dirty flag, separate from a
    structural dirty_all flag (resize/clear/scroll/alt-screen-toggle).
    TerminalView only rebuilds canvas instructions for rows that changed -
    typing a character, moving the cursor, or editing one line no longer
    touches the rest of the screen. Heavy scrolling output (e.g. `yes`,
    `cat` on a big file) still triggers a full-frame rebuild each newline,
    same as any terminal, but that's capped by the REDRAW_HZ throttle so it
    can't outrun the UI thread.
  * If no monospace font is bundled under assets/fonts/ and none of the
    known on-device system paths exist, main.py downloads JetBrains Mono
    (OFL-1.1 licensed, free for any use) directly from its GitHub repo on
    first launch - same "check first, fetch once, cache on disk" pattern
    as BootstrapManager.

IMPORTANT / HONEST LIMITATIONS THAT STILL APPLY:
  * No double-width/double-height character support, mouse reporting, or
    bracketed paste.
  * There is no Wayland/X11 display server in this file. Termux:X11 is a
    large, separate subsystem (its own SurfaceFlinger-backed rendering
    activity + a full X server) - a bigger follow-up task on its own.
  * Both the bootstrap and the font download require network access on
    first launch (android.permissions = INTERNET in buildozer.spec).
"""

import hashlib
import json
import os
import platform
import shutil
import ssl
import stat
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from collections import OrderedDict
from ctypes import (
    CDLL, c_char_p, c_int, POINTER, byref, create_string_buffer
)

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle, InstructionGroup
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

from vt100_parser import Screen, VTParser, DEFAULT_FG, DEFAULT_BG

# ---------------------------------------------------------------------------
# Custom environment - deliberately different from real Termux so the two
# can coexist on the same device without any path/shell collision.
# ---------------------------------------------------------------------------
PREFIX = "/data/data/com.boffin.wayland/files/usr"
HOME = "/data/data/com.boffin.wayland/files/home"
LIB_NAME = "libptycore.so"  # bundled by buildozer, loaded from the app's native lib dir

# ---------------------------------------------------------------------------
# HTTPS certificate verification.
#
# python-for-android's cross-compiled CPython for Android does NOT wire up
# a system CA trust store the way a desktop Python install does (it has no
# access to Android's certificate store, and ships no CA bundle of its own).
# Without this, every urllib.request.urlopen() call to an https:// URL fails
# on-device with:
#   ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]
#   certificate verify failed: unable to get local issuer certificate
# even though the exact same code works fine on a desktop dev machine (which
# does have a system CA store). The fix is to point the SSL context at the
# `certifi` package's bundled CA file explicitly - certifi is pulled in by
# buildozer.spec's requirements (it's also a transitive dependency of the
# `requests` recipe, but we depend on it directly here since we only use
# the stdlib urllib, not requests).
# ---------------------------------------------------------------------------


def _https_context():
    """Returns an SSL context that verifies against certifi's CA bundle.
    Falls back to Python's own default context (with a note) if certifi
    isn't importable for some reason, rather than silently disabling
    verification - a failed download is better than an unverified one."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Bootstrap source: the official Termux rootfs archives published on
# termux/termux-packages GitHub releases. We fetch the *latest* release
# tagged "bootstrap-*" at runtime (instead of hardcoding one) so this code
# doesn't silently go stale when Termux cuts a new bootstrap. The archive
# itself is identical to what real Termux ships - only the extraction
# destination (our own com.boffin.wayland PREFIX) differs, so there is no
# collision with a real Termux install.
# ---------------------------------------------------------------------------
GITHUB_RELEASES_API = "https://api.github.com/repos/termux/termux-packages/releases"

ARCH_MAP = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv7l": "arm",
    "armv8l": "arm",
    "armv6l": "arm",
    "i686": "i686",
    "i386": "i686",
    "x86_64": "x86_64",
}


class BootstrapError(RuntimeError):
    pass


class BootstrapManager:
    """
    Ensures PREFIX contains a working shell (bin/bash or bin/sh) *before*
    the C++ PTY core is ever spawned. If it's missing, downloads the
    architecture-matching official Termux bootstrap-*.zip and extracts it
    into our own isolated PREFIX.

    Callbacks:
        on_status(str)     - human-readable status line, called from any thread
        on_progress(float) - 0.0-1.0 progress fraction, called from any thread
    Neither callback touches Kivy widgets directly - the caller is
    responsible for marshaling back to the main thread (see TerminalApp
    below, which wraps both in Clock.schedule_once).
    """

    def __init__(self, prefix: str, on_status=None, on_progress=None):
        self.prefix = prefix
        self.on_status = on_status or (lambda msg: None)
        self.on_progress = on_progress or (lambda frac: None)

    # -- public API -----------------------------------------------------

    def shell_present(self) -> bool:
        return (
            os.path.exists(f"{self.prefix}/bin/bash")
            or os.path.exists(f"{self.prefix}/bin/sh")
        )

    def ensure_bootstrap(self):
        """Idempotent: does nothing if a shell already exists at PREFIX."""
        if self.shell_present():
            self.on_status("Bootstrap already present.")
            self.on_progress(1.0)
            return

        parent_dir = os.path.dirname(self.prefix.rstrip("/"))
        os.makedirs(parent_dir, exist_ok=True)

        arch = self._detect_arch()
        self.on_status(f"Looking up latest bootstrap for {arch}...")
        release = self._find_latest_bootstrap_release()
        asset_url, expected_sha256 = self._asset_url_and_hash(release, arch)

        tmp_zip = os.path.join(parent_dir, f"bootstrap-{arch}.zip")
        try:
            self._download(asset_url, tmp_zip)

            if expected_sha256:
                self.on_status("Verifying checksum...")
                actual = self._sha256_of(tmp_zip)
                if actual.lower() != expected_sha256.lower():
                    raise BootstrapError(
                        "Checksum mismatch on downloaded bootstrap "
                        f"(expected {expected_sha256}, got {actual}) - "
                        "refusing to install a corrupted/tampered archive."
                    )
            else:
                self.on_status("No published checksum found for this release, skipping verification.")

            self._extract(tmp_zip)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

        if not self.shell_present():
            raise BootstrapError(
                "Bootstrap extracted but no bin/bash or bin/sh was found afterwards - "
                "unexpected zip contents."
            )

        self.on_status("Bootstrap ready.")
        self.on_progress(1.0)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _detect_arch() -> str:
        machine = platform.machine() or os.uname().machine
        arch = ARCH_MAP.get(machine)
        if not arch:
            raise BootstrapError(f"Unsupported/unrecognized CPU architecture: {machine!r}")
        return arch

    def _find_latest_bootstrap_release(self) -> dict:
        req = urllib.request.Request(
            GITHUB_RELEASES_API + "?per_page=30",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "boffin-wayland"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=_https_context()) as resp:
                releases = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BootstrapError(f"Could not reach GitHub releases API: {exc}") from exc

        for rel in releases:
            if rel.get("tag_name", "").startswith("bootstrap-"):
                return rel
        raise BootstrapError("No 'bootstrap-*' release found on termux/termux-packages")

    @staticmethod
    def _asset_url_and_hash(release: dict, arch: str):
        asset_name = f"bootstrap-{arch}.zip"
        asset_url = None
        for asset in release.get("assets", []):
            if asset.get("name") == asset_name:
                asset_url = asset.get("browser_download_url")
                break
        if not asset_url:
            raise BootstrapError(
                f"Release {release.get('tag_name')!r} has no asset named {asset_name!r}"
            )

        # Termux publishes a "sha256  filename" checksum list in the release
        # description - grab it opportunistically for integrity verification.
        expected_sha256 = None
        for line in (release.get("body") or "").splitlines():
            line = line.strip()
            parts = line.split()
            if len(parts) == 2 and parts[1] == asset_name and len(parts[0]) == 64:
                expected_sha256 = parts[0]
                break
        return asset_url, expected_sha256

    def _download(self, url: str, dest_path: str):
        self.on_status("Downloading bootstrap...")
        req = urllib.request.Request(url, headers={"User-Agent": "boffin-wayland"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=_https_context()) as resp, open(dest_path, "wb") as out:
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                block_size = 1 << 16
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        self.on_progress(min(1.0, downloaded / total_size))
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total_size / (1024 * 1024)
                        self.on_status(f"Downloading bootstrap... {mb_done:.1f}/{mb_total:.1f} MB")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BootstrapError(f"Download failed: {exc}") from exc

    @staticmethod
    def _sha256_of(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()

    def _extract(self, zip_path: str):
        self.on_status("Extracting bootstrap files...")
        staging = self.prefix.rstrip("/") + ".staging"
        if os.path.exists(staging):
            shutil.rmtree(staging)
        os.makedirs(staging, exist_ok=True)

        # Termux bootstrap zips don't store real symlinks (zip has no clean
        # cross-platform symlink support); instead a SYMLINKS.txt lists them
        # as "target<-arrow>link_path", one per line, using the U+2190 (<-)
        # character as separator. We recreate them for real after extraction.
        pending_symlinks = []  # (target, link_path_relative_to_prefix)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                total = len(names) or 1
                for i, name in enumerate(names):
                    if name == "SYMLINKS.txt":
                        with zf.open(name) as f:
                            text = f.read().decode("utf-8")
                        for raw_line in text.splitlines():
                            raw_line = raw_line.strip("\n")
                            if not raw_line:
                                continue
                            if "\u2190" not in raw_line:
                                continue  # malformed/unexpected line, skip defensively
                            target, link_path = raw_line.split("\u2190", 1)
                            pending_symlinks.append((target, link_path))
                        self.on_progress((i + 1) / total)
                        continue

                    target_path = os.path.join(staging, name)
                    if name.endswith("/"):
                        os.makedirs(target_path, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        with zf.open(name) as src, open(target_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    self.on_progress((i + 1) / total)
        except zipfile.BadZipFile as exc:
            raise BootstrapError(f"Downloaded bootstrap archive is not a valid zip: {exc}") from exc

        self.on_status(f"Creating {len(pending_symlinks)} symlinks...")
        for target, link_path in pending_symlinks:
            link_full_path = os.path.join(staging, link_path)
            os.makedirs(os.path.dirname(link_full_path), exist_ok=True)
            if os.path.lexists(link_full_path):
                os.remove(link_full_path)
            os.symlink(target, link_full_path)

        self.on_status("Setting executable permissions...")
        exec_prefixes = ("bin", "libexec", os.path.join("lib", "apt", "methods"))
        for root, _dirs, files in os.walk(staging, followlinks=False):
            rel_root = os.path.relpath(root, staging)
            if rel_root == ".":
                rel_root = ""
            if not rel_root.startswith(exec_prefixes):
                continue
            for fname in files:
                fpath = os.path.join(root, fname)
                if os.path.islink(fpath):
                    continue  # permission bits on symlinks are meaningless/not settable on Android
                try:
                    st = os.stat(fpath)
                    os.chmod(fpath, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                except OSError:
                    pass

        if os.path.exists(self.prefix):
            shutil.rmtree(self.prefix)
        shutil.move(staging, self.prefix)


class BusyboxError(RuntimeError):
    pass


class BusyboxManager:
    """
    Installs a bundled static BusyBox binary into PREFIX/bin and symlinks
    every applet it supports (ls, grep, sed, awk, tar, vi, xxd, ...) into
    PREFIX/bin too - but ONLY for command names that don't already exist
    there. This fills in extra commands the minimal Termux bootstrap
    doesn't ship, without ever overwriting or shadowing the bootstrap's
    own real binaries (bash, apt, dpkg, etc. stay exactly as Termux
    provides them) - matching the actual goal ("extra commands besides
    the basics"), not a bash/coreutils replacement.

    Unlike BootstrapManager and the font downloader, this binary is
    bundled directly in the APK under assets/busybox/ rather than
    downloaded - it was hand-built for this project, so there's no
    network step or release lookup needed here.

    Currently only ships an aarch64 build (see assets/busybox/). On any
    other device architecture this is skipped with a clear status message
    rather than attempted - silently failing to exec a wrong-arch ELF
    would be a confusing crash instead.

    Not fatal if it fails: BusyBox is a nice-to-have, not required for the
    terminal core (bash/sh from the bootstrap already work on their own).
    Callers should catch BusyboxError and continue rather than blocking
    the whole app on it.
    """

    BUNDLED_ARCH = "aarch64"
    BUNDLED_FILENAME = "busybox-aarch64"

    def __init__(self, prefix: str, on_status=None, on_progress=None):
        self.prefix = prefix
        self.on_status = on_status or (lambda msg: None)
        self.on_progress = on_progress or (lambda frac: None)

    # -- public API -----------------------------------------------------

    def installed(self) -> bool:
        return os.path.exists(os.path.join(self.prefix, "bin", "busybox"))

    def ensure_busybox(self):
        """Idempotent: does nothing if PREFIX/bin/busybox already exists.
        Skips (not an error) if the device isn't aarch64 or the bundled
        binary is missing from this build."""
        if self.installed():
            self.on_status("BusyBox already installed.")
            self.on_progress(1.0)
            return

        machine = platform.machine() or os.uname().machine
        arch = ARCH_MAP.get(machine)
        if arch != self.BUNDLED_ARCH:
            self.on_status(
                f"BusyBox skipped: only an {self.BUNDLED_ARCH} build is bundled "
                f"(this device is {arch or machine!r})."
            )
            self.on_progress(1.0)
            return

        bundled_path = self._bundled_busybox_path()
        if not os.path.exists(bundled_path):
            self.on_status(f"BusyBox skipped: bundled binary not found at {bundled_path}.")
            self.on_progress(1.0)
            return

        bin_dir = os.path.join(self.prefix, "bin")
        try:
            os.makedirs(bin_dir, exist_ok=True)
            dest = os.path.join(bin_dir, "busybox")

            self.on_status("Installing BusyBox...")
            shutil.copy2(bundled_path, dest)
            st = os.stat(dest)
            os.chmod(dest, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as exc:
            raise BusyboxError(f"Could not install busybox binary: {exc}") from exc

        self.on_status("Linking BusyBox applets...")
        applets = self._list_applets(dest)
        linked = self._link_applets(bin_dir, applets)

        skipped = len(applets) - linked
        self.on_status(
            f"BusyBox ready ({linked} new command(s) linked"
            + (f", {skipped} already present)" if skipped else ")")
        )
        self.on_progress(1.0)

    # -- internals --------------------------------------------------------

    def _bundled_busybox_path(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(here, "assets", "busybox", self.BUNDLED_FILENAME)

    @staticmethod
    def _list_applets(busybox_path: str):
        """Asks the binary itself which applets it supports (`busybox --list`)
        rather than hardcoding a list, so this stays correct across whatever
        BusyBox build/version actually gets bundled."""
        try:
            result = subprocess.run(
                [busybox_path, "--list"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            return []

    def _link_applets(self, bin_dir: str, applets) -> int:
        linked = 0
        total = len(applets) or 1
        for i, name in enumerate(applets):
            link_path = os.path.join(bin_dir, name)
            # os.path.lexists (not exists) so we don't clobber even a
            # dangling symlink left over from some earlier partial state -
            # "already present" always wins over BusyBox's version.
            if not os.path.lexists(link_path):
                try:
                    os.symlink("busybox", link_path)
                    linked += 1
                except OSError:
                    pass  # best-effort - skip anything we can't link and move on
            self.on_progress((i + 1) / total)
        return linked


class PtyCoreError(RuntimeError):
    pass


class PtyCore:
    """Thin ctypes wrapper around the C++ pty_core API."""

    def __init__(self):
        self.lib = CDLL(LIB_NAME)

        self.lib.pty_spawn.argtypes = [
            c_char_p, POINTER(c_char_p), c_char_p, POINTER(c_char_p),
            c_int, c_int, POINTER(c_int), POINTER(c_int),
        ]
        self.lib.pty_spawn.restype = c_int

        self.lib.pty_read.argtypes = [c_int, c_char_p, c_int]
        self.lib.pty_read.restype = c_int

        self.lib.pty_write.argtypes = [c_int, c_char_p, c_int]
        self.lib.pty_write.restype = c_int

        self.lib.pty_resize.argtypes = [c_int, c_int, c_int]
        self.lib.pty_resize.restype = c_int

        self.lib.pty_terminate.argtypes = [c_int, c_int]
        self.lib.pty_terminate.restype = c_int

        self.lib.pty_is_alive.argtypes = [c_int]
        self.lib.pty_is_alive.restype = c_int

        self.master_fd = None
        self.pid = None

    @staticmethod
    def _to_c_array(strings):
        arr = (c_char_p * (len(strings) + 1))()
        for i, s in enumerate(strings):
            arr[i] = s.encode("utf-8")
        arr[len(strings)] = None
        return arr

    def spawn_shell(self, rows=24, cols=80):
        shell_path = f"{PREFIX}/bin/bash"
        if not os.path.exists(shell_path):
            shell_path = f"{PREFIX}/bin/sh"
        if not os.path.exists(shell_path):
            raise PtyCoreError(
                f"No shell found at {PREFIX}/bin/bash or /sh. "
                "Bootstrap your PREFIX filesystem before starting the terminal."
            )

        argv = self._to_c_array([shell_path, "-l"])

        env_list = [
            f"PREFIX={PREFIX}",
            f"HOME={HOME}",
            f"PATH={PREFIX}/bin:/system/bin",
            f"LD_LIBRARY_PATH={PREFIX}/lib",
            f"TMPDIR={PREFIX}/tmp",
            "TERM=xterm-256color",
            "LANG=en_US.UTF-8",
        ]
        envp = self._to_c_array(env_list)

        out_fd = c_int(-1)
        out_pid = c_int(-1)

        ret = self.lib.pty_spawn(
            shell_path.encode("utf-8"),
            argv,
            HOME.encode("utf-8"),
            envp,
            rows, cols,
            byref(out_fd), byref(out_pid),
        )
        if ret != 0:
            raise PtyCoreError("pty_spawn() failed - check adb logcat for details")

        self.master_fd = out_fd.value
        self.pid = out_pid.value
        return self.master_fd, self.pid

    def read(self, size=4096) -> bytes:
        buf = create_string_buffer(size)
        n = self.lib.pty_read(self.master_fd, buf, size)
        if n <= 0:
            return b""
        return buf.raw[:n]

    def write(self, data: bytes) -> int:
        return self.lib.pty_write(self.master_fd, data, len(data))

    def resize(self, rows, cols) -> int:
        return self.lib.pty_resize(self.master_fd, rows, cols)

    def is_alive(self) -> bool:
        if self.pid is None:
            return False
        return self.lib.pty_is_alive(self.pid) == 1

    def terminate(self):
        if self.master_fd is not None:
            self.lib.pty_terminate(self.master_fd, self.pid or -1)
            self.master_fd = None
            self.pid = None


FONT_DOWNLOAD_URL = "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Regular.ttf"
FONT_FILENAME = "JetBrainsMono-Regular.ttf"


def _find_bundled_or_system_font():
    """Looks for a monospace TTF under python/assets/fonts/ first, then a
    couple of common on-device system paths. Does NOT download anything -
    see _ensure_monospace_font() for that. Returns None if nothing is found."""
    here = os.path.dirname(os.path.abspath(__file__))
    bundled_dir = os.path.join(here, "assets", "fonts")
    if os.path.isdir(bundled_dir):
        for fname in sorted(os.listdir(bundled_dir)):
            if fname.lower().endswith(".ttf"):
                return os.path.join(bundled_dir, fname)

    for candidate in (
        "/system/fonts/DroidSansMono.ttf",
        "/system/fonts/RobotoMono-Regular.ttf",
    ):
        if os.path.exists(candidate):
            return candidate

    return None


def _ensure_monospace_font(on_status=None):
    """Returns a path to a usable monospace TTF. If nothing bundled/on-device
    is found, downloads JetBrains Mono (SIL OFL-1.1, free for any use)
    straight from its GitHub repo into assets/fonts/ - same "check once,
    fetch once, cache on disk" pattern as BootstrapManager. Returns None
    (caller falls back to Kivy's default, non-monospace font) if both the
    on-device search and the download fail."""
    status = on_status or (lambda msg: None)

    existing = _find_bundled_or_system_font()
    if existing:
        return existing

    here = os.path.dirname(os.path.abspath(__file__))
    fonts_dir = os.path.join(here, "assets", "fonts")
    try:
        os.makedirs(fonts_dir, exist_ok=True)
    except OSError as exc:
        status(f"Could not create fonts directory ({exc}), using default font.")
        return None

    dest = os.path.join(fonts_dir, FONT_FILENAME)
    status("No monospace font found - downloading JetBrains Mono...")
    try:
        req = urllib.request.Request(FONT_DOWNLOAD_URL, headers={"User-Agent": "boffin-wayland"})
        with urllib.request.urlopen(req, timeout=60, context=_https_context()) as resp:
            data = resp.read()
        tmp_path = dest + ".part"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, dest)
        status("Monospace font ready.")
        return dest
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        status(f"Font download failed ({exc}) - falling back to the default font.")
        return None


class TerminalView(Widget):
    """
    The VT100-aware terminal surface.

    Rendering: canvas-based, not a markup Label. Each screen row owns one
    kivy.graphics.InstructionGroup; within a row, consecutive cells sharing
    the same (fg, bg, bold, underline, reverse) are merged into a single
    "run" and drawn as: an optional Color+Rectangle for the background,
    then a Color+Rectangle using a small cached glyph-shape texture (tinted
    by the preceding Color, the same technique kivy.uix.label uses
    internally) for the foreground text, then an optional thin Rectangle
    for underline. This is what makes per-cell BACKGROUND colors and
    reverse video (nano's status bar, htop's highlighted rows) render
    correctly, which a single markup Label can't do.

    Selective redraw: Screen.pop_dirty() reports exactly which row indices
    changed plus a separate cursor-only flag. TerminalView only rebuilds
    the InstructionGroups for rows that changed - moving the cursor or
    editing one line does not touch the other rows' cached instructions.

    Scrollback: mouse wheel / touch-drag calls Screen.scroll_view(), which
    is a no-op while an app is using the alternate screen buffer
    (nano/vim/htop), matching real terminal behavior. Typing anything
    snaps the view back to live output, also matching real terminals.

    Performance: keystrokes write straight to the PTY, independent of
    rendering. The PTY reader thread only feeds the parser and lets Screen
    track dirty state (cheap); the actual redraw runs on a throttled
    Clock.schedule_interval (REDRAW_HZ) and skips entirely when nothing
    is dirty.
    """

    REDRAW_HZ = 20
    TEXTURE_CACHE_MAX = 800
    SCROLL_WHEEL_LINES = 3

    KEY_ESCAPES = {
        "enter": b"\r",
        "backspace": b"\x7f",
        "tab": b"\t",
        "escape": b"\x1b",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "delete": b"\x1b[3~",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
    }

    def __init__(self, pty: "PtyCore", font_name=None, font_size=14, **kwargs):
        super().__init__(**kwargs)
        self.pty = pty
        self.font_size = font_size
        self.font_name = font_name or _find_bundled_or_system_font()

        self.screen = Screen(rows=24, cols=80)
        self.parser = VTParser(self.screen)

        self._char_w, self._char_h = self._measure_char_cell()
        self._texture_cache = OrderedDict()

        with self.canvas.before:
            Color(DEFAULT_BG[0] / 255, DEFAULT_BG[1] / 255, DEFAULT_BG[2] / 255, 1)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)

        self._row_groups = []  # one InstructionGroup per visible screen row

        self._cursor_color = Color(0.8, 0.8, 0.8, 0.0)
        self._cursor_rect = Rectangle(pos=(0, 0), size=(self._char_w, self._char_h))
        self.canvas.after.add(self._cursor_color)
        self.canvas.after.add(self._cursor_rect)

        self._running = False
        self._reader_thread = None
        self._redraw_ev = None
        self._keyboard = None

        self.bind(pos=self._sync_bg_rect, size=self._sync_bg_rect)
        self.bind(size=self._on_resize)

    # -- font metrics -----------------------------------------------------

    def _measure_char_cell(self):
        probe = CoreLabel(text="M", font_name=self.font_name, font_size=self.font_size)
        probe.refresh()
        w, h = probe.texture.size
        return max(1, w), max(1, int(h * 1.05))

    def _get_texture(self, text, bold):
        key = (text, bold)
        tex = self._texture_cache.get(key)
        if tex is not None:
            self._texture_cache.move_to_end(key)
            return tex
        label = CoreLabel(text=text, font_name=self.font_name, font_size=self.font_size, bold=bold)
        label.refresh()
        tex = label.texture
        self._texture_cache[key] = tex
        if len(self._texture_cache) > self.TEXTURE_CACHE_MAX:
            self._texture_cache.popitem(last=False)
        return tex

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        rows, cols = self._grid_dims_for_size(self.size)
        self.screen.resize(rows, cols)
        self._ensure_row_groups(rows)

        try:
            self.pty.spawn_shell(rows=rows, cols=cols)
        except PtyCoreError as exc:
            for ch in f"\r\n[Boffin-Wayland] {exc}\r\n":
                self.parser.feed(ch.encode("utf-8"))
            self._redraw(0)
            return

        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        self._redraw_ev = Clock.schedule_interval(self._redraw, 1.0 / self.REDRAW_HZ)
        self._request_keyboard()

    def stop(self):
        self._running = False
        if self._redraw_ev:
            self._redraw_ev.cancel()
        self._release_keyboard()
        self.pty.terminate()

    # -- PTY reading (background thread) ------------------------------------

    def _read_loop(self):
        while self._running:
            data = self.pty.read()
            if not data:
                if not self.pty.is_alive():
                    self._running = False
                continue
            self.parser.feed(data)  # mutates Screen; marks specific rows/cursor dirty

    # -- canvas row management -----------------------------------------------

    def _ensure_row_groups(self, count):
        while len(self._row_groups) < count:
            g = InstructionGroup()
            self.canvas.add(g)
            self._row_groups.append(g)
        while len(self._row_groups) > count:
            g = self._row_groups.pop()
            self.canvas.remove(g)

    def _render_row(self, group, cells, y):
        group.clear()
        n = len(cells)
        col = 0
        while col < n:
            cell = cells[col]
            fg, bg = cell.fg, cell.bg
            if cell.reverse:
                fg, bg = (bg or DEFAULT_BG), (fg or DEFAULT_FG)
            bold, underline = cell.bold, cell.underline

            run_start = col
            run_text = cell.ch or " "
            col += 1
            while col < n:
                c2 = cells[col]
                fg2, bg2 = c2.fg, c2.bg
                if c2.reverse:
                    fg2, bg2 = (bg2 or DEFAULT_BG), (fg2 or DEFAULT_FG)
                if (fg2, bg2, c2.bold, c2.underline) != (fg, bg, bold, underline):
                    break
                run_text += (c2.ch or " ")
                col += 1

            run_len = col - run_start
            run_w = run_len * self._char_w
            rx = self.x + run_start * self._char_w

            if bg is not None:
                group.add(Color(bg[0] / 255, bg[1] / 255, bg[2] / 255, 1))
                group.add(Rectangle(pos=(rx, y), size=(run_w, self._char_h)))

            fg_rgb = fg or DEFAULT_FG
            tex = self._get_texture(run_text, bold)
            group.add(Color(fg_rgb[0] / 255, fg_rgb[1] / 255, fg_rgb[2] / 255, 1))
            group.add(Rectangle(texture=tex, pos=(rx, y), size=(run_w, self._char_h)))

            if underline:
                group.add(Color(fg_rgb[0] / 255, fg_rgb[1] / 255, fg_rgb[2] / 255, 1))
                group.add(Rectangle(pos=(rx, y + 1), size=(run_w, max(1, int(self._char_h * 0.06)))))

    # -- redraw (throttled, main thread, selective) --------------------------

    def _redraw(self, _dt):
        if not self.screen.is_dirty():
            return
        dirty = self.screen.pop_dirty()
        rows, cur_row, cur_col, cursor_visible, scroll_offset = self.screen.get_visible_rows()

        if scroll_offset != 0:
            # Scrolled into history: row indices in the visible window don't
            # map 1:1 to live grid row indices, so any change repaints the
            # whole window. This only happens while the user is browsing
            # scrollback, not while actively typing at the live prompt.
            rows_to_redraw = range(len(rows)) if (dirty["all"] or dirty["rows"] or dirty["view"]) else []
        elif dirty["all"]:
            rows_to_redraw = range(len(rows))
        else:
            rows_to_redraw = sorted(r for r in dirty["rows"] if r < len(rows))

        top_y = self.y + self.height - self._char_h
        for r in rows_to_redraw:
            if r >= len(self._row_groups):
                continue
            self._render_row(self._row_groups[r], rows[r], top_y - r * self._char_h)

        if cursor_visible:
            self._cursor_color.a = 0.55
            self._cursor_rect.pos = (self.x + cur_col * self._char_w, top_y - cur_row * self._char_h)
            self._cursor_rect.size = (self._char_w, self._char_h)
        else:
            self._cursor_color.a = 0.0

    def _sync_bg_rect(self, *_args):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    # -- resize -> reflow screen + tell the PTY (SIGWINCH-equivalent) -------

    def _grid_dims_for_size(self, size):
        w, h = size
        cols = max(10, int(w // self._char_w))
        rows = max(4, int(h // self._char_h))
        return rows, cols

    def _on_resize(self, *_args):
        if not self._running:
            return
        rows, cols = self._grid_dims_for_size(self.size)
        if (rows, cols) != (self.screen.rows, self.screen.cols):
            self.screen.resize(rows, cols)
            self._ensure_row_groups(rows)
            self.pty.resize(rows, cols)

    # -- scrolling (mouse wheel + touch drag) --------------------------------

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        self._request_keyboard()

        if touch.is_mouse_scrolling:
            if touch.button == "scrollup":
                self.screen.scroll_view(self.SCROLL_WHEEL_LINES)
            elif touch.button == "scrolldown":
                self.screen.scroll_view(-self.SCROLL_WHEEL_LINES)
            return True

        touch.grab(self)
        touch.ud["boffin_start_y"] = touch.y
        touch.ud["boffin_scrolled_lines"] = 0
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is not self:
            return super().on_touch_move(touch)
        start_y = touch.ud.get("boffin_start_y", touch.y)
        # Kivy's y-axis is bottom-up (y=0 at the bottom of the window), so a
        # physical downward finger drag DECREASES touch.y. We want that
        # gesture (drag down) to reveal older content, matching typical
        # scrollable-list / pull-down behavior, so we negate dy here.
        dy = start_y - touch.y
        lines = int(dy // self._char_h)
        prev_lines = touch.ud.get("boffin_scrolled_lines", 0)
        if lines != prev_lines:
            self.screen.scroll_view(lines - prev_lines)
            touch.ud["boffin_scrolled_lines"] = lines
        return True

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            return True
        return super().on_touch_up(touch)

    # -- keyboard input -------------------------------------------------

    def _request_keyboard(self):
        if self._keyboard is not None:
            return
        self._keyboard = Window.request_keyboard(self._on_keyboard_closed, self, "text")
        self._keyboard.bind(on_key_down=self._on_key_down)
        Window.bind(on_textinput=self._on_textinput)

    def _release_keyboard(self):
        if self._keyboard is not None:
            self._keyboard.unbind(on_key_down=self._on_key_down)
            self._keyboard.release()
            self._keyboard = None
        Window.unbind(on_textinput=self._on_textinput)

    def _on_keyboard_closed(self):
        self._keyboard = None

    def _on_key_down(self, _keyboard, keycode, _text, modifiers):
        key_name = keycode[1]

        if "ctrl" in modifiers and len(key_name) == 1 and key_name.isalpha():
            self.screen.scroll_to_bottom()
            self.pty.write(bytes([ord(key_name.upper()) - 64]))
            return True

        escape = self.KEY_ESCAPES.get(key_name)
        if escape:
            self.screen.scroll_to_bottom()
            self.pty.write(escape)
            return True

        return False  # let on_textinput handle plain printable characters

    def _on_textinput(self, _window, text):
        if self._keyboard is None:
            return
        self.screen.scroll_to_bottom()
        self.pty.write(text.encode("utf-8"))







class LorieBridge:
    """
    Thin pyjnius glue that attaches/detaches the native LorieSurfaceView
    (Java: android_src/com/boffin/wayland/LorieSurfaceView.java; native:
    cpp/lorie_bridge.cpp) into the Activity's view hierarchy, layered on
    top of Kivy's own view.

    FOUNDATION STEP: this is the X11-display architecture groundwork, not
    an X server. Calling show() attaches the real native SurfaceView; if
    you see a solid dark-slate rectangle appear, that's lorie_bridge.cpp's
    smoke-test fill confirming the whole Java -> JNI -> ANativeWindow ->
    hardware buffer -> SurfaceFlinger pipeline actually works. There is no
    X server rendering into it yet - that's the next milestone.

    Only meaningful on Android (needs pyjnius + the compiled Java class);
    available() lets the caller detect that and show a sensible message
    instead of crashing when testing main.py's Kivy logic on a desktop.
    """

    def __init__(self):
        self._view = None

    @staticmethod
    def available() -> bool:
        try:
            import jnius  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _make_runnable(fn):
        from jnius import PythonJavaClass, java_method

        class _Runnable(PythonJavaClass):
            __javainterfaces__ = ["java/lang/Runnable"]
            __javacontext__ = "app"

            def __init__(self, target):
                super().__init__()
                self._target = target

            @java_method("()V")
            def run(self):
                self._target()

        return _Runnable(fn)

    def show(self):
        from jnius import autoclass

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        LorieSurfaceView = autoclass("com.boffin.wayland.LorieSurfaceView")
        LayoutParams = autoclass("android.widget.FrameLayout$LayoutParams")

        def _attach():
            view = LorieSurfaceView(activity)
            params = LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT)
            activity.addContentView(view, params)
            view.requestFocus()
            self._view = view

        activity.runOnUiThread(self._make_runnable(_attach))

    def hide(self):
        if self._view is None:
            return
        from jnius import autoclass

        view = self._view
        self._view = None

        def _detach():
            parent = view.getParent()
            if parent is not None:
                parent.removeView(view)

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        activity.runOnUiThread(self._make_runnable(_detach))


class BoffinWaylandApp(App):
    """
    Startup sequence (requirement #5): the bootstrap must succeed *before*
    the PTY core is ever spawned. build()/on_start() only set up the
    "initializing" screen; _run_bootstrap() (on a background thread) does
    the actual work and only calls _launch_terminal() on success.
    """

    def build(self):
        self.title = "Boffin-Wayland"
        self.term = None
        self.lorie_bridge = LorieBridge()
        self._x11_shown = False

        self.root_layout = BoxLayout(orientation="vertical", padding=16, spacing=10)

        self.status_label = Label(
            text="Starting Boffin-Wayland...",
            size_hint_y=None,
            height=32,
            color=(0.9, 0.9, 0.9, 1),
        )
        self.progress_bar = ProgressBar(max=1.0, value=0.0, size_hint_y=None, height=18)
        self.log_view = TextInput(
            readonly=True,
            font_size="13sp",
            background_color=(0, 0, 0, 1),
            foreground_color=(0.6, 0.9, 0.6, 1),
        )

        self.root_layout.add_widget(self.status_label)
        self.root_layout.add_widget(self.progress_bar)
        self.root_layout.add_widget(self.log_view)

        return self.root_layout

    def on_start(self):
        threading.Thread(target=self._run_bootstrap, daemon=True).start()

    # -- bootstrap phase (runs on a background thread) ---------------------

    def _run_bootstrap(self):
        manager = BootstrapManager(
            PREFIX,
            on_status=self._on_bootstrap_status,
            on_progress=self._on_bootstrap_progress,
        )
        try:
            os.makedirs(HOME, exist_ok=True)
            os.makedirs(f"{PREFIX}/tmp", exist_ok=True)
            manager.ensure_bootstrap()
        except BootstrapError as exc:
            self._on_bootstrap_status(f"[ERROR] Bootstrap failed: {exc}")
            return

        busybox_manager = BusyboxManager(
            PREFIX,
            on_status=self._on_bootstrap_status,
            on_progress=self._on_bootstrap_progress,
        )
        try:
            busybox_manager.ensure_busybox()
        except BusyboxError as exc:
            # Not fatal - bash/sh from the Termux bootstrap already work on
            # their own; BusyBox just adds extra commands on top.
            self._on_bootstrap_status(f"[WARNING] BusyBox install failed: {exc} (continuing without it)")

        self.font_path = _ensure_monospace_font(on_status=self._on_bootstrap_status)

        Clock.schedule_once(lambda dt: self._launch_terminal())

    def _on_bootstrap_status(self, message: str):
        def _update(_dt):
            self.status_label.text = message
            self.log_view.text += message + "\n"
            self.log_view.cursor = (0, len(self.log_view._lines) - 1)
        Clock.schedule_once(_update)

    def _on_bootstrap_progress(self, fraction: float):
        def _update(_dt):
            self.progress_bar.value = fraction
        Clock.schedule_once(_update)

    # -- terminal phase (main thread only) ----------------------------------

    def _launch_terminal(self):
        self.root_layout.clear_widgets()

        top_bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=44)
        self.x11_toggle_btn = Button(
            text="X11 Display (experimental, foundation only) - tap to show",
        )
        self.x11_toggle_btn.bind(on_press=self._toggle_x11_display)
        top_bar.add_widget(self.x11_toggle_btn)
        self.root_layout.add_widget(top_bar)

        pty = PtyCore()
        self.term = TerminalView(pty, font_name=getattr(self, "font_path", None))
        self.root_layout.add_widget(self.term)
        self.term.start()

    def _toggle_x11_display(self, _instance):
        if not self.lorie_bridge.available():
            self.x11_toggle_btn.text = "X11 Display unavailable (pyjnius/Android only)"
            return

        if self._x11_shown:
            self.lorie_bridge.hide()
            self.x11_toggle_btn.text = "X11 Display (experimental, foundation only) - tap to show"
        else:
            # See LorieBridge.show()'s docstring: this attaches the real
            # native SurfaceView. A solid dark-slate rectangle appearing on
            # top of the terminal confirms the Java -> JNI -> ANativeWindow
            # pipeline works - there's no X server behind it yet.
            self.lorie_bridge.show()
            self.x11_toggle_btn.text = "X11 Display (foundation smoke test) - tap to hide"
        self._x11_shown = not self._x11_shown

    def on_stop(self):
        if self._x11_shown:
            self.lorie_bridge.hide()
        if self.term is not None:
            self.term.stop()


if __name__ == "__main__":
    BoffinWaylandApp().run()
