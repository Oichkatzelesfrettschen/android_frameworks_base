"""Exercise production legacy frame ownership and capture-intent diagnostics."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function


CPP_PREFIX = r'''
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cerrno>
#include <cstdlib>
#include <initializer_list>
#define ALOGE(...) ((void)0)
#define ALOGV(...) ((void)0)
#define ALOGW(...) ((void)0)
#define ATRACE_CALL() ((void)0)
#define LOG_ALWAYS_FATAL_IF(condition,...) do { if (condition) std::abort(); } while (0)
#define ALIGN(value,alignment) (((value)+(alignment)-1)&~((alignment)-1))
#define OVERRIDE_SURFACE_ERROR(error) do { if ((error)==-ENODEV) (error)=BAD_VALUE; } while(0)
using status_t=int;
constexpr int BAD_VALUE=-22, NO_ERROR=0, NATIVE_WINDOW_FORMAT=1;
constexpr int HAL_PIXEL_FORMAT_YCrCb_420_SP=17, HAL_PIXEL_FORMAT_YV12=842094169;
constexpr int HAL_PIXEL_FORMAT_YCbCr_420_888=35, HAL_PIXEL_FORMAT_BLOB=33;
constexpr int HAL_PIXEL_FORMAT_RGBA_8888=1, GRALLOC_USAGE_SW_WRITE_OFTEN=48;
constexpr uint16_t CAMERA3_JPEG_BLOB_ID=255;
struct camera3_jpeg_blob { uint16_t jpeg_blob_id; uint32_t jpeg_size; };
struct android_ycbcr { void* y=nullptr; void* cb=nullptr; void* cr=nullptr; };
struct ANativeWindowBuffer {};
static ANativeWindowBuffer nativeBuffer;
static int locks, unlocks;
struct ANativeWindow {
    bool dequeued=false;
    int queueError=0, cancelError=0, queues=0, cancels=0, dequeues=0;
    int query(ANativeWindow*,int,int32_t* format) { *format=HAL_PIXEL_FORMAT_BLOB; return 0; }
    int queueBuffer(ANativeWindow* window,ANativeWindowBuffer* buffer,int fence) {
        if (window!=this || buffer!=&nativeBuffer || fence!=-1 || !dequeued) std::abort();
        queues++;
        if (!queueError) dequeued=false;
        return queueError;
    }
    int cancelBuffer(ANativeWindow* window,ANativeWindowBuffer* buffer,int fence) {
        if (window!=this || buffer!=&nativeBuffer || fence!=-1 || !dequeued) std::abort();
        cancels++;
        if (!cancelError) dequeued=false;
        return cancelError;
    }
};
int native_window_dequeue_buffer_and_wait(ANativeWindow* window,ANativeWindowBuffer** buffer) {
    if (window->dequeued) return -EBUSY;
    window->dequeued=true; window->dequeues++; *buffer=&nativeBuffer; return 0;
}
template<class T> struct sp {
    T* pointer;
    sp(T* value):pointer(value) {}
    T* get() const { return pointer; }
    T* operator->() const { return pointer; }
    bool operator==(std::nullptr_t) const { return pointer==nullptr; }
};
struct GraphicBuffer {
    uint8_t bytes[128]{};
    static GraphicBuffer* from(ANativeWindowBuffer*) { static GraphicBuffer instance; return &instance; }
    uint32_t getWidth() { return 64; }
    uint32_t getHeight() { return 1; }
    uint32_t getStride() { return 64; }
    int lock(int,void** address) { locks++; *address=bytes; return 0; }
    int lockYCbCr(int,android_ycbcr*) { locks++; return 0; }
    int unlock() { unlocks++; return 0; }
    ANativeWindowBuffer* getNativeBuffer() { return &nativeBuffer; }
};
template<typename... Arguments> void rgbToYuv420(Arguments&&...) {}
'''
CPP_CASES = r'''
int main() {
    uint8_t input[16]{};
    for (int error : {-EIO,-ENODEV,-EINVAL,-EBUSY,0}) {
        ANativeWindow window;
        window.queueError=error;
        locks=unlocks=0;
        int result=produceFrame(&window,input,64,1,HAL_PIXEL_FORMAT_BLOB,sizeof(input));
        int expected=error==-ENODEV ? BAD_VALUE : error;
        if (result!=expected || window.dequeued || window.cancels!=(error?1:0) ||
            locks!=1 || unlocks!=1 || window.queues!=1) return 1;
        window.queueError=0;
        if (produceFrame(&window,input,64,1,HAL_PIXEL_FORMAT_BLOB,sizeof(input))!=0 ||
            window.dequeues!=2 || window.queues!=2 || window.dequeued || unlocks!=2) return 2;
    }
    ANativeWindow abandoned;
    abandoned.queueError=-EIO;
    abandoned.cancelError=-ENODEV;
    if (produceFrame(&abandoned,input,64,1,HAL_PIXEL_FORMAT_BLOB,sizeof(input))!=-EIO ||
        abandoned.cancels!=1 || !abandoned.dequeued) return 3;
    return 0;
}
'''
JAVA_PREFIX = r'''
import java.util.ArrayList;
import java.util.List;
public class CaptureIntentFixture {
    static final String TAG="camera";
    static final int CONTROL_CAPTURE_INTENT_CUSTOM=0, CONTROL_CAPTURE_INTENT_PREVIEW=1;
    static final int CONTROL_CAPTURE_INTENT_STILL_CAPTURE=2, CONTROL_CAPTURE_INTENT_VIDEO_RECORD=3;
    static final int CONTROL_CAPTURE_INTENT_VIDEO_SNAPSHOT=4, CONTROL_CAPTURE_INTENT_ZERO_SHUTTER_LAG=5;
    static final int CONTROL_CAPTURE_INTENT_MANUAL=6;
    static class Log {
        static final List<String> warnings=new ArrayList<>();
        static void w(String tag,String message) { warnings.add(message); }
    }
'''
JAVA_CASES = r'''
    public static void main(String[] args) {
        for (int value : new int[]{0,1,2,3,4,5,6,99}) {
            Log.warnings.clear();
            int result=filterSupportedCaptureIntent(value);
            if (value<=4) {
                if (result!=value || !Log.warnings.isEmpty()) throw new AssertionError("supported intent changes");
            } else {
                if (result!=CONTROL_CAPTURE_INTENT_PREVIEW || Log.warnings.size()!=1 ||
                    !Log.warnings.get(0).contains("value " + value + ";") ||
                    !Log.warnings.get(0).startsWith(value==99 ? "Unknown" : "Unsupported"))
                    throw new AssertionError("rejected intent diagnostic " + value + ": " + Log.warnings);
            }
        }
    }
}
'''


class LegacyCameraQueue(unittest.TestCase):
    def test_queue_failure_returns_dequeued_buffer(self):
        source = (ROOT / "core/jni/android_hardware_camera2_legacy_LegacyCameraDevice.cpp").read_text()
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "camera_queue_ownership.cpp"
        fixture.write_text(CPP_PREFIX + function(source, "static status_t produceFrame(") + CPP_CASES)
        executable = output / "camera_queue_ownership"
        subprocess.run(["clang++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        str(fixture), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)

    def test_capture_intent_logs_original_value_once(self):
        source = (ROOT / "core/java/android/hardware/camera2/legacy/LegacyRequestMapper.java").read_text()
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "CaptureIntentFixture.java"
        fixture.write_text(JAVA_PREFIX + function(source, "static int filterSupportedCaptureIntent(") + JAVA_CASES)
        java = Path(os.environ["JAVA_HOME"]) / "bin"
        subprocess.run([str(java / "javac"), "-Xlint:all", "-Werror", "-d", str(output), str(fixture)], check=True)
        subprocess.run([str(java / "java"), "-cp", str(output), "CaptureIntentFixture"], check=True)


if __name__ == "__main__":
    unittest.main()
