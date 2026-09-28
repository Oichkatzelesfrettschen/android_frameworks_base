#ifndef _ANDROID_HARDWARE_CAMERA_H
#define _ANDROID_HARDWARE_CAMERA_H

#include <binder/IMemory.h>
#include <camera/Camera.h>
#include <utils/RefBase.h>
#include <utils/Timers.h>

#include "jni.h"

namespace android {

/*
 * Receiver of the HAL1 recording stream (CAMERA_MSG_VIDEO_FRAME data callbacks)
 * of an android.hardware.Camera object. onRecordingFrame runs on the binder
 * thread that delivers the frame, and the sink returns every frame it receives
 * to the HAL through Camera::releaseRecordingFrame, since the HAL recording
 * stream runs from a fixed pool of video buffers.
 */
class CameraRecordingFrameSink : public virtual RefBase {
public:
    virtual void onRecordingFrame(const sp<Camera>& camera, nsecs_t timestamp,
            const sp<IMemory>& frame) = 0;
    // Waits for an in-progress onRecordingFrame; later frames only return to the HAL.
    virtual void deactivate() = 0;
};

/*
 * Installs sink (or clears it, for a null sink) as the recording-frame receiver
 * of the Java android.hardware.Camera object `camera`, and returns the sink it
 * replaces through `previous`. With no sink installed, recording frames return
 * to the HAL on arrival. Returns the native camera, or null after
 * Camera.release(), in which case a RuntimeException is pending.
 */
sp<Camera> android_hardware_Camera_setRecordingFrameSink(JNIEnv* env, jobject camera,
        const sp<CameraRecordingFrameSink>& sink, sp<CameraRecordingFrameSink>* previous);

} // namespace android

#endif // _ANDROID_HARDWARE_CAMERA_H
