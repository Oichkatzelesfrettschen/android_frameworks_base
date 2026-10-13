"""Exercise restored camera Java and JNI boundary methods outside Android.

TEST_OUTPUT_DIR names an existing retained build directory; JAVA_HOME selects JDK.
"""

import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(os.environ.get("FRAMEWORK_SOURCE", Path(__file__).resolve().parents[3]))


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    position = opening + 1
    while depth:
        depth += (source[position] == "{") - (source[position] == "}")
        position += 1
    return source[start:position]


JAVA_PREFIX = r"""
import java.util.*;
public class CameraBoundaryFixture {
    static final String TAG = "camera";
    static class Log { static void e(String tag,String msg) {} }
    static class EGL14 {
        static final int EGL_SUCCESS=0x3000, EGL_BAD_NATIVE_WINDOW=0x300b, EGL_BAD_SURFACE=0x300d;
        static int pending=EGL_SUCCESS, calls;
        static int eglGetError() { calls++; int value=pending; pending=EGL_SUCCESS; return value; }
    }
    static class LegacyExceptionUtils {
        static final int NO_ERROR=0;
        static class BufferQueueAbandonedException extends Exception {
            private static final long serialVersionUID = 1;
        }
    }
    static class Surface {}
    static class SparseArray<T> { SparseArray(int count) {} void put(int key,T value) {} }
    static class OutputConfiguration {
        static final int ROTATION_0=0;
        int rotation;
        List<Surface> surfaces=Arrays.asList(new Surface());
        int getRotation() { return rotation; }
        List<Surface> getSurfaces() { return surfaces; }
        Surface getSurface() { return surfaces.get(0); }
    }
    static class SessionConfiguration {
        static final int SESSION_REGULAR=0;
        List<OutputConfiguration> outputs=Arrays.asList(new OutputConfiguration());
        int getSessionType() { return SESSION_REGULAR; }
        Object getInputConfiguration() { return null; }
        List<OutputConfiguration> getOutputConfigurations() { return outputs; }
    }
    static class Device {
        int calls;
        int configureOutputs(SparseArray<Surface> outputs,boolean validate) { calls++; return 0; }
    }
    Device mLegacyDevice=new Device();
"""
JAVA_CASES = r"""
    public static void main(String[] args) throws Exception {
        CameraBoundaryFixture fixture=new CameraBoundaryFixture();
        for (int error : new int[]{EGL14.EGL_SUCCESS,EGL14.EGL_BAD_NATIVE_WINDOW,EGL14.EGL_BAD_SURFACE,0x3002}) {
            EGL14.pending=error;
            int before=EGL14.calls;
            int classification=0;
            try { fixture.checkEglDrawError("draw"); }
            catch (LegacyExceptionUtils.BufferQueueAbandonedException expected) { classification=1; }
            catch (IllegalStateException expected) { classification=2; }
            int wanted=error==EGL14.EGL_SUCCESS ? 0 : error==0x3002 ? 2 : 1;
            if (classification!=wanted || EGL14.calls!=before+1) throw new AssertionError("consumed EGL error");
        }
        SessionConfiguration config=new SessionConfiguration();
        for (int rotation : new int[]{0,1,2,3}) {
            config.outputs.get(0).rotation=rotation;
            int before=fixture.mLegacyDevice.calls;
            boolean supported=fixture.isSessionConfigurationSupported(config);
            if (supported!=(rotation==0) || fixture.mLegacyDevice.calls!=before+(rotation==0?1:0))
                throw new AssertionError("rotation admission");
        }
    }
}
"""
CPP_PREFIX = r"""
#include <cstdint>
#include <cstring>
#include <cerrno>
#define ALOGE(...) ((void)0)
#define ALOGV(...) ((void)0)
#define OVERRIDE_SURFACE_ERROR(error) do { if ((error)==-ENODEV) (error)=BAD_VALUE; } while(0)
using jint=int32_t;
using jlong=int64_t;
using jobject=void*;
using status_t=int;
constexpr int BAD_VALUE=-22, NO_ERROR=0, NATIVE_WINDOW_FORMAT=1;
struct JNIEnv {};
struct ANativeWindow {
    int format=1;
    int query(ANativeWindow*,int,int32_t* result) { *result=format; return 0; }
};
template<class T> struct sp {
    T* pointer=nullptr;
    sp()=default;
    sp(T* value):pointer(value) {}
    T* get() { return pointer; }
    T* operator->() { return pointer; }
    bool operator==(std::nullptr_t) const { return pointer==nullptr; }
};
static ANativeWindow window;
static bool hasWindow=true;
static uint64_t usage;
static int queryError, scalingError;
static sp<ANativeWindow> getNativeWindow(JNIEnv*,jobject) { return hasWindow ? &window : nullptr; }
static void jniThrowException(JNIEnv*,const char*,const char*) {}
static int native_window_get_consumer_usage(ANativeWindow*,uint64_t* result) { *result=usage; return queryError; }
static int native_window_set_scaling_mode(ANativeWindow*,int) { return scalingError; }
"""
CPP_CASES = r"""
int main() {
    JNIEnv environment;
    usage=(UINT64_C(1)<<48) | (UINT64_C(1)<<31) | 0x100;
    if (static_cast<uint64_t>(LegacyCameraDevice_nativeDetectSurfaceUsageFlags(&environment,nullptr,nullptr))!=usage) return 1;
    queryError=-ENODEV;
    if (LegacyCameraDevice_nativeDetectSurfaceUsageFlags(&environment,nullptr,nullptr)!=BAD_VALUE) return 2;
    for (int format : {1,2,3,4,5,34}) {
        window.format=format;
        if (LegacyCameraDevice_nativeDetectSurfaceType(&environment,nullptr,nullptr)!=format) return 3;
    }
    scalingError=-ENODEV;
    if (LegacyCameraDevice_nativeSetScalingMode(&environment,nullptr,nullptr,0)!=BAD_VALUE) return 4;
    scalingError=0;
    if (LegacyCameraDevice_nativeSetScalingMode(&environment,nullptr,nullptr,0)!=0) return 5;
    return 0;
}
"""


class LegacyCameraBoundaries(unittest.TestCase):
    def test_java_error_and_rotation_methods(self):
        source = (
            ROOT
            / "core/java/android/hardware/camera2/legacy/SurfaceTextureRenderer.java"
        ).read_text()
        methods = function(source, "private void checkEglDrawError(")
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/CameraDeviceUserShim.java"
        ).read_text()
        methods += function(source, "public boolean isSessionConfigurationSupported(")
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "CameraBoundaryFixture.java"
        fixture.write_text(JAVA_PREFIX + methods + JAVA_CASES)
        java = Path(os.environ["JAVA_HOME"]) / "bin"
        subprocess.run(
            [
                str(java / "javac"),
                "-Xlint:all",
                "-Werror",
                "-d",
                str(output),
                str(fixture),
            ],
            check=True,
        )
        subprocess.run(
            [str(java / "java"), "-cp", str(output), "CameraBoundaryFixture"],
            check=True,
        )

    def test_jni_surface_methods(self):
        source = (
            ROOT / "core/jni/android_hardware_camera2_legacy_LegacyCameraDevice.cpp"
        ).read_text()
        methods = "\n".join(
            function(source, signature)
            for signature in (
                "static jlong LegacyCameraDevice_nativeDetectSurfaceUsageFlags(",
                "static jint LegacyCameraDevice_nativeDetectSurfaceType(",
                "static jint LegacyCameraDevice_nativeSetScalingMode(",
            )
        )
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "camera_surface_boundaries.cpp"
        fixture.write_text(
            "#include <initializer_list>\n" + CPP_PREFIX + methods + CPP_CASES
        )
        binary = output / "camera_surface_boundaries"
        subprocess.run(
            [
                "clang++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-unused-parameter",
                "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all",
                str(fixture),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)

    def test_cross_language_contracts(self):
        root = ROOT / "core/java/android/hardware"
        device = (root / "camera2/legacy/LegacyCameraDevice.java").read_text()
        self.assertIn(
            "public static long detectSurfaceUsageFlags(Surface surface)", device
        )
        self.assertIn(
            "private static native long nativeDetectSurfaceUsageFlags(Surface surface)",
            device,
        )
        native = (
            ROOT / "core/jni/android_hardware_camera2_legacy_LegacyCameraDevice.cpp"
        ).read_text()
        self.assertIn(
            '"nativeDetectSurfaceUsageFlags",\n    "(Landroid/view/Surface;)J"', native
        )
        surface = (root / "camera2/utils/SurfaceUtils.java").read_text()
        self.assertIn(
            "LegacyCameraDevice.detectSurfaceFormat(surface)",
            function(surface, "public static int detectSurfaceFormat("),
        )
        raw = function(device, "public static int detectSurfaceFormat(")
        self.assertIn("nativeDetectSurfaceType(surface)", raw)
        self.assertNotIn("ImageFormat.PRIVATE", raw)
        manager = (root / "camera2/CameraManager.java").read_text()
        self.assertIn("getDisplaySize(), uid, oomScoreOffset);", manager)
        shim = (root / "camera2/legacy/CameraDeviceUserShim.java").read_text()
        self.assertIn("new CameraLooper(cameraId, clientUid, oomScoreOffset)", shim)
        self.assertIn("mClientUid = clientUid;", shim)
        self.assertIn(
            "mCamera.cameraInitUnspecified( mCameraId, mClientUid, mOomScoreOffset)",
            " ".join(shim.split()),
        )
        camera = (root / "Camera.java").read_text()
        self.assertIn(
            "cameraInitVersion(cameraId, CAMERA_HAL_API_VERSION_UNSPECIFIED, clientUid)",
            camera,
        )
        self.assertIn(
            "ActivityThread.currentOpPackageName(), clientUid, oomScoreOffset)", camera
        )
        native = (ROOT / "core/jni/android_hardware_Camera.cpp").read_text()
        self.assertIn(
            "clientUid, Camera::USE_CALLING_PID, oomScoreOffset, camera)", native
        )
        self.assertIn('"(Ljava/lang/Object;IILjava/lang/String;II)I"', native)
        request = (root / "camera2/legacy/RequestThreadManager.java").read_text()
        start = request.index("if (captureInfo == null || captureInfo.first == null) {")
        dropped = request[start : request.index("RequestHolder holder =", start)]
        self.assertLess(
            dropped.index("mReceivedJpeg.open();"), dropped.index("return;")
        )


if __name__ == "__main__":
    unittest.main()
