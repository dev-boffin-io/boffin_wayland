#!/usr/bin/env bash
# Cross-compiles libptycore.so and liblorie_bridge.so for every Android ABI
# using the NDK's CMake toolchain file, and copies the results into
# ../libs/<abi>/ so buildozer.spec can bundle them straight into the APK.
#
# Requirements:
#   - Android NDK installed (set ANDROID_NDK_HOME below or export it yourself)
#   - cmake + ninja on PATH
#
# Usage:
#   export ANDROID_NDK_HOME=/path/to/Android/Sdk/ndk/25.2.9519653
#   ./build_native.sh

set -euo pipefail

: "${ANDROID_NDK_HOME:?Set ANDROID_NDK_HOME to your NDK install path first}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_ROOT="${SCRIPT_DIR}/../libs"
MIN_API=23   # forkpty()/openpty() require API 23+ on bionic

ABIS=("arm64-v8a" "armeabi-v7a" "x86_64")
TARGETS=("ptycore" "lorie_bridge")

for ABI in "${ABIS[@]}"; do
    echo "== Building for ${ABI} =="
    BUILD_DIR="${SCRIPT_DIR}/build-${ABI}"
    rm -rf "${BUILD_DIR}"
    mkdir -p "${BUILD_DIR}"

    cmake -S "${SCRIPT_DIR}" -B "${BUILD_DIR}" -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE="${ANDROID_NDK_HOME}/build/cmake/android.toolchain.cmake" \
        -DANDROID_ABI="${ABI}" \
        -DANDROID_PLATFORM="android-${MIN_API}" \
        -DCMAKE_BUILD_TYPE=Release

    for TARGET in "${TARGETS[@]}"; do
        cmake --build "${BUILD_DIR}" --target "${TARGET}" -- -j"$(nproc)"
    done

    mkdir -p "${OUT_ROOT}/${ABI}"
    for TARGET in "${TARGETS[@]}"; do
        cp "${BUILD_DIR}/lib${TARGET}.so" "${OUT_ROOT}/${ABI}/lib${TARGET}.so"
        echo "-> ${OUT_ROOT}/${ABI}/lib${TARGET}.so"
    done
done

echo
echo "Done. Built for: ${ABIS[*]} (targets: ${TARGETS[*]})"
echo "Point buildozer.spec's android.add_libs_* keys at boffin_wayland/libs/<abi>/*.so"
