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

#define LOG_TAG "Legacy-CameraDevice-JNI"
// #define LOG_NDEBUG 0
#include <utils/Log.h>
#include <utils/Errors.h>
#include <utils/Trace.h>
#include <camera/CameraUtils.h>
#include <camera/camera2/OutputConfiguration.h>

#include "jni.h"
#include <nativehelper/JNIHelp.h>
#include "android_hardware_Camera.h"
#include "core_jni_helpers.h"
#include "android_runtime/android_view_Surface.h"
#include "android_runtime/android_graphics_SurfaceTexture.h"

#include <gui/IGraphicBufferProducer.h>
#include <gui/IProducerListener.h>
#include <gui/Surface.h>
#include <hardware/camera3.h>
#include <surfacetexture/SurfaceTexture.h>
#include <system/camera_metadata.h>
#include <system/window.h>
#include <ui/GraphicBuffer.h>
#include <utils/Mutex.h>

#include <stdint.h>
#include <inttypes.h>

using namespace android;

// fully-qualified class name
#define CAMERA_DEVICE_CLASS_NAME "android/hardware/camera2/legacy/LegacyCameraDevice"
#define CAMERA_DEVICE_BUFFER_SLACK  3
#define DONT_CARE 0

#define ARRAY_SIZE(a) (sizeof(a)/sizeof(*(a)))

#define ALIGN(x, mask) ( ((x) + (mask) - 1) & ~((mask) - 1) )

// Use BAD_VALUE for surface abandoned error
#define OVERRIDE_SURFACE_ERROR(err) \
do {                                \
    if (err == -ENODEV) {           \
        err = BAD_VALUE;            \
    }                               \
} while (0)

#define UPDATE(md, tag, data, size)               \
do {                                              \
    if ((md).update((tag), (data), (size))) {     \
        ALOGE("Update " #tag " failed!");         \
        return BAD_VALUE;                         \
    }                                             \
} while (0)

/**
 * Convert from RGB 888 to Y'CbCr using the conversion specified in JFIF v1.02
 */
static void rgbToYuv420(uint8_t* rgbBuf, size_t width, size_t height, uint8_t* yPlane,
        uint8_t* crPlane, uint8_t* cbPlane, size_t chromaStep, size_t yStride, size_t chromaStride) {
    uint8_t R, G, B;
    size_t index = 0;
    for (size_t j = 0; j < height; j++) {
        uint8_t* cr = crPlane;
        uint8_t* cb = cbPlane;
        uint8_t* y = yPlane;
        bool jEven = (j & 1) == 0;
        for (size_t i = 0; i < width; i++) {
            R = rgbBuf[index++];
            G = rgbBuf[index++];
            B = rgbBuf[index++];
            *y++ = (77 * R + 150 * G +  29 * B) >> 8;
            if (jEven && (i & 1) == 0) {
                *cb = (( -43 * R - 85 * G + 128 * B) >> 8) + 128;
                *cr = (( 128 * R - 107 * G - 21 * B) >> 8) + 128;
                cr += chromaStep;
                cb += chromaStep;
            }
            // Skip alpha
            index++;
        }
        yPlane += yStride;
        if (jEven) {
            crPlane += chromaStride;
            cbPlane += chromaStride;
        }
    }
}

static void rgbToYuv420(uint8_t* rgbBuf, size_t width, size_t height, android_ycbcr* ycbcr) {
    size_t cStep = ycbcr->chroma_step;
    size_t cStride = ycbcr->cstride;
    size_t yStride = ycbcr->ystride;
    ALOGV("%s: yStride is: %zu, cStride is: %zu, cStep is: %zu", __FUNCTION__, yStride, cStride,
            cStep);
    rgbToYuv420(rgbBuf, width, height, reinterpret_cast<uint8_t*>(ycbcr->y),
            reinterpret_cast<uint8_t*>(ycbcr->cr), reinterpret_cast<uint8_t*>(ycbcr->cb),
            cStep, yStride, cStride);
}

static status_t connectSurface(const sp<Surface>& surface, int32_t maxBufferSlack) {
    status_t err = NO_ERROR;

    err = surface->connect(NATIVE_WINDOW_API_CAMERA, nullptr);
    if (err != OK) {
        ALOGE("%s: Unable to connect to surface, error %s (%d).", __FUNCTION__,
                strerror(-err), err);
        return err;
    }

    err = native_window_set_usage(surface.get(), GRALLOC_USAGE_SW_WRITE_OFTEN);
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to set native window usage flag, error %s (%d).", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    int minUndequeuedBuffers;
    err = static_cast<ANativeWindow*>(surface.get())->query(surface.get(),
            NATIVE_WINDOW_MIN_UNDEQUEUED_BUFFERS, &minUndequeuedBuffers);
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to get native window min undequeued buffers, error %s (%d).",
                __FUNCTION__, strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    ALOGV("%s: Setting buffer count to %d", __FUNCTION__,
            maxBufferSlack + 1 + minUndequeuedBuffers);
    err = native_window_set_buffer_count(surface.get(), maxBufferSlack + 1 + minUndequeuedBuffers);
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to set native window buffer count, error %s (%d).", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

/**
 * Produce a frame in the given surface.
 *
 * Args:
 *    anw - a surface to produce a frame in.
 *    pixelBuffer - image buffer to generate a frame from.
 *    width - width of the pixelBuffer in pixels.
 *    height - height of the pixelBuffer in pixels.
 *    pixelFmt - format of the pixelBuffer, one of:
 *               HAL_PIXEL_FORMAT_YCrCb_420_SP,
 *               HAL_PIXEL_FORMAT_YCbCr_420_888,
 *               HAL_PIXEL_FORMAT_BLOB
 *    bufSize - the size of the pixelBuffer in bytes.
 */
static status_t produceFrame(const sp<ANativeWindow>& anw,
                             uint8_t* pixelBuffer,
                             int32_t bufWidth, // Width of the pixelBuffer
                             int32_t bufHeight, // Height of the pixelBuffer
                             int32_t pixelFmt, // Format of the pixelBuffer
                             int32_t bufSize) {
    ATRACE_CALL();
    status_t err = NO_ERROR;
    ANativeWindowBuffer* anb;
    ALOGV("%s: Dequeue buffer from %p %dx%d (fmt=%x, size=%x)",
            __FUNCTION__, anw.get(), bufWidth, bufHeight, pixelFmt, bufSize);

    if (anw == 0) {
        ALOGE("%s: anw must not be NULL", __FUNCTION__);
        return BAD_VALUE;
    } else if (pixelBuffer == NULL) {
        ALOGE("%s: pixelBuffer must not be NULL", __FUNCTION__);
        return BAD_VALUE;
    } else if (bufWidth < 0) {
        ALOGE("%s: width must be non-negative", __FUNCTION__);
        return BAD_VALUE;
    } else if (bufHeight < 0) {
        ALOGE("%s: height must be non-negative", __FUNCTION__);
        return BAD_VALUE;
    } else if (bufSize < 0) {
        ALOGE("%s: bufSize must be non-negative", __FUNCTION__);
        return BAD_VALUE;
    }

    size_t width = static_cast<size_t>(bufWidth);
    size_t height = static_cast<size_t>(bufHeight);
    size_t bufferLength = static_cast<size_t>(bufSize);

    // TODO: Switch to using Surface::lock and Surface::unlockAndPost
    err = native_window_dequeue_buffer_and_wait(anw.get(), &anb);
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to dequeue buffer, error %s (%d).", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    sp<GraphicBuffer> buf(GraphicBuffer::from(anb));
    uint32_t grallocBufWidth = buf->getWidth();
    uint32_t grallocBufHeight = buf->getHeight();
    uint32_t grallocBufStride = buf->getStride();
    if (grallocBufWidth != width || grallocBufHeight != height) {
        ALOGE("%s: Received gralloc buffer with bad dimensions %" PRIu32 "x%" PRIu32
                ", expecting dimensions %zu x %zu",  __FUNCTION__, grallocBufWidth,
                grallocBufHeight, width, height);
        return BAD_VALUE;
    }

    int32_t bufFmt = 0;
    err = anw->query(anw.get(), NATIVE_WINDOW_FORMAT, &bufFmt);
    if (err != NO_ERROR) {
        ALOGE("%s: Error while querying surface pixel format %s (%d).", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    uint64_t tmpSize = (pixelFmt == HAL_PIXEL_FORMAT_BLOB) ? grallocBufWidth :
            4 * grallocBufHeight * grallocBufWidth;
    if (bufFmt != pixelFmt) {
        if (bufFmt == HAL_PIXEL_FORMAT_RGBA_8888 && pixelFmt == HAL_PIXEL_FORMAT_BLOB) {
            ALOGV("%s: Using BLOB to RGBA format override.", __FUNCTION__);
            tmpSize = 4 * (grallocBufWidth + grallocBufStride * (grallocBufHeight - 1));
        } else {
            ALOGW("%s: Format mismatch in produceFrame: expecting format %#" PRIx32
                    ", but received buffer with format %#" PRIx32, __FUNCTION__, pixelFmt, bufFmt);
        }
    }

    if (tmpSize > SIZE_MAX) {
        ALOGE("%s: Overflow calculating size, buffer with dimens %zu x %zu is absurdly large...",
                __FUNCTION__, width, height);
        return BAD_VALUE;
    }

    size_t totalSizeBytes = tmpSize;

    ALOGV("%s: Pixel format chosen: %x", __FUNCTION__, pixelFmt);
    switch(pixelFmt) {
        case HAL_PIXEL_FORMAT_YCrCb_420_SP: {
            if (bufferLength < totalSizeBytes) {
                ALOGE("%s: PixelBuffer size %zu too small for given dimensions",
                        __FUNCTION__, bufferLength);
                return BAD_VALUE;
            }
            uint8_t* img = NULL;
            ALOGV("%s: Lock buffer from %p for write", __FUNCTION__, anw.get());
            err = buf->lock(GRALLOC_USAGE_SW_WRITE_OFTEN, (void**)(&img));
            if (err != NO_ERROR) return err;

            uint8_t* yPlane = img;
            uint8_t* uPlane = img + height * width;
            uint8_t* vPlane = uPlane + 1;
            size_t chromaStep = 2;
            size_t yStride = width;
            size_t chromaStride = width;

            rgbToYuv420(pixelBuffer, width, height, yPlane,
                    uPlane, vPlane, chromaStep, yStride, chromaStride);
            break;
        }
        case HAL_PIXEL_FORMAT_YV12: {
            if (bufferLength < totalSizeBytes) {
                ALOGE("%s: PixelBuffer size %zu too small for given dimensions",
                        __FUNCTION__, bufferLength);
                return BAD_VALUE;
            }

            if ((width & 1) || (height & 1)) {
                ALOGE("%s: Dimens %zu x %zu are not divisible by 2.", __FUNCTION__, width, height);
                return BAD_VALUE;
            }

            uint8_t* img = NULL;
            ALOGV("%s: Lock buffer from %p for write", __FUNCTION__, anw.get());
            err = buf->lock(GRALLOC_USAGE_SW_WRITE_OFTEN, (void**)(&img));
            if (err != NO_ERROR) {
                ALOGE("%s: Error %s (%d) while locking gralloc buffer for write.", __FUNCTION__,
                        strerror(-err), err);
                return err;
            }

            uint32_t stride = buf->getStride();
            ALOGV("%s: stride is: %" PRIu32, __FUNCTION__, stride);
            LOG_ALWAYS_FATAL_IF(stride % 16, "Stride is not 16 pixel aligned %d", stride);

            uint32_t cStride = ALIGN(stride / 2, 16);
            size_t chromaStep = 1;

            uint8_t* yPlane = img;
            uint8_t* crPlane = img + static_cast<uint32_t>(height) * stride;
            uint8_t* cbPlane = crPlane + cStride * static_cast<uint32_t>(height) / 2;

            rgbToYuv420(pixelBuffer, width, height, yPlane,
                    crPlane, cbPlane, chromaStep, stride, cStride);
            break;
        }
        case HAL_PIXEL_FORMAT_YCbCr_420_888: {
            // Software writes with YCbCr_420_888 format are unsupported
            // by the gralloc module for now
            if (bufferLength < totalSizeBytes) {
                ALOGE("%s: PixelBuffer size %zu too small for given dimensions",
                        __FUNCTION__, bufferLength);
                return BAD_VALUE;
            }
            android_ycbcr ycbcr = android_ycbcr();
            ALOGV("%s: Lock buffer from %p for write", __FUNCTION__, anw.get());

            err = buf->lockYCbCr(GRALLOC_USAGE_SW_WRITE_OFTEN, &ycbcr);
            if (err != NO_ERROR) {
                ALOGE("%s: Failed to lock ycbcr buffer, error %s (%d).", __FUNCTION__,
                        strerror(-err), err);
                return err;
            }
            rgbToYuv420(pixelBuffer, width, height, &ycbcr);
            break;
        }
        case HAL_PIXEL_FORMAT_BLOB: {
            int8_t* img = NULL;
            struct camera3_jpeg_blob footer = {
                .jpeg_blob_id = CAMERA3_JPEG_BLOB_ID,
                .jpeg_size = (uint32_t)bufferLength
            };

            size_t totalJpegSize = bufferLength + sizeof(footer);
            totalJpegSize = (totalJpegSize + 3) & ~0x3; // round up to nearest octonibble

            if (totalJpegSize > totalSizeBytes) {
                ALOGE("%s: Pixel buffer needs size %zu, cannot fit in gralloc buffer of size %zu",
                        __FUNCTION__, totalJpegSize, totalSizeBytes);
                return BAD_VALUE;
            }

            err = buf->lock(GRALLOC_USAGE_SW_WRITE_OFTEN, (void**)(&img));
            if (err != NO_ERROR) {
                ALOGE("%s: Failed to lock buffer, error %s (%d).", __FUNCTION__, strerror(-err),
                        err);
                return err;
            }

            memcpy(img, pixelBuffer, bufferLength);
            memcpy(img + totalSizeBytes - sizeof(footer), &footer, sizeof(footer));
            break;
        }
        default: {
            ALOGE("%s: Invalid pixel format in produceFrame: %x", __FUNCTION__, pixelFmt);
            return BAD_VALUE;
        }
    }

    ALOGV("%s: Unlock buffer from %p", __FUNCTION__, anw.get());
    err = buf->unlock();
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to unlock buffer, error %s (%d).", __FUNCTION__, strerror(-err), err);
        return err;
    }

    ALOGV("%s: Queue buffer to %p", __FUNCTION__, anw.get());
    err = anw->queueBuffer(anw.get(), buf->getNativeBuffer(), /*fenceFd*/-1);
    if (err != NO_ERROR) {
        ALOGE("%s: Failed to queue buffer, error %s (%d).", __FUNCTION__, strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

static sp<ANativeWindow> getNativeWindow(JNIEnv* env, jobject surface) {
    sp<ANativeWindow> anw;
    if (surface) {
        anw = android_view_Surface_getNativeWindow(env, surface);
        if (env->ExceptionCheck()) {
            return NULL;
        }
    } else {
        jniThrowNullPointerException(env, "surface");
        return NULL;
    }
    if (anw == NULL) {
        ALOGE("%s: Surface had no valid native window.", __FUNCTION__);
        return NULL;
    }
    return anw;
}

static sp<ANativeWindow> getSurfaceTextureNativeWindow(JNIEnv* env, jobject thiz) {
    sp<IGraphicBufferProducer> producer(SurfaceTexture_getProducer(env, thiz));
    sp<Surface> surfaceTextureClient(producer != NULL ? new Surface(producer) : NULL);
    return surfaceTextureClient;
}

static sp<ANativeWindow> getNativeWindowFromTexture(JNIEnv* env, jobject surfaceTexture) {
    sp<ANativeWindow> anw;
    if (surfaceTexture) {
        anw = getSurfaceTextureNativeWindow(env, surfaceTexture);
        if (env->ExceptionCheck()) {
            return NULL;
        }
    } else {
        jniThrowNullPointerException(env, "surfaceTexture");
        return NULL;
    }
    if (anw == NULL) {
        jniThrowExceptionFmt(env, "java/lang/IllegalArgumentException",
                "SurfaceTexture had no valid native window.");
        return NULL;
    }
    return anw;
}

static sp<Surface> getSurface(JNIEnv* env, jobject surface) {
    sp<Surface> s;
    if (surface) {
        s = android_view_Surface_getSurface(env, surface);
        if (env->ExceptionCheck()) {
            return NULL;
        }
    } else {
        jniThrowNullPointerException(env, "surface");
        return NULL;
    }
    if (s == NULL) {
        jniThrowExceptionFmt(env, "java/lang/IllegalArgumentException",
                "Surface had no valid native Surface.");
        return NULL;
    }
    return s;
}

/*
 * Copies the HAL1 recording stream into a video encoder input surface.
 *
 * With VIDEO_BUFFER_MODE_DATA_CALLBACK_YUV the HAL delivers each recording
 * frame as NV12 pixels in shared memory, laid out as the Venus encoder input
 * (Y stride aligned to 128, Y scanlines to 32, chroma scanlines to 16, chroma
 * after the padded luma plane), or tightly packed. Each frame is copied row by
 * row into a buffer dequeued from the encoder surface, stamped with the HAL
 * timestamp, and returned to the HAL before onRecordingFrame returns, so the
 * HAL's fixed video buffer pool never waits on the encoder. A frame that finds
 * no free encoder buffer within kDequeueTimeoutNs is dropped.
 */
class RecordingStreamBridge : public CameraRecordingFrameSink {
public:
    RecordingStreamBridge(const sp<Surface>& surface, uint32_t width, uint32_t height)
            : mSurface(surface), mWidth(width), mHeight(height) {}

    void onRecordingFrame(const sp<Camera>& camera, nsecs_t timestamp,
            const sp<IMemory>& frame) override {
        {
            Mutex::Autolock _l(mLock);
            if (mActive && frame != nullptr) {
                copyFrame_l(timestamp, frame);
            }
        }
        camera->releaseRecordingFrame(frame);
    }

    void deactivate() override {
        Mutex::Autolock _l(mLock);
        mActive = false;
        ALOGI("%s: %ux%u recording stream: %" PRIu64 " frames copied, %" PRIu64 " dropped",
                __FUNCTION__, mWidth, mHeight, mCopied, mDropped);
    }

    static constexpr nsecs_t dequeueTimeoutNs() { return kDequeueTimeoutNs; }

private:
    static constexpr nsecs_t kDequeueTimeoutNs = 20000000; // 20 ms

    void copyFrame_l(nsecs_t timestamp, const sp<IMemory>& frame) {
        const uint8_t* src = static_cast<const uint8_t*>(frame->unsecurePointer());
        const size_t srcSize = frame->size();
        const size_t chromaRows = (mHeight + 1) / 2;
        size_t srcYStride = ALIGN(mWidth, 128);
        size_t srcChromaOffset = srcYStride * ALIGN(mHeight, 32);
        if (src == nullptr || srcSize < srcChromaOffset + srcYStride * chromaRows) {
            srcYStride = mWidth;
            srcChromaOffset = static_cast<size_t>(mWidth) * mHeight;
            if (src == nullptr || srcSize < srcChromaOffset + srcYStride * chromaRows) {
                ALOGE("%s: %zu-byte frame is too small for %ux%u NV12", __FUNCTION__, srcSize,
                        mWidth, mHeight);
                mDropped++;
                return;
            }
        }

        ANativeWindow* anw = mSurface.get();
        ANativeWindowBuffer* anb;
        status_t err = native_window_dequeue_buffer_and_wait(anw, &anb);
        if (err != NO_ERROR) {
            if (mDropped++ == 0 || err != TIMED_OUT) {
                ALOGW("%s: no encoder buffer, dropping frame: %s (%d)", __FUNCTION__,
                        strerror(-err), err);
            }
            return;
        }
        sp<GraphicBuffer> buf(GraphicBuffer::from(anb));
        android_ycbcr ycbcr = android_ycbcr();
        err = buf->lockYCbCr(GRALLOC_USAGE_SW_WRITE_OFTEN, &ycbcr);
        if (err != NO_ERROR) {
            ALOGE("%s: lockYCbCr failed: %s (%d)", __FUNCTION__, strerror(-err), err);
            anw->cancelBuffer(anw, anb, /*fenceFd*/-1);
            mDropped++;
            return;
        }
        if (mCopied == 0) {
            ALOGI("%s: %ux%u source %zu bytes, y stride %zu, chroma at %zu; target y stride %zu,"
                    " c stride %zu, chroma step %zu, cb at %td, cr at %td", __FUNCTION__, mWidth,
                    mHeight, srcSize, srcYStride, srcChromaOffset, ycbcr.ystride, ycbcr.cstride,
                    ycbcr.chroma_step, static_cast<uint8_t*>(ycbcr.cb) -
                    static_cast<uint8_t*>(ycbcr.y), static_cast<uint8_t*>(ycbcr.cr) -
                    static_cast<uint8_t*>(ycbcr.y));
        }

        uint8_t* dstY = static_cast<uint8_t*>(ycbcr.y);
        for (size_t row = 0; row < mHeight; row++) {
            memcpy(dstY + row * ycbcr.ystride, src + row * srcYStride, mWidth);
        }
        const uint8_t* srcC = src + srcChromaOffset;
        uint8_t* dstCb = static_cast<uint8_t*>(ycbcr.cb);
        uint8_t* dstCr = static_cast<uint8_t*>(ycbcr.cr);
        for (size_t row = 0; row < chromaRows; row++) {
            const uint8_t* in = srcC + row * srcYStride;
            if (ycbcr.chroma_step == 2 && dstCr == dstCb + 1) {
                memcpy(dstCb + row * ycbcr.cstride, in, mWidth & ~1u);
                continue;
            }
            uint8_t* cb = dstCb + row * ycbcr.cstride;
            uint8_t* cr = dstCr + row * ycbcr.cstride;
            for (size_t col = 0; col < mWidth / 2; col++) {
                cb[col * ycbcr.chroma_step] = in[2 * col];
                cr[col * ycbcr.chroma_step] = in[2 * col + 1];
            }
        }

        err = buf->unlock();
        if (err == NO_ERROR) {
            err = native_window_set_buffers_timestamp(anw, timestamp);
        }
        if (err != NO_ERROR) {
            ALOGE("%s: unable to finish buffer: %s (%d)", __FUNCTION__, strerror(-err), err);
            anw->cancelBuffer(anw, anb, /*fenceFd*/-1);
            mDropped++;
            return;
        }
        err = anw->queueBuffer(anw, anb, /*fenceFd*/-1);
        if (err != NO_ERROR) {
            ALOGE("%s: queueBuffer failed: %s (%d)", __FUNCTION__, strerror(-err), err);
            mDropped++;
            return;
        }
        mCopied++;
    }

    Mutex mLock;
    const sp<Surface> mSurface;
    const uint32_t mWidth;
    const uint32_t mHeight;
    bool mActive = true;
    uint64_t mCopied = 0;
    uint64_t mDropped = 0;
};

extern "C" {

static jint LegacyCameraDevice_nativeDisconnectSurface(JNIEnv* env, jobject thiz,
          jobject surface) {
    ALOGV("nativeDisconnectSurface");
    if (surface == nullptr) return NO_ERROR;

    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGV("Buffer queue has already been abandoned.");
        return NO_ERROR;
    }

    status_t err = native_window_api_disconnect(anw.get(), NATIVE_WINDOW_API_CAMERA);
    if(err != NO_ERROR) {
        jniThrowException(env, "java/lang/UnsupportedOperationException",
            "Error while disconnecting surface");
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeDetectTextureDimens(JNIEnv* env, jobject thiz,
        jobject surfaceTexture, jintArray dimens) {
    ALOGV("nativeDetectTextureDimens");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindowFromTexture(env, surfaceTexture)) == NULL) {
        ALOGE("%s: Could not retrieve native window from SurfaceTexture.", __FUNCTION__);
        return BAD_VALUE;
    }

    int32_t dimenBuf[2];
    status_t err = anw->query(anw.get(), NATIVE_WINDOW_WIDTH, dimenBuf);
    if(err != NO_ERROR) {
        ALOGE("%s: Error while querying SurfaceTexture width %s (%d)", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    err = anw->query(anw.get(), NATIVE_WINDOW_HEIGHT, dimenBuf + 1);
    if(err != NO_ERROR) {
        ALOGE("%s: Error while querying SurfaceTexture height %s (%d)", __FUNCTION__,
                strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }

    env->SetIntArrayRegion(dimens, /*start*/0, /*length*/ARRAY_SIZE(dimenBuf), dimenBuf);
    if (env->ExceptionCheck()) {
        return BAD_VALUE;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeConnectSurface(JNIEnv* env, jobject thiz, jobject surface) {
    ALOGV("nativeConnectSurface");
    sp<Surface> s;
    if ((s = getSurface(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve surface.", __FUNCTION__);
        return BAD_VALUE;
    }
    status_t err = connectSurface(s, CAMERA_DEVICE_BUFFER_SLACK);
    if (err != NO_ERROR) {
        ALOGE("%s: Error while configuring surface %s (%d).", __FUNCTION__, strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeProduceFrame(JNIEnv* env, jobject thiz, jobject surface,
        jbyteArray pixelBuffer, jint width, jint height, jint pixelFormat) {
    ALOGV("nativeProduceFrame");
    sp<ANativeWindow> anw;

    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }

    if (pixelBuffer == NULL) {
        jniThrowNullPointerException(env, "pixelBuffer");
        return DONT_CARE;
    }

    int32_t bufSize = static_cast<int32_t>(env->GetArrayLength(pixelBuffer));
    jbyte* pixels = env->GetByteArrayElements(pixelBuffer, /*is_copy*/NULL);

    if (pixels == NULL) {
        jniThrowNullPointerException(env, "pixels");
        return DONT_CARE;
    }

    status_t err = produceFrame(anw, reinterpret_cast<uint8_t*>(pixels), width, height,
            pixelFormat, bufSize);
    env->ReleaseByteArrayElements(pixelBuffer, pixels, JNI_ABORT);

    if (err != NO_ERROR) {
        ALOGE("%s: Error while producing frame %s (%d).", __FUNCTION__, strerror(-err), err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeSetSurfaceFormat(JNIEnv* env, jobject thiz, jobject surface,
        jint pixelFormat) {
    ALOGV("nativeSetSurfaceType");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }
    status_t err = native_window_set_buffers_format(anw.get(), pixelFormat);
    if (err != NO_ERROR) {
        ALOGE("%s: Error while setting surface format %s (%d).", __FUNCTION__, strerror(-err), err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeSetSurfaceDimens(JNIEnv* env, jobject thiz, jobject surface,
        jint width, jint height) {
    ALOGV("nativeSetSurfaceDimens");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }

    // Set user dimensions only
    // The producer dimensions are owned by GL
    status_t err = native_window_set_buffers_user_dimensions(anw.get(), width, height);
    if (err != NO_ERROR) {
        ALOGE("%s: Error while setting surface user dimens %s (%d).", __FUNCTION__, strerror(-err),
                err);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeSetSurfaceOrientation(JNIEnv* env, jobject thiz,
        jobject surface, jint facing, jint orientation) {
    ALOGV("nativeSetSurfaceOrientation");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }

    status_t err = NO_ERROR;
    CameraMetadata staticMetadata;

    int32_t orientVal = static_cast<int32_t>(orientation);
    uint8_t facingVal = static_cast<uint8_t>(facing);
    staticMetadata.update(ANDROID_SENSOR_ORIENTATION, &orientVal, 1);
    staticMetadata.update(ANDROID_LENS_FACING, &facingVal, 1);

    int32_t transform = 0;

    // MIRROR_MODE_AUTO flips front-facing buffers horizontally and the
    // inverse-display flag keeps the buffer aligned with the physical sensor
    // under UI rotation, the transform the HAL1 Camera2Client applies to its
    // preview stream.
    if ((err = CameraUtils::getRotationTransform(staticMetadata,
            OutputConfiguration::MIRROR_MODE_AUTO, /*enableTransformInverseDisplay*/true,
            /*out*/&transform)) != NO_ERROR) {
        ALOGE("%s: Invalid rotation transform %s (%d)", __FUNCTION__, strerror(-err),
                err);
        return err;
    }

    ALOGV("%s: Setting buffer sticky transform to %d", __FUNCTION__, transform);

    if ((err = native_window_set_buffers_sticky_transform(anw.get(), transform)) != NO_ERROR) {
        ALOGE("%s: Unable to configure surface transform, error %s (%d)", __FUNCTION__,
                strerror(-err), err);
        return err;
    }

    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeSetNextTimestamp(JNIEnv* env, jobject thiz, jobject surface,
        jlong timestamp) {
    ALOGV("nativeSetNextTimestamp");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }

    status_t err = NO_ERROR;

    if ((err = native_window_set_buffers_timestamp(anw.get(), static_cast<int64_t>(timestamp))) !=
            NO_ERROR) {
        ALOGE("%s: Unable to set surface timestamp, error %s (%d)", __FUNCTION__, strerror(-err),
                err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeSetScalingMode(JNIEnv* env, jobject thiz, jobject surface,
        jint mode) {
    ALOGV("nativeSetScalingMode");
    sp<ANativeWindow> anw;
    if ((anw = getNativeWindow(env, surface)) == NULL) {
        ALOGE("%s: Could not retrieve native window from surface.", __FUNCTION__);
        return BAD_VALUE;
    }
    status_t err = NO_ERROR;
    if ((err = native_window_set_scaling_mode(anw.get(), static_cast<int>(mode))) != NO_ERROR) {
        ALOGE("%s: Unable to set surface scaling mode, error %s (%d)", __FUNCTION__,
                strerror(-err), err);
        return err;
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeStartRecordingStream(JNIEnv* env, jobject thiz,
        jobject camera, jobject surface, jint width, jint height) {
    ALOGV("nativeStartRecordingStream");
    if (width <= 0 || height <= 0) {
        return BAD_VALUE;
    }
    sp<Surface> s = getSurface(env, surface);
    if (s == nullptr) {
        return BAD_VALUE;
    }
    status_t err = connectSurface(s, CAMERA_DEVICE_BUFFER_SLACK);
    if (err == NO_ERROR) {
        err = native_window_set_buffers_dimensions(s.get(), width, height);
    }
    // A GL texture consumer imports the buffer through eglCreateImageKHR, and
    // the Adreno EGL driver accepts YCrCb_420_SP there but rejects the
    // flexible YCbCr_420_888 enum; an encoder takes YCbCr_420_888, which
    // gralloc resolves to its encodeable layout. copyFrame_l writes either
    // through lockYCbCr.
    int consumerUsage = 0;
    if (err == NO_ERROR) {
        err = s->query(NATIVE_WINDOW_CONSUMER_USAGE_BITS, &consumerUsage);
    }
    if (err == NO_ERROR) {
        bool textureOnly = (consumerUsage & GRALLOC_USAGE_HW_TEXTURE) != 0
                && (consumerUsage & GRALLOC_USAGE_HW_VIDEO_ENCODER) == 0;
        err = native_window_set_buffers_format(s.get(), textureOnly
                ? HAL_PIXEL_FORMAT_YCrCb_420_SP : HAL_PIXEL_FORMAT_YCbCr_420_888);
    }
    if (err != NO_ERROR) {
        ALOGE("%s: unable to configure the recording surface: %s (%d)", __FUNCTION__,
                strerror(-err), err);
        native_window_api_disconnect(s.get(), NATIVE_WINDOW_API_CAMERA);
        OVERRIDE_SURFACE_ERROR(err);
        return err;
    }
    s->setDequeueTimeout(RecordingStreamBridge::dequeueTimeoutNs());

    sp<RecordingStreamBridge> bridge = new RecordingStreamBridge(s, static_cast<uint32_t>(width),
            static_cast<uint32_t>(height));
    sp<CameraRecordingFrameSink> previous;
    sp<Camera> c = android_hardware_Camera_setRecordingFrameSink(env, camera, bridge, &previous);
    if (c == nullptr) {
        native_window_api_disconnect(s.get(), NATIVE_WINDOW_API_CAMERA);
        return NO_INIT;
    }
    err = c->setVideoBufferMode(hardware::ICamera::VIDEO_BUFFER_MODE_DATA_CALLBACK_YUV);
    if (err == NO_ERROR) {
        err = c->startRecording();
    }
    if (err != NO_ERROR) {
        ALOGE("%s: unable to start the recording stream: %s (%d)", __FUNCTION__,
                strerror(-err), err);
        android_hardware_Camera_setRecordingFrameSink(env, camera, previous, nullptr);
        bridge->deactivate();
        native_window_api_disconnect(s.get(), NATIVE_WINDOW_API_CAMERA);
        return err;
    }
    ALOGI("%s: %dx%d recording stream feeds a surface with consumer usage 0x%x", __FUNCTION__,
            width, height, consumerUsage);
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeStopRecordingStream(JNIEnv* env, jobject thiz,
        jobject camera, jobject surface) {
    ALOGV("nativeStopRecordingStream");
    sp<CameraRecordingFrameSink> previous;
    sp<Camera> c = android_hardware_Camera_setRecordingFrameSink(env, camera, nullptr, &previous);
    if (c == nullptr) {
        return NO_INIT;
    }
    c->stopRecording();
    if (previous != nullptr) {
        previous->deactivate();
    }
    sp<ANativeWindow> anw = getNativeWindow(env, surface);
    if (anw != nullptr) {
        native_window_api_disconnect(anw.get(), NATIVE_WINDOW_API_CAMERA);
    }
    return NO_ERROR;
}

static jint LegacyCameraDevice_nativeGetJpegFooterSize(JNIEnv* env, jobject thiz) {
    ALOGV("nativeGetJpegFooterSize");
    return static_cast<jint>(sizeof(struct camera3_jpeg_blob));
}

} // extern "C"

static const JNINativeMethod gCameraDeviceMethods[] = {
    { "nativeConnectSurface",
    "(Landroid/view/Surface;)I",
    (void *)LegacyCameraDevice_nativeConnectSurface },
    { "nativeProduceFrame",
    "(Landroid/view/Surface;[BIII)I",
    (void *)LegacyCameraDevice_nativeProduceFrame },
    { "nativeSetSurfaceFormat",
    "(Landroid/view/Surface;I)I",
    (void *)LegacyCameraDevice_nativeSetSurfaceFormat },
    { "nativeSetSurfaceDimens",
    "(Landroid/view/Surface;II)I",
    (void *)LegacyCameraDevice_nativeSetSurfaceDimens },
    { "nativeDetectTextureDimens",
    "(Landroid/graphics/SurfaceTexture;[I)I",
    (void *)LegacyCameraDevice_nativeDetectTextureDimens },
    { "nativeSetSurfaceOrientation",
    "(Landroid/view/Surface;II)I",
    (void *)LegacyCameraDevice_nativeSetSurfaceOrientation },
    { "nativeSetNextTimestamp",
    "(Landroid/view/Surface;J)I",
    (void *)LegacyCameraDevice_nativeSetNextTimestamp },
    { "nativeGetJpegFooterSize",
    "()I",
    (void *)LegacyCameraDevice_nativeGetJpegFooterSize },
    { "nativeSetScalingMode",
    "(Landroid/view/Surface;I)I",
    (void *)LegacyCameraDevice_nativeSetScalingMode },
    { "nativeDisconnectSurface",
    "(Landroid/view/Surface;)I",
    (void *)LegacyCameraDevice_nativeDisconnectSurface },
    { "nativeStartRecordingStream",
    "(Landroid/hardware/Camera;Landroid/view/Surface;II)I",
    (void *)LegacyCameraDevice_nativeStartRecordingStream },
    { "nativeStopRecordingStream",
    "(Landroid/hardware/Camera;Landroid/view/Surface;)I",
    (void *)LegacyCameraDevice_nativeStopRecordingStream },
};

// Get all the required offsets in java class and register native functions
int register_android_hardware_camera2_legacy_LegacyCameraDevice(JNIEnv* env)
{
    // Register native functions
    return RegisterMethodsOrDie(env,
            CAMERA_DEVICE_CLASS_NAME,
            gCameraDeviceMethods,
            NELEM(gCameraDeviceMethods));
}
