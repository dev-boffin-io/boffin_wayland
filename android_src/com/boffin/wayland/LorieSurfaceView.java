package com.boffin.wayland;

import android.content.Context;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;

/**
 * Native rendering surface for Boffin-Wayland's future X11 display.
 *
 * This is a plain Java View - deliberately NOT driven through Kivy/pyjnius
 * callbacks - because SurfaceHolder lifecycle and touch/key delivery are
 * latency-sensitive and need to reach native code (ANativeWindow) as
 * directly as possible. Python's only job (see LorieBridge in main.py) is
 * to instantiate this view via pyjnius and attach it to the Activity's
 * view hierarchy; from that point on this class talks to native code
 * (cpp/lorie_bridge.cpp) on its own, on the UI thread.
 *
 * FOUNDATION STEP: the native side currently only proves the
 * Java -> JNI -> ANativeWindow pipeline works end to end (it fills the
 * surface with a solid color as soon as it's created/resized, and logs
 * touch/key events via logcat). There is no X server rendering here yet -
 * that is the next milestone once this bridge is confirmed working on a
 * real device.
 */
public class LorieSurfaceView extends SurfaceView implements SurfaceHolder.Callback {

    static {
        System.loadLibrary("lorie_bridge");
    }

    public LorieSurfaceView(Context context) {
        super(context);
        init();
    }

    public LorieSurfaceView(Context context, AttributeSet attrs) {
        super(context, attrs);
        init();
    }

    private void init() {
        getHolder().addCallback(this);
        setFocusable(true);
        setFocusableInTouchMode(true);
    }

    // -- SurfaceHolder.Callback ----------------------------------------

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        nativeSurfaceCreated(holder.getSurface());
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        nativeSurfaceChanged(holder.getSurface(), format, width, height);
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        nativeSurfaceDestroyed();
    }

    // -- input forwarding -------------------------------------------------
    // Foundation step: these reach native code and are logged there. Once
    // the X server DDX exists, nativeTouchEvent/nativeKeyEvent will feed
    // its input subsystem instead (mirroring how a real X server receives
    // pointer/keyboard events).

    @Override
    public boolean onTouchEvent(MotionEvent event) {
        int action = event.getActionMasked();
        int pointerIndex = event.getActionIndex();
        int pointerId = event.getPointerId(pointerIndex);
        nativeTouchEvent(action, event.getX(pointerIndex), event.getY(pointerIndex), pointerId);
        return true;
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        nativeKeyEvent(KeyEvent.ACTION_DOWN, keyCode, event.getUnicodeChar(), event.getMetaState());
        return true;
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        nativeKeyEvent(KeyEvent.ACTION_UP, keyCode, event.getUnicodeChar(), event.getMetaState());
        return true;
    }

    // -- native methods, implemented in cpp/lorie_bridge.cpp ------------

    private native void nativeSurfaceCreated(Surface surface);
    private native void nativeSurfaceChanged(Surface surface, int format, int width, int height);
    private native void nativeSurfaceDestroyed();
    private native void nativeTouchEvent(int action, float x, float y, int pointerId);
    private native void nativeKeyEvent(int action, int keyCode, int unicodeChar, int metaState);
}
