#include "lorie_bridge.h"

#include <jni.h>
#include <android/native_window_jni.h>
#include <android/log.h>
#include <mutex>
#include <cstdint>

#define LOG_TAG "BoffinLorieBridge"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

std::mutex g_mutex;
ANativeWindow* g_window = nullptr;
int g_width = 0;
int g_height = 0;
boffin_lorie_window_event_cb g_event_cb = nullptr;
void* g_event_cb_user_data = nullptr;

void notify_event_locked() {
    if (g_event_cb) {
        g_event_cb(g_window, g_width, g_height, g_event_cb_user_data);
    }
}

// Foundation-step smoke test only: fills the whole surface with a solid
// color so we can visually confirm - before any X server exists - that the
// Java -> JNI -> ANativeWindow pipeline actually works. This function goes
// away once the real X server DDX renders into g_window instead.
void fill_solid_color_locked(uint8_t r, uint8_t g, uint8_t b) {
    if (!g_window) return;

    if (ANativeWindow_setBuffersGeometry(g_window, 0, 0, WINDOW_FORMAT_RGBA_8888) != 0) {
        LOGE("ANativeWindow_setBuffersGeometry failed");
        return;
    }

    ANativeWindow_Buffer buffer;
    if (ANativeWindow_lock(g_window, &buffer, nullptr) != 0) {
        LOGE("ANativeWindow_lock failed");
        return;
    }

    auto* pixels = static_cast<uint8_t*>(buffer.bits);
    for (int y = 0; y < buffer.height; y++) {
        uint8_t* row = pixels + static_cast<size_t>(y) * buffer.stride * 4;
        for (int x = 0; x < buffer.width; x++) {
            row[x * 4 + 0] = r;
            row[x * 4 + 1] = g;
            row[x * 4 + 2] = b;
            row[x * 4 + 3] = 0xFF;
        }
    }

    ANativeWindow_unlockAndPost(g_window);
}

void replace_window_locked(ANativeWindow* new_window, int width, int height) {
    if (g_window && g_window != new_window) {
        ANativeWindow_release(g_window);
    }
    g_window = new_window;
    g_width = width;
    g_height = height;
}

} // namespace

// ---------------------------------------------------------------------------
// Public C API (for the future X server DDX)
// ---------------------------------------------------------------------------

ANativeWindow* boffin_lorie_get_window(int* out_width, int* out_height) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (out_width) *out_width = g_width;
    if (out_height) *out_height = g_height;
    return g_window;
}

void boffin_lorie_set_window_event_callback(boffin_lorie_window_event_cb cb, void* user_data) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_event_cb = cb;
    g_event_cb_user_data = user_data;
}

// ---------------------------------------------------------------------------
// JNI entry points called from com.boffin.wayland.LorieSurfaceView
// ---------------------------------------------------------------------------

extern "C" {

JNIEXPORT void JNICALL
Java_com_boffin_wayland_LorieSurfaceView_nativeSurfaceCreated(
    JNIEnv* env, jobject /*thiz*/, jobject surface) {
    std::lock_guard<std::mutex> lock(g_mutex);

    ANativeWindow* window = ANativeWindow_fromSurface(env, surface);
    if (!window) {
        LOGE("ANativeWindow_fromSurface returned null in nativeSurfaceCreated");
        return;
    }
    replace_window_locked(window, ANativeWindow_getWidth(window), ANativeWindow_getHeight(window));
    LOGI("Surface created: %dx%d", g_width, g_height);

    // Dark slate blue - visually distinct from Kivy's own background and
    // from plain black, so seeing it appear is an unambiguous confirmation
    // that this native surface (not Kivy's GL surface) is what's on screen.
    fill_solid_color_locked(0x2E, 0x34, 0x40);
    notify_event_locked();
}

JNIEXPORT void JNICALL
Java_com_boffin_wayland_LorieSurfaceView_nativeSurfaceChanged(
    JNIEnv* env, jobject /*thiz*/, jobject surface, jint /*format*/, jint width, jint height) {
    std::lock_guard<std::mutex> lock(g_mutex);

    ANativeWindow* window = ANativeWindow_fromSurface(env, surface);
    if (!window) {
        LOGE("ANativeWindow_fromSurface returned null in nativeSurfaceChanged");
        return;
    }
    replace_window_locked(window, width, height);
    LOGI("Surface changed: %dx%d", g_width, g_height);

    fill_solid_color_locked(0x2E, 0x34, 0x40);
    notify_event_locked();
}

JNIEXPORT void JNICALL
Java_com_boffin_wayland_LorieSurfaceView_nativeSurfaceDestroyed(
    JNIEnv* /*env*/, jobject /*thiz*/) {
    std::lock_guard<std::mutex> lock(g_mutex);
    replace_window_locked(nullptr, 0, 0);
    LOGI("Surface destroyed");
    notify_event_locked();
}

JNIEXPORT void JNICALL
Java_com_boffin_wayland_LorieSurfaceView_nativeTouchEvent(
    JNIEnv* /*env*/, jobject /*thiz*/, jint action, jfloat x, jfloat y, jint pointer_id) {
    // Foundation step: just log. Will be wired into the X server's input
    // subsystem once that exists (see README roadmap).
    LOGI("touch action=%d pointer=%d pos=(%.1f, %.1f)", action, pointer_id, x, y);
}

JNIEXPORT void JNICALL
Java_com_boffin_wayland_LorieSurfaceView_nativeKeyEvent(
    JNIEnv* /*env*/, jobject /*thiz*/, jint action, jint key_code, jint unicode_char, jint meta_state) {
    LOGI("key action=%d keyCode=%d unicode=%d meta=%d", action, key_code, unicode_char, meta_state);
}

} // extern "C"
