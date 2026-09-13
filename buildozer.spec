[app]

title = Boffin-Wayland
package.name = wayland
package.domain = com.boffin

source.dir = python
source.include_exts = py,png,jpg,kv,atlas,ttf
# busybox-aarch64 has no file extension, so include_exts (extension-based)
# won't catch it - include_patterns (glob-based) is needed for that.
source.include_patterns = assets/busybox/*, assets/bootstrap/*
version = 0.1.0

requirements = python3==3.11.9,kivy==2.3.0,certifi

# THE ACTUAL FIX: buildozer does NOT use whatever python-for-android was
# `pip install`-ed in CI - it clones its own copy from GitHub, and defaults
# to the `master` branch whenever p4a.branch is unset/commented out (see
# https://github.com/kivy/buildozer/blob/master/buildozer/default.spec).
# master moves forward continuously; by now it targets Python 3.14, which
# has no prebuilt wheels for kivy/pyjnius/android, causing exactly the
# SDL2/wheel build failures we hit. v2024.01.21 is the p4a release that
# explicitly bumped its Kivy compatibility to 2.3.0 - pin to that tag
# instead of trusting the "defaults to master" behavior.
p4a.branch = v2024.01.21

orientation = all
fullscreen = 0

# --- Android specifics --------------------------------------------------

# forkpty()/openpty() require API 23+ on bionic - keep this in sync with
# MIN_API in cpp/build_native.sh
android.minapi = 24
# STATUS UPDATE: android.api is back up to 34. The temporary fix (pinning
# this to 28 - see git history for the full original explanation of why
# Android 10+'s targetSdkVersion>=29 W^X restriction blocks bash from
# execve()-ing out of PREFIX) has been superseded by the real fix:
# system_linker_exec, implemented across:
#   - cpp/system_linker_helpers.h   (shared ELF/shebang analysis logic)
#   - cpp/pty_core.cpp               (redirects the initial bash spawn)
#   - cpp/exec_shim.cpp              (LD_PRELOAD-ed into bash so every
#                                      command bash itself runs afterward -
#                                      ls, cat, nano, python3, etc. - gets
#                                      the same redirect, not just bash's
#                                      own startup)
# The core mechanism (execve() the system's own dynamic linker with the
# real target as its first argument) was verified end-to-end on a Linux
# host standing in for Android's linker64 - see the project's test notes.
# It has NOT yet been verified against Android's actual bionic linker64 on
# a real device, which is a meaningfully different environment (SELinux
# policy, APEX linker paths, bionic-specific linker behavior).
#
# IF THIS BUILD RUNS BUT BASH FAILS TO START AGAIN: that means something
# about real device/bionic behavior differs from what was tested here.
# Bisect by temporarily setting android.api back to 28 - if that fixes it,
# the problem is specifically in the system_linker_exec path (check logcat
# for "BoffinSystemLinkerExec" tag messages, which log every redirect
# decision and failure reason); if 28 *also* fails now, something else
# changed (e.g. the new exec_shim.so isn't being packaged/loaded).
android.api = 34
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

# Isolated package name - guarantees zero collision with a real Termux /
# Termux:X11 install on the same device (different data dir, different
# process, different signing identity).
android.permissions = INTERNET

# Prebuilt native libraries produced by cpp/build_native.sh
# (run that script BEFORE `buildozer android debug`)
android.add_libs_arm64_v8a = libs/arm64-v8a/libptycore.so, libs/arm64-v8a/liblorie_bridge.so, libs/arm64-v8a/libexec_shim.so
android.add_libs_armeabi_v7a = libs/armeabi-v7a/libptycore.so, libs/armeabi-v7a/liblorie_bridge.so, libs/armeabi-v7a/libexec_shim.so

# Extra Java source (com.boffin.wayland.LorieSurfaceView - the native X11
# display surface, see android_src/ and cpp/lorie_bridge.cpp). NOTE: the
# exact directory layout p4a expects here has shifted between buildozer/
# python-for-android versions (old ant-based builds vs newer gradle-based
# ones expect different package-path nesting) - this is the single most
# version-fragile line in this whole spec. If the build fails to pick up
# LorieSurfaceView, check your installed p4a's docs for "add_src" /
# "add_jars" and adjust this path/layout accordingly.
android.add_src = android_src

# Keep the app foreground-only for now; a real terminal usually wants a
# persistent foreground service so the shell isn't killed when backgrounded -
# add that as a follow-up once the basic PTY bridge is confirmed working.
android.allow_backup = 0

[buildozer]
log_level = 2
warn_on_root = 1
