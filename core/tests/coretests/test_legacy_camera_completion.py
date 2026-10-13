"""Run the production collector and legacy-open test bodies with deterministic peers."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function


STUBS = {
    "android/util/Log.java": """package android.util; public class Log {
        public static void d(String tag,String message) {} public static void w(String tag,String message) {}
        public static void e(String tag,String message) {} }""",
    "android/util/MutableLong.java": "package android.util; public class MutableLong { public long value; public MutableLong(long value) { this.value=value; } }",
    "android/util/Pair.java": "package android.util; public class Pair<A,B> { public final A first; public final B second; public Pair(A first,B second) { this.first=first; this.second=second; } }",
    "android/view/Surface.java": "package android.view; public class Surface { public final boolean jpeg; public Surface(boolean jpeg) { this.jpeg=jpeg; } }",
    "android/hardware/camera2/impl/CameraDeviceImpl.java": """package android.hardware.camera2.impl;
        public class CameraDeviceImpl { public static class CameraDeviceCallbacks {
        public static final int ERROR_CAMERA_REQUEST=3, ERROR_CAMERA_BUFFER=5; } }""",
    "android/hardware/camera2/legacy/LegacyExceptionUtils.java": """package android.hardware.camera2.legacy;
        public class LegacyExceptionUtils { public static class BufferQueueAbandonedException extends Exception {
        private static final long serialVersionUID=1; } }""",
    "android/hardware/camera2/legacy/LegacyRequest.java": "package android.hardware.camera2.legacy; public class LegacyRequest {}",
    "android/hardware/camera2/legacy/RequestHolder.java": """package android.hardware.camera2.legacy;
        import java.util.*; import android.view.Surface;
        public class RequestHolder {
            public final long frame; public boolean failed,abandoned;
            public final List<Surface> targets;
            public RequestHolder(long frame,boolean preview,boolean jpeg) {
                this.frame=frame; targets=new ArrayList<>();
                if (preview) targets.add(new Surface(false)); if (jpeg) targets.add(new Surface(true));
            }
            public boolean hasJpegTargets() { return targets.stream().anyMatch(surface->surface.jpeg); }
            public boolean hasPreviewTargets() { return targets.stream().anyMatch(surface->!surface.jpeg); }
            public void failRequest() { failed=true; }
            public RequestHolder getRequest() { return this; }
            public List<Surface> getTargets() { return targets; }
            public boolean jpegType(Surface surface) throws LegacyExceptionUtils.BufferQueueAbandonedException { return surface.jpeg; }
            public int getRequestId() { return (int)frame; } public long getFrameNumber() { return frame; }
            public void setOutputAbandoned() { abandoned=true; }
        }""",
    "android/hardware/camera2/legacy/CameraDeviceState.java": """package android.hardware.camera2.legacy;
        import android.view.Surface;
        public class CameraDeviceState {
            public static final int NO_CAPTURE_ERROR=-1;
            public int starts,requestErrors,bufferErrors;
            public void setCaptureStart(RequestHolder request,long timestamp,int error) {
                starts++; if (error==3) requestErrors++; else if(error!=-1) throw new AssertionError(error);
            }
            public void setCaptureResult(RequestHolder request,Object result,int error,Surface surface) {
                if (error!=5 || starts==0) throw new AssertionError("buffer error before capture"); bufferErrors++;
            }
        }""",
}
COLLECTOR_CASES = r"""
package android.hardware.camera2.legacy;
import android.util.MutableLong;
import java.util.concurrent.TimeUnit;
public class CaptureCompletionFixture {
    static void require(boolean value,String message) { if (!value) throw new AssertionError(message); }
    static void completed(CaptureCollector collector,RequestHolder request) throws Exception {
        require(collector.waitForEmpty(0,TimeUnit.NANOSECONDS),"in-flight request remains after failAll");
        require(collector.waitForPreviewsEmpty(0,TimeUnit.NANOSECONDS),"preview remains after failAll");
        MutableLong timestamp=new MutableLong(-1);
        require(collector.waitForRequestCompleted(request,0,TimeUnit.NANOSECONDS,timestamp),"completion missing");
        require(!collector.waitForRequestCompleted(request,0,TimeUnit.NANOSECONDS,timestamp),"duplicate completion");
        collector.failAll();
        require(collector.waitForEmpty(0,TimeUnit.NANOSECONDS),"repeat failure changes count");
        RequestHolder reopened=new RequestHolder(request.frame+1,true,false);
        require(collector.queueRequest(reopened,new LegacyRequest(),0,TimeUnit.NANOSECONDS),"capacity remains blocked");
        collector.previewCaptured(456); collector.previewProduced();
        require(collector.waitForRequestCompleted(reopened,0,TimeUnit.NANOSECONDS,timestamp),"next preview fails");
        require(timestamp.value==456,"next preview timestamp");
    }
    public static void main(String[] args) throws Exception {
        for (boolean failPreview : new boolean[]{true,false}) {
            for (int progress=0; progress<3; progress++) {
                CameraDeviceState state=new CameraDeviceState();
                CaptureCollector collector=new CaptureCollector(2,state);
                RequestHolder request=new RequestHolder(10,true,true);
                require(collector.queueRequest(request,new LegacyRequest(),0,TimeUnit.NANOSECONDS),"queue mixed");
                if (progress==1) { collector.jpegCaptured(123); collector.previewCaptured(123); }
                if (progress==2) { collector.jpegProduced(); collector.previewProduced(); }
                if (failPreview) collector.failNextPreview(); else collector.failNextJpeg();
                require(!collector.waitForEmpty(0,TimeUnit.NANOSECONDS),"mixed request completes before remaining output");
                collector.failAll();
                require(state.starts==1,"capture start count");
                require(state.requestErrors==(progress==1 ? 0 : 1),"request failure classification");
                require(state.bufferErrors==(progress==1 ? 2 : 0),"buffer failure classification");
                completed(collector,request);
            }
        }
        CameraDeviceState state=new CameraDeviceState();
        CaptureCollector collector=new CaptureCollector(2,state);
        RequestHolder request=new RequestHolder(20,true,true);
        require(collector.queueRequest(request,new LegacyRequest(),0,TimeUnit.NANOSECONDS),"queue lost preview");
        collector.jpegCaptured(123);
        collector.failNextJpeg();
        collector.reportPreviewBufferLost(request,request.targets.get(0));
        require(state.bufferErrors==1 && request.abandoned,"active preview loss notification disappears");
        collector.previewCaptured(123); collector.previewProduced();
        completed(collector,request);
        for (boolean jpeg : new boolean[]{true,false}) {
            collector=new CaptureCollector(2,new CameraDeviceState());
            request=new RequestHolder(30,!jpeg,jpeg);
            require(collector.queueRequest(request,new LegacyRequest(),0,TimeUnit.NANOSECONDS),"queue single");
            if (jpeg) collector.failNextJpeg(); else collector.failNextPreview();
            completed(collector,request);
        }
    }
}
"""
OPEN_PREFIX = r"""
public class LegacyOpenExpectationFixture {
    static final String TAG="camera";
    static final int API_VERSION_2=2;
    static boolean legacy,failOpen;
    static int releases,opens;
    static final RuntimeException expectedFailure=new RuntimeException("injected open failure");
    static class Log { static void i(String tag,String message) {} static void v(String tag,String message) {} }
    static class CameraCharacteristics {
        static final int INFO_SUPPORTED_HARDWARE_LEVEL=1, INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY=2;
        Integer get(int key) { return legacy ? 2 : 3; }
    }
    static class CameraManager { CameraCharacteristics getCameraCharacteristics(String id) { return new CameraCharacteristics(); } }
    static class Context {
        <T> T getSystemService(Class<T> type) { return type.cast(new CameraManager()); }
        String getPackageName() { return "test"; }
    }
    Context getContext() { return new Context(); }
    static void assertNotNull(String message,Object value) { if (value==null) throw new AssertionError(message); }
    static void assertNotNull(Object value) { assertNotNull("null camera",value); }
    static class Camera {
        static final int CAMERA_HAL_API_VERSION_1_0=256;
        static int getNumberOfCameras() { return 1; }
        static Camera openLegacy(int id,int version) { opens++; if(failOpen) throw expectedFailure; return new Camera(); }
        void release() { releases++; }
    }
    Camera mCamera;
    interface ICameraClient {}
    static class DummyCameraClient implements ICameraClient {}
    static class ICamera { void disconnect() { releases++; } }
    static class ICameraService {
        static final int USE_CALLING_UID=-1, USE_CALLING_PID=-1;
        boolean supportsCameraApi(String id,int api) { return !legacy; }
        ICamera connectLegacy(ICameraClient callbacks,int id,int version,String name,int uid,int pid) {
            opens++; if (failOpen) throw expectedFailure; return new ICamera();
        }
    }
    static class Utils {
        int getGuessedNumCameras() { return 1; }
        ICameraService getCameraService() { return new ICameraService(); }
    }
    final Utils mUtils=new Utils();
"""
OPEN_CASES = r"""
    public static void main(String[] args) throws Exception {
        for (boolean hardwareLegacy : new boolean[]{true,false}) {
            for (boolean fail : new boolean[]{true,false}) {
                legacy=hardwareLegacy; failOpen=fail; opens=releases=0;
                boolean propagated=false;
                try { new LegacyOpenExpectationFixture().ENTRYPOINT(); }
                catch (RuntimeException error) { if(error!=expectedFailure) throw error; propagated=true; }
                if (propagated!=(legacy && fail)) throw new AssertionError("legacy open failure suppression");
                if (opens!=1 || releases!=(fail ? 0 : 1)) throw new AssertionError("open/release lifecycle");
            }
        }
    }
}
"""


class LegacyCameraCompletion(unittest.TestCase):
    def compile_run(self, sources, name, output):
        java = Path(os.environ["JAVA_HOME"]) / "bin"
        subprocess.run(
            [str(java / "javac"), "-Werror", "-d", str(output), *map(str, sources)],
            check=True,
        )
        subprocess.run([str(java / "java"), "-cp", str(output), name], check=True)

    def test_mixed_capture_failure_completes_and_reopens(self):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True) / "collector"
        output.mkdir(exist_ok=True)
        sources = []
        for name, body in STUBS.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            sources.append(path)
        path = output / "android/hardware/camera2/legacy/CaptureCollector.java"
        path.write_bytes(
            (
                ROOT / "core/java/android/hardware/camera2/legacy/CaptureCollector.java"
            ).read_bytes()
        )
        sources.append(path)
        path = output / "android/hardware/camera2/legacy/CaptureCompletionFixture.java"
        path.write_text(COLLECTOR_CASES)
        sources.append(path)
        self.compile_run(
            sources, "android.hardware.camera2.legacy.CaptureCompletionFixture", output
        )

    def check_open_test(self, relative, entrypoint):
        source = (
            ROOT
            / "media/tests/MediaFrameworkTest/src/com/android/mediaframeworktest"
            / relative
        ).read_text()
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True) / entrypoint
        output.mkdir(exist_ok=True)
        path = output / "LegacyOpenExpectationFixture.java"
        path.write_text(
            OPEN_PREFIX
            + function(source, "public void " + entrypoint + "(")
            + OPEN_CASES.replace("ENTRYPOINT", entrypoint)
        )
        self.compile_run([path], "LegacyOpenExpectationFixture", output)

    def test_binder_open_failure_is_required_on_legacy(self):
        self.check_open_test("integration/CameraBinderTest.java", "testConnectLegacy")

    def test_public_open_failure_is_required_on_legacy(self):
        self.check_open_test("unit/CameraOpenTest.java", "testOpenLegacy")


if __name__ == "__main__":
    unittest.main()
