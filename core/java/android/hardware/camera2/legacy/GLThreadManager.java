/*
 * Copyright (C) 2014 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package android.hardware.camera2.legacy;

import android.graphics.SurfaceTexture;
import android.hardware.camera2.impl.CameraDeviceImpl;
import android.os.ConditionVariable;
import android.os.Handler;
import android.os.Message;
import android.os.Process;
import android.util.Log;
import android.util.Pair;
import android.util.Size;
import android.view.Surface;

import java.util.Collection;

import static com.android.internal.util.Preconditions.*;

/**
 * GLThreadManager handles the thread used for rendering into the configured output surfaces.
 */
@SuppressWarnings("deprecation")
public class GLThreadManager {
    private final String TAG;
    private static final boolean DEBUG = false;

    private static final int MSG_NEW_CONFIGURATION = 1;
    private static final int MSG_NEW_FRAME = 2;
    private static final int MSG_CLEANUP = 3;
    private static final int MSG_DROP_FRAMES = 4;
    private static final int MSG_ALLOW_FRAMES = 5;
    private static final int MSG_NEW_RECORD_FRAME = 6;
    private static final int MSG_SET_RECORD_SOURCE = 7;
    private static final int MSG_CLEAR_UNPAIRED_TARGETS = 8;

    private CaptureCollector mCaptureCollector;

    private final CameraDeviceState mDeviceState;

    private final SurfaceTextureRenderer mTextureRenderer;

    private final RequestHandlerThread mGLHandlerThread;

    private final RequestThreadManager.FpsCounter mPrevCounter =
            new RequestThreadManager.FpsCounter("GL Preview Producer");

    /**
     * Container object for Configure messages.
     */
    private static class ConfigureHolder {
        public final ConditionVariable condition;
        public final Collection<Pair<Surface, Size>> surfaces;
        public final CaptureCollector collector;

        public ConfigureHolder(ConditionVariable condition, Collection<Pair<Surface,
                Size>> surfaces, CaptureCollector collector) {
            this.condition = condition;
            this.surfaces = surfaces;
            this.collector = collector;
        }
    }

    private final Handler.Callback mGLHandlerCb = new Handler.Callback() {
        private boolean mCleanup = false;
        private boolean mConfigured = false;
        private boolean mDroppingFrames = false;

        @SuppressWarnings("unchecked")
        @Override
        public boolean handleMessage(Message msg) {
            if (mCleanup) {
                return true;
            }
            try {
                switch (msg.what) {
                    case MSG_NEW_CONFIGURATION:
                        ConfigureHolder configure = (ConfigureHolder) msg.obj;
                        mTextureRenderer.cleanupEGLContext();
                        mTextureRenderer.configureSurfaces(configure.surfaces);
                        mCaptureCollector = checkNotNull(configure.collector);
                        configure.condition.open();
                        mConfigured = true;
                        break;
                    case MSG_NEW_FRAME:
                    case MSG_NEW_RECORD_FRAME:
                        if (mDroppingFrames) {
                            Log.w(TAG, "Ignoring frame.");
                            break;
                        }
                        if (DEBUG) {
                            mPrevCounter.countAndLog();
                        }
                        if (!mConfigured) {
                            Log.e(TAG, "Dropping frame, EGL context not configured!");
                        }
                        mTextureRenderer.drawIntoSurfaces(mCaptureCollector,
                                msg.what == MSG_NEW_RECORD_FRAME);
                        break;
                    case MSG_SET_RECORD_SOURCE:
                        mTextureRenderer.setRecordSource(msg.arg1 != 0);
                        break;
                    case MSG_CLEAR_UNPAIRED_TARGETS:
                        mTextureRenderer.clearUnpairedTargets();
                        break;
                    case MSG_CLEANUP:
                        mTextureRenderer.cleanupEGLContext();
                        mCleanup = true;
                        mConfigured = false;
                        break;
                    case MSG_DROP_FRAMES:
                        mDroppingFrames = true;
                        break;
                    case MSG_ALLOW_FRAMES:
                        mDroppingFrames = false;
                        break;
                    case RequestHandlerThread.MSG_POKE_IDLE_HANDLER:
                        // OK: Ignore message.
                        break;
                    default:
                        Log.e(TAG, "Unhandled message " + msg.what + " on GLThread.");
                        break;
                }
            } catch (Exception e) {
                Log.e(TAG, "Received exception on GL render thread: ", e);
                mDeviceState.setError(CameraDeviceImpl.CameraDeviceCallbacks.ERROR_CAMERA_DEVICE);
            }
            return true;
        }
    };

    /**
     * Create a new GL thread and renderer.
     *
     * @param cameraId the camera id for this thread.
     * @param facing direction the camera is facing.
     * @param state {@link CameraDeviceState} to use for error handling.
     */
    public GLThreadManager(int cameraId, int facing, CameraDeviceState state) {
        mTextureRenderer = new SurfaceTextureRenderer(facing);
        TAG = String.format("CameraDeviceGLThread-%d", cameraId);
        mGLHandlerThread = new RequestHandlerThread(TAG, Process.THREAD_PRIORITY_URGENT_DISPLAY,
                mGLHandlerCb);
        mDeviceState = state;
    }

    /**
     * Start the thread.
     *
     * <p>
     * This must be called before queueing new frames.
     * </p>
     */
    public void start() {
        mGLHandlerThread.start();
    }

    /**
     * Wait until the thread has started.
     */
    public void waitUntilStarted() {
        mGLHandlerThread.waitUntilStarted();
    }

    /**
     * Quit the thread.
     *
     * <p>
     * No further methods can be called after this.
     * </p>
     */
    public void quit() {
        Handler handler = mGLHandlerThread.getHandler();
        handler.sendMessageAtFrontOfQueue(handler.obtainMessage(MSG_CLEANUP));
        mGLHandlerThread.quitSafely();
        try {
            mGLHandlerThread.join();
        } catch (InterruptedException e) {
            Log.e(TAG, String.format("Thread %s (%d) interrupted while quitting.",
                    mGLHandlerThread.getName(), mGLHandlerThread.getId()));
        }
    }

    /**
     * Queue a new call to draw into the surfaces specified in the next available preview
     * request from the {@link CaptureCollector} passed to
     * {@link #setConfigurationAndWait(java.util.Collection, CaptureCollector)};
     */
    public void queueNewFrame() {
        Handler handler = mGLHandlerThread.getHandler();

        /**
         * Avoid queuing more than one new frame.  If we are not consuming faster than frames
         * are produced, drop frames rather than allowing the queue to back up.
         */
        if (!handler.hasMessages(MSG_NEW_FRAME)) {
            handler.sendMessage(handler.obtainMessage(MSG_NEW_FRAME));
        } else {
            Log.e(TAG, "GLThread dropping frame.  Not consuming frames quickly enough!");
        }
    }

    /**
     * Queue a draw of the next recording stream frame, as {@link #queueNewFrame()} does for
     * preview frames; at most one is queued at a time.
     */
    public void queueNewRecordFrame() {
        Handler handler = mGLHandlerThread.getHandler();
        if (!handler.hasMessages(MSG_NEW_RECORD_FRAME)) {
            handler.sendMessage(handler.obtainMessage(MSG_NEW_RECORD_FRAME));
        }
    }

    /**
     * Make the recording stream texture ({@code true}) or the preview texture ({@code false})
     * the source of output frames, in order with queued frames.
     */
    public void setRecordSource(boolean record) {
        Handler handler = mGLHandlerThread.getHandler();
        handler.sendMessage(handler.obtainMessage(MSG_SET_RECORD_SOURCE, record ? 1 : 0,
                /*arg2*/0));
    }

    /**
     * End unpaired recording-frame draws until the next frame pairs with a request, in order
     * with queued frames.
     */
    public void clearUnpairedTargets() {
        mGLHandlerThread.getHandler().sendEmptyMessage(MSG_CLEAR_UNPAIRED_TARGETS);
    }

    private void runOnGlAndWait(Runnable operation) {
        ConditionVariable completion = new ConditionVariable(false);
        RuntimeException[] failure = new RuntimeException[1];
        if (!mGLHandlerThread.getHandler().post(() -> {
            try {
                operation.run();
            } catch (RuntimeException exception) {
                failure[0] = exception;
            } finally {
                completion.open();
            }
        })) throw new IllegalStateException("GL thread stops before recording operation");
        completion.block();
        if (failure[0] != null) throw failure[0];
    }

    /** Create the importer before camera callbacks can queue GL draws. */
    public long createIonRecordingBridge(int width, int height) {
        long[] result = new long[1];
        runOnGlAndWait(() -> result[0] = mTextureRenderer.createIonRecordingBridge(this,
                width, height));
        return result[0];
    }

    /** Select the ring after the HAL finishes its recording startup. */
    public void startIonRecordingDraws() {
        runOnGlAndWait(() -> {
            mTextureRenderer.setRecordSource(true);
            queueNewRecordFrame();
        });
    }

    /** Disconnect terminal outputs from the request thread to wake blocked GL dequeues. */
    public void disconnectIonOutputQueues() {
        mTextureRenderer.disconnectIonOutputQueues();
    }

    /** Finish queued draws before stopping the HAL recording pool. */
    public void stopIonRecordingDraws() {
        runOnGlAndWait(() -> mTextureRenderer.setRecordSource(false));
    }

    /** Free imported images after HAL callbacks and GPU releases drain. */
    public void destroyIonRecordingBridge() {
        runOnGlAndWait(() -> mTextureRenderer.destroyIonRecordingBridge());
    }

    /**
     * Configure the GL renderer for the given set of output surfaces, and block until
     * this configuration has been applied.
     *
     * @param surfaces a collection of pairs of {@link android.view.Surface}s and their
     *                 corresponding sizes to configure.
     * @param collector a {@link CaptureCollector} to retrieve requests from.
     */
    public void setConfigurationAndWait(Collection<Pair<Surface, Size>> surfaces,
                                        CaptureCollector collector) {
        checkNotNull(collector, "collector must not be null");
        Handler handler = mGLHandlerThread.getHandler();

        final ConditionVariable condition = new ConditionVariable(/*closed*/false);
        ConfigureHolder configure = new ConfigureHolder(condition, surfaces, collector);

        Message m = handler.obtainMessage(MSG_NEW_CONFIGURATION, /*arg1*/0, /*arg2*/0, configure);
        handler.sendMessage(m);

        // Block until configuration applied.
        condition.block();
    }

    /**
     * Get the underlying surface to produce frames from.
     *
     * <p>
     * This returns the surface that is drawn into the set of surfaces passed in for each frame.
     * This method should only be called after a call to
     * {@link #setConfigurationAndWait(java.util.Collection)}.  Calling this before the first call
     * to {@link #setConfigurationAndWait(java.util.Collection)}, after {@link #quit()}, or
     * concurrently to one of these calls may result in an invalid
     * {@link android.graphics.SurfaceTexture} being returned.
     * </p>
     *
     * @return an {@link android.graphics.SurfaceTexture} to draw to.
     */
    public SurfaceTexture getCurrentSurfaceTexture() {
        return mTextureRenderer.getSurfaceTexture();
    }

    /**
     * Ignore any subsequent calls to {@link #queueNewFrame(java.util.Collection)}.
     */
    public void ignoreNewFrames() {
        mGLHandlerThread.getHandler().sendEmptyMessage(MSG_DROP_FRAMES);
    }

    /**
     * Wait until no messages are queued.
     */
    public void waitUntilIdle() {
        mGLHandlerThread.waitUntilIdle();
    }

    /**
     * Re-enable drawing new frames after a call to {@link #ignoreNewFrames()}.
     */
    public void allowNewFrames() {
        mGLHandlerThread.getHandler().sendEmptyMessage(MSG_ALLOW_FRAMES);
    }
}
