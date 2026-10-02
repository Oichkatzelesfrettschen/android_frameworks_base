#ifndef ANDROID_LEGACY_CAMERA_ION_BUFFER_H
#define ANDROID_LEGACY_CAMERA_ION_BUFFER_H

#include <cutils/native_handle.h>
#include <limits.h>
#include <ui/GraphicBuffer.h>
#include <unistd.h>

namespace android {

// msm8974 gralloc0 stores the Venus stride and scanlines in its private handle.
// A real allocation supplies the metadata fd and verifies the ABI before import.
class LegacyCameraIonBuffer {
   public:
    static constexpr PixelFormat kVenusFormat = 0x7fa30c04;
    static constexpr uint64_t kUsage = GRALLOC_USAGE_HW_TEXTURE | GRALLOC_USAGE_SW_WRITE_OFTEN;

    status_t initialize(uint32_t width, uint32_t height) {
        mTemplate =
            new GraphicBuffer(width, height, kVenusFormat, 1, kUsage, "HAL1 recording ION import");
        status_t error = mTemplate->initCheck();
        if (error != NO_ERROR) return error;
        const native_handle_t* handle = mTemplate->handle;
        if (width > INT_MAX - 127u || height > INT_MAX - 31u || handle == nullptr ||
            handle->version != sizeof(native_handle_t) || handle->numFds != 2 ||
            handle->numInts != 12 || handle->data[2] != 0x676d736d || handle->data[4] <= 0 ||
            handle->data[5] != 0 || handle->data[8] != 0 || handle->data[10] != kVenusFormat ||
            handle->data[11] != static_cast<int>((width + 127u) & ~127u) ||
            handle->data[12] != static_cast<int>((height + 31u) & ~31u) ||
            (handle->data[3] & 0x208) != 0x208) {
            mTemplate.clear();
            return BAD_VALUE;
        }
        return NO_ERROR;
    }

    size_t allocationSize() const { return static_cast<size_t>(mTemplate->handle->data[4]); }

    sp<GraphicBuffer> import(int ionFd, size_t size, size_t offset) const {
        if (mTemplate == nullptr || ionFd < 0 || offset != 0 || size != allocationSize()) {
            return nullptr;
        }
        native_handle_t* handle = native_handle_clone(mTemplate->handle);
        if (handle == nullptr) return nullptr;
        close(handle->data[0]);
        handle->data[0] = dup(ionFd);
        if (handle->data[0] < 0) {
            native_handle_close(handle);
            native_handle_delete(handle);
            return nullptr;
        }
        handle->data[3] |= 0x80;  // PRIV_FLAGS_NON_CPU_WRITER: CPP writes the ION allocation.
        handle->data[7] = 0;      // registerBuffer establishes the app-local pixel mapping.
        handle->data[9] = 0;      // EGL establishes the app-local GPU address.
        handle->data[13] = 0;     // registerBuffer establishes the metadata mapping.
        sp<GraphicBuffer> buffer = new GraphicBuffer(
            handle, GraphicBuffer::CLONE_HANDLE, mTemplate->getWidth(), mTemplate->getHeight(),
            kVenusFormat, 1, kUsage, mTemplate->getStride());
        native_handle_close(handle);
        native_handle_delete(handle);
        return buffer->initCheck() == NO_ERROR ? buffer : nullptr;
    }

   private:
    sp<GraphicBuffer> mTemplate;
};

}  // namespace android
#endif
