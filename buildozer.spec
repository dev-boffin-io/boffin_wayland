[app]

title = Boffin-Wayland
package.name = wayland
package.domain = com.boffin

source.dir = python
source.include_exts = py,png,jpg,kv,atlas
version = 0.1.0

requirements = python3,kivy==2.3.0

orientation = all
fullscreen = 0

# --- Android specifics --------------------------------------------------

# forkpty()/openpty() require API 23+ on bionic - keep this in sync with
# MIN_API in cpp/build_native.sh
android.minapi = 24
android.api = 34
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

# Isolated package name - guarantees zero collision with a real Termux /
# Termux:X11 install on the same device (different data dir, different
# process, different signing identity).
android.permissions = INTERNET

# Prebuilt native libraries produced by cpp/build_native.sh
# (run that script BEFORE `buildozer android debug`)
android.add_libs_arm64_v8a = libs/arm64-v8a/libptycore.so, libs/arm64-v8a/liblorie_bridge.so
android.add_libs_armeabi_v7a = libs/armeabi-v7a/libptycore.so, libs/armeabi-v7a/liblorie_bridge.so

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
python_version = 3.11
