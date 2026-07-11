#ifndef BOFFIN_LORIE_BRIDGE_H
#define BOFFIN_LORIE_BRIDGE_H

#include <android/native_window.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * lorie_bridge - the native half of the Java LorieSurfaceView bridge.
 *
 * FOUNDATION STEP: right now this just captures the ANativeWindow behind
 * LorieSurfaceView and fills it with a solid color as a smoke test (proves
 * Java -> JNI -> ANativeWindow -> hardware buffer -> SurfaceFlinger works
 * end to end before any X server exists). The functions below are the
 * public API a future X server DDX will call instead of the smoke test.
 */

/* Returns the ANativeWindow currently backing the on-screen X11 display
 * surface, or NULL if no LorieSurfaceView is attached/visible right now.
 * Does NOT transfer ownership - never call ANativeWindow_release() on the
 * pointer returned here; lorie_bridge.cpp owns its lifecycle. Safe to call
 * from any thread. */
ANativeWindow* boffin_lorie_get_window(int* out_width, int* out_height);

/* Callback signature for window lifecycle notifications: called with the
 * new ANativeWindow (or NULL on destroy) and its dimensions whenever the
 * surface is created, resized, or destroyed. Intended for the future X
 * server DDX to know when to (re)start, resize, or tear down its rendering
 * target - it should not need to poll boffin_lorie_get_window(). */
typedef void (*boffin_lorie_window_event_cb)(ANativeWindow* window, int width, int height, void* user_data);

/* Registers (or clears, with cb=NULL) the window-event callback. Only one
 * callback is supported at a time. */
void boffin_lorie_set_window_event_callback(boffin_lorie_window_event_cb cb, void* user_data);

#ifdef __cplusplus
}
#endif

#endif /* BOFFIN_LORIE_BRIDGE_H */
