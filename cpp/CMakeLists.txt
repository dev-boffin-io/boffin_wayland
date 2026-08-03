cmake_minimum_required(VERSION 3.18)
project(boffin_wayland_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# -- ptycore: PTY/shell backend (see pty_core.h) -----------------------------

add_library(ptycore SHARED
    pty_core.cpp
)

target_compile_options(ptycore PRIVATE -Wall -Wextra -fPIC -fvisibility=default)

if(ANDROID)
    target_link_libraries(ptycore log)
endif()

# forkpty()/openpty() live in libutil on glibc but are in bionic's libc
# directly on Android, so no extra -lutil is needed here.

# -- lorie_bridge: native half of the future X11-display SurfaceView -------
# Foundation step only (see lorie_bridge.h/.cpp docstrings): captures the
# ANativeWindow behind com.boffin.wayland.LorieSurfaceView and smoke-tests
# it with a solid-color fill. This is what a future X server DDX will
# render into instead.

add_library(lorie_bridge SHARED
    lorie_bridge.cpp
)

target_compile_options(lorie_bridge PRIVATE -Wall -Wextra -fPIC -fvisibility=default)

if(ANDROID)
    # `android` provides ANativeWindow_fromSurface/lock/unlockAndPost etc.
    target_link_libraries(lorie_bridge log android)
endif()

# -- exec_shim: LD_PRELOAD workaround for Android 10+'s W^X restriction ----
# See system_linker_helpers.h and exec_shim.cpp's docstrings for the full
# explanation. Built as its own .so so it can be LD_PRELOAD-ed into bash
# (and everything bash spawns) independently of ptycore/lorie_bridge.

add_library(exec_shim SHARED
    exec_shim.cpp
)

target_compile_options(exec_shim PRIVATE -Wall -Wextra -fPIC -fvisibility=default)

if(ANDROID)
    # `log` for __android_log_print; `dl` for dlsym(RTLD_NEXT, ...) - on
    # some NDK/API combinations dl symbols are already in libc, but linking
    # it explicitly is harmless and keeps this portable across API levels.
    target_link_libraries(exec_shim log dl)
endif()
