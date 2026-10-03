#ifndef ANDROID_LEGACY_CAMERA_ION_RECORDING_H
#define ANDROID_LEGACY_CAMERA_ION_RECORDING_H

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <android_runtime/AndroidRuntime.h>
#include <cutils/properties.h>

#include <algorithm>
#include <condition_variable>
#include <deque>
#include <map>
#include <mutex>
#include <thread>

#include "LegacyCameraIonBuffer.h"

namespace android {

// The GL thread owns EGLImages; the release thread owns completion-fence waits.
// HAL allocations stay held through the last sampling draw and its EGL completion fence.
class IonRecordingStreamBridge : public CameraRecordingFrameSink {
   public:
    IonRecordingStreamBridge(JNIEnv* env, jobject manager, uint32_t width, uint32_t height)
        : mWidth(width),
          mHeight(height),
          mDisplay(eglGetCurrentDisplay()),
          mManager(env->NewGlobalRef(manager)),
          mReleaser(&IonRecordingStreamBridge::releaseLoop, this) {
        jclass managerClass = env->GetObjectClass(manager);
        mNotify = env->GetMethodID(managerClass, "queueNewRecordFrame", "()V");
        env->DeleteLocalRef(managerClass);
    }

    ~IonRecordingStreamBridge() override {
        {
            std::lock_guard<std::mutex> lock(mReleaseLock);
            mQuit = true;
            mReleaseReady.notify_one();
        }
        mReleaser.join();
        if (mManager != nullptr) AndroidRuntime::getJNIEnv()->DeleteGlobalRef(mManager);
    }

    status_t initialize() {
        if (mDisplay == EGL_NO_DISPLAY || eglGetCurrentContext() == EGL_NO_CONTEXT ||
            mNotify == nullptr || mManager == nullptr)
            return NO_INIT;
        const char* extensions = eglQueryString(mDisplay, EGL_EXTENSIONS);
        if (extensions == nullptr || strstr(extensions, "EGL_KHR_fence_sync") == nullptr) {
            return INVALID_OPERATION;
        }
        mCreateImage =
            reinterpret_cast<PFNEGLCREATEIMAGEKHRPROC>(eglGetProcAddress("eglCreateImageKHR"));
        mDestroyImage =
            reinterpret_cast<PFNEGLDESTROYIMAGEKHRPROC>(eglGetProcAddress("eglDestroyImageKHR"));
        mBindImage = reinterpret_cast<PFNGLEGLIMAGETARGETTEXTURE2DOESPROC>(
            eglGetProcAddress("glEGLImageTargetTexture2DOES"));
        mCreateSync =
            reinterpret_cast<PFNEGLCREATESYNCKHRPROC>(eglGetProcAddress("eglCreateSyncKHR"));
        mDestroySync =
            reinterpret_cast<PFNEGLDESTROYSYNCKHRPROC>(eglGetProcAddress("eglDestroySyncKHR"));
        mWaitSync = reinterpret_cast<PFNEGLCLIENTWAITSYNCKHRPROC>(
            eglGetProcAddress("eglClientWaitSyncKHR"));
        if (!mCreateImage || !mDestroyImage || !mBindImage || !mCreateSync || !mDestroySync ||
            !mWaitSync)
            return INVALID_OPERATION;
        return mImporter.initialize(mWidth, mHeight);
    }

    void onRecordingFrame(const sp<Camera>& camera, nsecs_t timestamp,
                          const sp<IMemory>& frame) override {
        errno = 0;
        const int callerNice = getpriority(PRIO_PROCESS, 0);
        const bool restoreNice = errno == 0 && callerNice > ANDROID_PRIORITY_URGENT_DISPLAY;
        if (restoreNice) setpriority(PRIO_PROCESS, 0, ANDROID_PRIORITY_URGENT_DISPLAY);
        {
            std::lock_guard<std::mutex> lock(mLock);
            ++mReceived;
            if (mReceived == 120 && frame != nullptr &&
                property_get_bool("debug.camera.ion.probe", false)) {
                const auto* pixels = static_cast<const uint8_t*>(frame->unsecurePointer());
                if (pixels != nullptr && frame->size() == mImporter.allocationSize()) {
                    const size_t stride = (mWidth + 127u) & ~127u;
                    ssize_t offset = 0;
                    size_t size = 0;
                    sp<IMemoryHeap> heap = frame->getMemory(&offset, &size);
                    char descriptorPath[64];
                    char descriptorTarget[128] = "unresolved";
                    if (heap != nullptr) {
                        snprintf(descriptorPath, sizeof(descriptorPath), "/proc/self/fd/%d",
                                 heap->getHeapID());
                        const ssize_t length = readlink(descriptorPath, descriptorTarget,
                                                        sizeof(descriptorTarget) - 1);
                        if (length >= 0) descriptorTarget[length] = '\0';
                    }
                    ALOGI("ION recording source probe Y=%u,%u,%u,%u bytes=%zu fd=%s",
                          pixels[0], pixels[mWidth / 2], pixels[(mHeight / 2) * stride],
                          pixels[(mHeight / 2) * stride + mWidth / 2], frame->size(),
                          descriptorTarget);
                }
            }
            if (!mActive || frame == nullptr || mError != NO_ERROR ||
                mPending.size() >= kQueueDepth || mHeld >= kHeldBudget) {
                ++mDropped;
                queueRelease({camera, frame, nullptr}, EGL_NO_SYNC_KHR);
            } else {
                const IBinder* key = IInterface::asBinder(frame).get();
                auto found = mSlots.find(key);
                if (found == mSlots.end() && mSlots.size() < kPoolSize) {
                    Slot slot;
                    slot.memory = frame;
                    ssize_t offset = -1;
                    size_t size = 0;
                    sp<IMemoryHeap> heap = frame->getMemory(&offset, &size);
                    if (heap != nullptr && offset == 0 && heap->getSize() == size) {
                        slot.buffer = mImporter.import(heap->getHeapID(), size, 0);
                    }
                    if (slot.buffer != nullptr) {
                        found = mSlots.emplace(key, std::move(slot)).first;
                        ALOGI("ION recording import slot=%zu bytes=%zu format=0x%x CPU-copy=0",
                              mSlots.size(), size, LegacyCameraIonBuffer::kVenusFormat);
                    }
                }
                LOG_ALWAYS_FATAL_IF(found != mSlots.end() && found->second.held,
                                    "HAL1 recording buffer reused before GPU ownership returns");
                if (found == mSlots.end()) {
                    mError = BAD_VALUE;
                    ++mDropped;
                    ALOGE("ION recording pool/layout/ownership validation failed");
                    queueRelease({camera, frame, nullptr}, EGL_NO_SYNC_KHR);
                } else {
                    found->second.held = true;
                    ++mHeld;
                    mHeldPeak = std::max(mHeldPeak, mHeld);
                    mPending.push_back({camera, frame, &found->second, timestamp});
                }
                notifyFrame();
            }
        }
        if (restoreNice) setpriority(PRIO_PROCESS, 0, callerNice);
    }

    nsecs_t bind(GLuint texture) {
        std::lock_guard<std::mutex> lock(mLock);
        if (mError != NO_ERROR) return mError;
        if (!mActive || mPending.empty()) return 0;
        if (mDrawing.slot != nullptr || eglGetCurrentDisplay() != mDisplay)
            return INVALID_OPERATION;
        mDrawing = std::move(mPending.front());
        mPending.pop_front();
        Slot& slot = *mDrawing.slot;
        if (slot.image == EGL_NO_IMAGE_KHR) {
            const EGLint attributes[] = {EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE};
            slot.image = mCreateImage(mDisplay, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID,
                                      slot.buffer->getNativeBuffer(), attributes);
            if (slot.image == EGL_NO_IMAGE_KHR) {
                ALOGE("ION recording eglCreateImageKHR failed 0x%x", eglGetError());
                mError = INVALID_OPERATION;
                queueRelease(mDrawing, EGL_NO_SYNC_KHR);
                mDrawing = {};
                mDrawIdle.notify_all();
                return mError;
            }
            ++mImages;
        }
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture);
        mBindImage(GL_TEXTURE_EXTERNAL_OES, slot.image);
        if (glGetError() != GL_NO_ERROR) {
            mError = INVALID_OPERATION;
            queueRelease(mDrawing, EGL_NO_SYNC_KHR);
            mDrawing = {};
            mDrawIdle.notify_all();
            return mError;
        }
        ++mBound;
        if (!mPending.empty()) notifyFrame();
        return mDrawing.timestamp;
    }

    status_t drawn() {
        std::lock_guard<std::mutex> lock(mLock);
        if (mDrawing.slot == nullptr) return NO_ERROR;
        EGLSyncKHR sync = mCreateSync(mDisplay, EGL_SYNC_FENCE_KHR, nullptr);
        glFlush();
        if (sync == EGL_NO_SYNC_KHR) {
            // Failed synchronization rejects the session; completion precedes ownership return.
            glFinish();
            LOG_ALWAYS_FATAL_IF(glGetError() != GL_NO_ERROR,
                                "HAL1 GPU completion is unproven after EGL fence rejection");
            mError = INVALID_OPERATION;
            ALOGE("ION recording EGL completion fence creation fails 0x%x", eglGetError());
        }
        queueRelease(mDrawing, sync);
        mDrawing = {};
        mDrawIdle.notify_all();
        return mError;
    }

    void deactivate() override {
        {
            std::unique_lock<std::mutex> lock(mLock);
            mActive = false;
            for (const Frame& frame : mPending) queueRelease(frame, EGL_NO_SYNC_KHR);
            mPending.clear();
            mDrawIdle.wait(lock, [this] { return mDrawing.slot == nullptr; });
        }
        std::unique_lock<std::mutex> releaseLock(mReleaseLock);
        mReleaseIdle.wait(releaseLock, [this] { return mReleaseQueue.empty() && !mReleasing; });
    }

    status_t destroyImages(JNIEnv* env) {
        deactivate();
        std::lock_guard<std::mutex> lock(mLock);
        for (auto& entry : mSlots) {
            if (entry.second.image != EGL_NO_IMAGE_KHR) {
                if (mDestroyImage(mDisplay, entry.second.image) != EGL_TRUE) {
                    mError = INVALID_OPERATION;
                    ALOGE("ION recording eglDestroyImageKHR fails 0x%x", eglGetError());
                }
            }
        }
        mSlots.clear();
        if (mManager != nullptr) env->DeleteGlobalRef(mManager);
        mManager = nullptr;
        ALOGI("ION recording %ux%u received=%" PRIu64 " bound=%" PRIu64 " returned=%" PRIu64
              " dropped=%" PRIu64 " images=%" PRIu64 " fence-errors=%" PRIu64
              " held-peak=%zu CPU-copy=0",
              mWidth, mHeight, mReceived, mBound, mReturned, mDropped, mImages, mFenceErrors,
              mHeldPeak);
        return mError;
    }

   private:
    static constexpr size_t kPoolSize = 16;
    static constexpr size_t kQueueDepth = 5;
    static constexpr size_t kHeldBudget = 6;
    struct Slot {
        sp<IMemory> memory;
        sp<GraphicBuffer> buffer;
        EGLImageKHR image = EGL_NO_IMAGE_KHR;
        bool held = false;
    };
    struct Frame {
        sp<Camera> camera;
        sp<IMemory> memory;
        Slot* slot = nullptr;
        nsecs_t timestamp = 0;
    };
    struct Release {
        Frame frame;
        EGLSyncKHR fence = EGL_NO_SYNC_KHR;
    };
    void notifyFrame() {
        JNIEnv* env = AndroidRuntime::getJNIEnv();
        if (mManager != nullptr && env != nullptr) env->CallVoidMethod(mManager, mNotify);
    }
    void queueRelease(const Frame& frame, EGLSyncKHR fence) {
        std::lock_guard<std::mutex> lock(mReleaseLock);
        mReleaseQueue.push_back({frame, fence});
        mReleaseReady.notify_one();
    }
    void releaseLoop() {
        setpriority(PRIO_PROCESS, 0, ANDROID_PRIORITY_URGENT_DISPLAY);
        std::unique_lock<std::mutex> releaseLock(mReleaseLock);
        for (;;) {
            mReleaseReady.wait(releaseLock, [this] { return mQuit || !mReleaseQueue.empty(); });
            if (mReleaseQueue.empty()) {
                eglReleaseThread();
                return;
            }
            Release next = std::move(mReleaseQueue.front());
            mReleaseQueue.pop_front();
            mReleasing = true;
            releaseLock.unlock();
            if (next.fence != EGL_NO_SYNC_KHR) {
                EGLint fenceStatus = mWaitSync(mDisplay, next.fence, 0, 100000000);
                if (fenceStatus != EGL_CONDITION_SATISFIED_KHR) {
                    {
                        std::lock_guard<std::mutex> lock(mLock);
                        ++mFenceErrors;
                        mError = TIMED_OUT;
                    }
                    ALOGE("ION recording EGL fence wait fails 0x%x; retaining HAL ownership",
                          fenceStatus);
                    // A timeout cannot authorize CPP writes while GPU reads remain pending.
                    fenceStatus = mWaitSync(mDisplay, next.fence, 0, EGL_FOREVER_KHR);
                    LOG_ALWAYS_FATAL_IF(fenceStatus != EGL_CONDITION_SATISFIED_KHR,
                                        "HAL1 recording fence completion is unproven: 0x%x",
                                        fenceStatus);
                }
                if (mDestroySync(mDisplay, next.fence) != EGL_TRUE) {
                    std::lock_guard<std::mutex> lock(mLock);
                    ++mFenceErrors;
                    mError = INVALID_OPERATION;
                    ALOGE("ION recording EGL completion fence destruction fails 0x%x",
                          eglGetError());
                }
            }
            {
                std::lock_guard<std::mutex> lock(mLock);
                if (next.frame.slot != nullptr) {
                    next.frame.slot->held = false;
                    --mHeld;
                }
            }
            next.frame.camera->releaseRecordingFrame(next.frame.memory);
            {
                std::lock_guard<std::mutex> lock(mLock);
                ++mReturned;
            }
            next = {};
            releaseLock.lock();
            mReleasing = false;
            if (mReleaseQueue.empty()) mReleaseIdle.notify_all();
        }
    }

    const uint32_t mWidth;
    const uint32_t mHeight;
    const EGLDisplay mDisplay;
    jobject mManager;
    jmethodID mNotify = nullptr;
    LegacyCameraIonBuffer mImporter;
    std::mutex mLock;
    std::condition_variable mDrawIdle;
    std::map<const IBinder*, Slot> mSlots;
    std::deque<Frame> mPending;
    Frame mDrawing;
    bool mActive = true;
    status_t mError = NO_ERROR;
    uint64_t mReceived = 0;
    uint64_t mBound = 0;
    uint64_t mReturned = 0;
    uint64_t mDropped = 0;
    uint64_t mImages = 0;
    uint64_t mFenceErrors = 0;
    size_t mHeld = 0;
    size_t mHeldPeak = 0;
    PFNEGLCREATEIMAGEKHRPROC mCreateImage = nullptr;
    PFNEGLDESTROYIMAGEKHRPROC mDestroyImage = nullptr;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC mBindImage = nullptr;
    PFNEGLCREATESYNCKHRPROC mCreateSync = nullptr;
    PFNEGLDESTROYSYNCKHRPROC mDestroySync = nullptr;
    PFNEGLCLIENTWAITSYNCKHRPROC mWaitSync = nullptr;
    std::mutex mReleaseLock;
    std::condition_variable mReleaseReady;
    std::condition_variable mReleaseIdle;
    std::deque<Release> mReleaseQueue;
    bool mQuit = false;
    bool mReleasing = false;
    std::thread mReleaser;
};

}  // namespace android
#endif
