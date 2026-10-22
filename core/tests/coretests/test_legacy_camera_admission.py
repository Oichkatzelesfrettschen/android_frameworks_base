"""Exercise admitted surface sets, JNI cleanup status, and public AE metadata."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function, CPP_PREFIX


RENDER_PREFIX = r"""
import java.util.*;
public class SurfaceAdmissionFixture {
    static final String TAG="camera", LEGACY_PERF_PROPERTY="perf";
    static class Log { static void w(String tag,String message,Object... detail) {} }
    static class LegacyExceptionUtils {
        static class BufferQueueAbandonedException extends Exception {
            private static final long serialVersionUID=1;
        }
    }
    static class Surface {
        final int mode;
        Surface(int value) { mode=value; }
    }
    static class Size { int getWidth() { return 640; } int getHeight() { return 480; } }
    static class Pair<A,B> {
        final A first; final B second;
        Pair(A left,B right) { first=left; second=right; }
    }
    static class EGLSurfaceHolder { Surface surface; int width,height; Object eglSurface; }
    static class SurfaceTexture { SurfaceTexture(int texture) {} }
    static class SystemProperties { static boolean getBoolean(String key,boolean value) { return value; } }
    static class LegacyCameraDevice {
        static void disconnectSurface(Surface surface) throws LegacyExceptionUtils.BufferQueueAbandonedException {}
        static boolean needsConversion(Surface surface) throws LegacyExceptionUtils.BufferQueueAbandonedException {
            if (surface.mode==1) throw new LegacyExceptionUtils.BufferQueueAbandonedException();
            return surface.mode>=2;
        }
        static void connectSurface(Surface surface) throws LegacyExceptionUtils.BufferQueueAbandonedException {
            if (surface.mode==2) throw new LegacyExceptionUtils.BufferQueueAbandonedException();
        }
    }
    final List<EGLSurfaceHolder> mSurfaces=new ArrayList<>(), mConversionSurfaces=new ArrayList<>();
    SurfaceTexture mSurfaceTexture;
    int contexts,regular,converted,current,initialized;
    void releaseEGLContext() { mSurfaces.clear(); mConversionSurfaces.clear(); mSurfaceTexture=null; }
    void configureEGLContext() { contexts++; }
    void configureEGLOutputSurfaces(Collection<EGLSurfaceHolder> surfaces) { regular+=surfaces.size(); }
    void configureEGLPbufferSurfaces(Collection<EGLSurfaceHolder> surfaces) { converted+=surfaces.size(); }
    void destroyOutputSurface(EGLSurfaceHolder holder) {}
    void makeCurrent(Object surface) throws LegacyExceptionUtils.BufferQueueAbandonedException { current++; }
    void initializeGLState() { initialized++; }
    int getTextureId() { return 1; }
    void setupGlTiming() {}
"""
RENDER_CASES = r"""
    static List<Pair<Surface,Size>> outputs(int... modes) {
        List<Pair<Surface,Size>> result=new ArrayList<>();
        for (int mode : modes) result.add(new Pair<>(new Surface(mode),new Size()));
        return result;
    }
    public static void main(String[] args) {
        for (int[] modes : new int[][]{{},{1},{1,1},{2},{1,2},{0},{3},{1,0,2,3}}) {
            SurfaceAdmissionFixture fixture=new SurfaceAdmissionFixture();
            boolean accepted=fixture.configureSurfaces(outputs(modes));
            int regular=0,converted=0;
            for (int mode : modes) { if (mode==0) regular++; if (mode==3) converted++; }
            int admitted=regular+converted>0 ? 1 : 0;
            boolean exact=regular+converted==modes.length;
            if (fixture.contexts!=admitted || fixture.regular!=regular || fixture.converted!=converted ||
                fixture.current!=admitted || fixture.initialized!=(exact ? admitted : 0) ||
                accepted!=exact || (fixture.mSurfaceTexture!=null)!=(exact && admitted==1)) throw new AssertionError(Arrays.toString(modes));
            fixture.configureSurfaces(outputs(1,2));
            if (!fixture.mSurfaces.isEmpty() || !fixture.mConversionSurfaces.isEmpty() ||
                fixture.mSurfaceTexture!=null || fixture.contexts!=admitted)
                throw new AssertionError("abandoned reconfiguration retains outputs");
            fixture.configureSurfaces(outputs(0));
            if (fixture.contexts!=admitted+1 || fixture.mSurfaceTexture==null)
                throw new AssertionError("valid reconfiguration fails");
        }
        new SurfaceAdmissionFixture().configureSurfaces(null);
    }
}
"""
METADATA_PREFIX = r"""
import java.util.*;
public class AntibandingFixture {
    static final String TAG="camera";
    static boolean DEBUG;
    static final int CONTROL_AE_ANTIBANDING_MODE_OFF=0, CONTROL_AE_ANTIBANDING_MODE_50HZ=1;
    static final int CONTROL_AE_ANTIBANDING_MODE_60HZ=2, CONTROL_AE_ANTIBANDING_MODE_AUTO=3;
    static final int CONTROL_AE_MODE_ON=1, CONTROL_AE_MODE_ON_AUTO_FLASH=2;
    static final int CONTROL_AE_MODE_ON_ALWAYS_FLASH=3, CONTROL_AE_MODE_ON_AUTO_FLASH_REDEYE=4;
    static final String CONTROL_AE_AVAILABLE_ANTIBANDING_MODES="antibanding";
    static final String CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES="fps", CONTROL_AE_AVAILABLE_MODES="ae";
    static final String CONTROL_AE_COMPENSATION_RANGE="compensation", CONTROL_AE_COMPENSATION_STEP="step";
    static final String CONTROL_AE_LOCK_AVAILABLE="lock";
    static class Log { static void v(String tag,String message) {} static void w(String tag,String message) {} }
    static class CameraMetadataNative {
        final Map<String,Object> values=new HashMap<>();
        void set(String key,Object value) { values.put(key,value); }
    }
    static class Range<T> { static <T> Range<T> create(T min,T max) { return new Range<T>(); } }
    static class ParamsUtils { static Object createRational(float value) { return value; } }
    static class ArrayUtils {
        static int[] convertStringListToIntArray(List<String> values,String[] names,int[] modes) { return null; }
    }
    static class Camera {
        static class Parameters {
            static final String ANTIBANDING_OFF="off", ANTIBANDING_50HZ="50hz";
            static final String ANTIBANDING_60HZ="60hz", ANTIBANDING_AUTO="auto";
            static final String FLASH_MODE_OFF="off", FLASH_MODE_AUTO="auto", FLASH_MODE_ON="on";
            static final String FLASH_MODE_RED_EYE="red-eye", FLASH_MODE_TORCH="torch";
            static final int PREVIEW_FPS_MIN_INDEX=0, PREVIEW_FPS_MAX_INDEX=1;
            List<String> modes;
            List<String> getSupportedAntibanding() { return modes; }
            List<int[]> getSupportedPreviewFpsRange() { return Collections.singletonList(new int[]{15000,30000}); }
            List<String> getSupportedFlashModes() { return null; }
            int getMinExposureCompensation() { return -2; }
            int getMaxExposureCompensation() { return 2; }
            float getExposureCompensationStep() { return 0.5f; }
            boolean isAutoExposureLockSupported() { return true; }
        }
    }
"""
METADATA_CASES = r"""
    public static void main(String[] args) {
        for (boolean debug : new boolean[]{false,true}) {
            DEBUG=debug;
            for (List<String> modes : Arrays.asList(null, Collections.<String>emptyList(),
                    Arrays.asList("vendor",null), Arrays.asList("off","50hz","60hz","auto"),
                    Arrays.asList("vendor","50hz",null,"auto"))) {
                Camera.Parameters parameters=new Camera.Parameters();
                parameters.modes=modes;
                CameraMetadataNative metadata=new CameraMetadataNative();
                mapControlAe(metadata,parameters);
                int[] result=(int[])metadata.values.get(CONTROL_AE_AVAILABLE_ANTIBANDING_MODES);
                int[] expected=modes==null || modes.isEmpty() || modes.size()==2 ? new int[]{} :
                    modes.get(0).equals("off") ? new int[]{0,1,2,3} : new int[]{1,3};
                if (!Arrays.equals(result,expected)) throw new AssertionError(Arrays.toString(result));
                if (metadata.values.size()!=6) throw new AssertionError("remaining AE fields lost");
            }
        }
    }
}
"""
DISCONNECT_CASES = r"""
int main() {
    JNIEnv environment;
    for (int error : {0,-ENODEV,-EINVAL,-EIO}) {
        disconnectError=error;
        pendingException=false;
        disconnectCalls=0;
        int result=LegacyCameraDevice_nativeDisconnectSurface(&environment,nullptr,&window);
        if (result!=(error==-ENODEV ? BAD_VALUE : error) || pendingException || disconnectCalls!=1) return 1;
    }
    disconnectCalls=0;
    if (LegacyCameraDevice_nativeDisconnectSurface(&environment,nullptr,nullptr)!=0 || disconnectCalls) return 2;
    hasWindow=false;
    if (LegacyCameraDevice_nativeDisconnectSurface(&environment,nullptr,&window)!=0 || disconnectCalls) return 3;
    return 0;
}
"""


CONNECT_PREFIX = r"""
#include <cerrno>
#include <cstdint>
#include <cstddef>
#include <initializer_list>
#define ALOGE(...) ((void)0)
#define ALOGV(...) ((void)0)
#define OVERRIDE_SURFACE_ERROR(error) do { if ((error)==-ENODEV) (error)=BAD_VALUE; } while(0)
using status_t=int;
constexpr int OK=0, NO_ERROR=0, BAD_VALUE=-EINVAL, NATIVE_WINDOW_API_CAMERA=2;
constexpr int GRALLOC_USAGE_SW_WRITE_OFTEN=48, NATIVE_WINDOW_MIN_UNDEQUEUED_BUFFERS=1;
static int failureStage,errorCode,disconnectError,disconnectCalls;
static bool connected;
struct ANativeWindow {
    int query(ANativeWindow*,int,int* value) { *value=2; return failureStage==3 ? errorCode : 0; }
};
struct Surface : ANativeWindow {
    int connect(int,void*) { if (failureStage==1) return errorCode; connected=true; return 0; }
    int disconnect(int) { disconnectCalls++; if (!disconnectError) connected=false; return disconnectError; }
};
template<class T> struct sp {
    T* pointer;
    sp(T* value):pointer(value) {}
    T* get() const { return pointer; }
    T* operator->() const { return pointer; }
};
static int native_window_set_usage(Surface*,int) { return failureStage==2 ? errorCode : 0; }
static int native_window_set_buffer_count(Surface*,int) { return failureStage==4 ? errorCode : 0; }
"""
CONNECT_CASES = r"""
int main() {
    Surface surface;
    for (int failure : {0,1,2,3,4}) {
        for (int error : {-EIO,-ENODEV,-EINVAL}) {
            failureStage=failure; errorCode=error;
            connected=false; disconnectCalls=0; disconnectError=0;
            int result=connectSurface(&surface,2);
            int expected=failure ? (failure>1 && error==-ENODEV ? BAD_VALUE : error) : 0;
            if (result!=expected || connected!=(failure==0) || disconnectCalls!=(failure>1 ? 1 : 0)) return 1;
            if (failure>1) {
                failureStage=0;
                if (connectSurface(&surface,2)!=0 || !connected) return 2;
            }
        }
    }
    failureStage=1; errorCode=-EBUSY; connected=true; disconnectCalls=0;
    if (connectSurface(&surface,2)!=-EBUSY || !connected || disconnectCalls) return 3;
    failureStage=2; errorCode=-EIO; disconnectError=-ENODEV; disconnectCalls=0;
    if (connectSurface(&surface,2)!=-EIO || disconnectCalls!=1) return 4;
    return 0;
}
"""


class LegacyCameraAdmission(unittest.TestCase):
    def run_java(self, name, content):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / (name + ".java")
        fixture.write_text(content)
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
        subprocess.run([str(java / "java"), "-cp", str(output), name], check=True)

    def test_empty_and_partial_surface_admission(self):
        source = (
            ROOT
            / "core/java/android/hardware/camera2/legacy/SurfaceTextureRenderer.java"
        ).read_text()
        self.run_java(
            "SurfaceAdmissionFixture",
            RENDER_PREFIX
            + function(source, "public boolean configureSurfaces(")
            + function(source, "private boolean makeCurrentOutput(")
            + RENDER_CASES,
        )

    def test_antibanding_filters_unknown_and_null_in_release(self):
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/LegacyMetadataMapper.java"
        ).read_text()
        methods = '@SuppressWarnings({"unchecked", "rawtypes"})\n' + function(
            source, "private static void mapControlAe("
        )
        methods += function(source, "private static int convertAntiBandingMode(")
        self.run_java("AntibandingFixture", METADATA_PREFIX + methods + METADATA_CASES)

    def test_connect_rolls_back_only_owned_producer(self):
        source = (
            ROOT / "core/jni/android_hardware_camera2_legacy_LegacyCameraDevice.cpp"
        ).read_text()
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "camera_connect_rollback.cpp"
        fixture.write_text(
            CONNECT_PREFIX
            + function(source, "static status_t connectSurface(")
            + CONNECT_CASES
        )
        executable = output / "camera_connect_rollback"
        subprocess.run(
            [
                "clang++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all",
                str(fixture),
                "-o",
                str(executable),
            ],
            check=True,
        )
        subprocess.run([str(executable)], check=True)

    def test_disconnect_returns_status_without_pending_exception(self):
        source = (
            ROOT / "core/jni/android_hardware_camera2_legacy_LegacyCameraDevice.cpp"
        ).read_text()
        prefix = CPP_PREFIX.replace(", NATIVE_WINDOW_FORMAT=1", "").replace(
            "static void jniThrowException(JNIEnv*,const char*,const char*) {}",
            "static bool pendingException;\n[[maybe_unused]] static void jniThrowException(JNIEnv*,const char*,const char*) { pendingException=true; }",
        )
        prefix = prefix.replace(
            "static int native_window_", "[[maybe_unused]] static int native_window_"
        )
        prefix += "\nconstexpr int NATIVE_WINDOW_API_CAMERA=2;\nstatic int disconnectError,disconnectCalls;\n"
        prefix += "static int native_window_api_disconnect(ANativeWindow*,int) { disconnectCalls++; return disconnectError; }\n"
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "camera_disconnect_status.cpp"
        fixture.write_text(
            "#include <initializer_list>\n"
            + prefix
            + function(
                source, "static jint LegacyCameraDevice_nativeDisconnectSurface("
            )
            + DISCONNECT_CASES
        )
        executable = output / "camera_disconnect_status"
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
                str(executable),
            ],
            check=True,
        )
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
