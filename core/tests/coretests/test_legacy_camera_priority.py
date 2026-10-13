"""Execute legacy camera initialization and its looper with priority arguments."""

import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(os.environ.get("FRAMEWORK_SOURCE", Path(__file__).resolve().parents[3]))


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, position = 1, opening + 1
    while depth:
        depth += (source[position] == "{") - (source[position] == "}")
        position += 1
    return source[start:position]


PREFIX = r"""
import java.lang.ref.WeakReference;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
public class CameraPriorityFixture {
    static final String TAG="camera";
    static final int CAMERA_HAL_API_VERSION_UNSPECIFIED=-1;
    static class Log {
        static void e(String tag,String message) {}
        static void e(String tag,String message,Throwable error) {}
    }
    static class ServiceSpecificException extends RuntimeException {
        private static final long serialVersionUID=1;
        ServiceSpecificException(int error) {}
    }
    static class ICameraService {
        static final int USE_CALLING_UID=-1, ERROR_INVALID_OPERATION=10;
    }
    static class ActivityThread {
        static String currentOpPackageName() { return "test.package"; }
    }
    static class Looper {
        static void prepare() {}
        static Looper myLooper() { return new Looper(); }
        static Looper getMainLooper() { return null; }
        static void loop() {}
        void quitSafely() {}
    }
    static class ConditionVariable {
        final CountDownLatch latch=new CountDownLatch(1);
        void open() { latch.countDown(); }
        void block() {
            try { latch.await(); } catch(InterruptedException failure) { throw new AssertionError(failure); }
        }
        boolean block(int timeout) {
            try { return latch.await(timeout,TimeUnit.MILLISECONDS); }
            catch(InterruptedException failure) { throw new AssertionError(failure); }
        }
    }
    static class EventHandler { EventHandler(Camera camera,Looper looper) {} }
    static class Camera {
        Object mShutterCallback,mRawImageCallback,mJpegCallback,mPreviewCallback,mPostviewCallback,mZoomListener;
        boolean mUsingPreviewAllocation;
        EventHandler mEventHandler;
        int receivedId,receivedUid,receivedOffset;
        static Camera openUninitialized() { return new Camera(); }
        void release() {}
        int native_setupForUid(Object reference,int id,int hal,String packageName,int uid,int offset) {
            receivedId=id; receivedUid=uid; receivedOffset=offset; return 0;
        }
"""
CASES = r"""
    public static void main(String[] args) {
        Camera direct=new Camera();
        direct.cameraInitUnspecified(3);
        if (direct.receivedUid!=-1 || direct.receivedOffset!=0) throw new AssertionError("default priority");
        direct.cameraInitUnspecified(3,700);
        if (direct.receivedUid!=700 || direct.receivedOffset!=0) throw new AssertionError("delegated default");
        for (int offset : new int[]{0,500,-1,Integer.MAX_VALUE}) {
            CameraLooper opener=new CameraLooper(3,700,offset);
            if (opener.waitForOpen(5000)!=0) throw new AssertionError("initialization error");
            Camera camera=opener.getCamera();
            if (camera.receivedId!=3 || camera.receivedUid!=700 || camera.receivedOffset!=offset)
                throw new AssertionError("priority forwarding");
            opener.close();
        }
    }
}
"""


class LegacyPriorityTest(unittest.TestCase):
    def test_camera_looper_forwards_uid_and_priority(self):
        camera = (ROOT / "core/java/android/hardware/Camera.java").read_text()
        methods = "\n".join(
            function(camera, signature)
            for signature in (
                "private int cameraInitVersion(int cameraId, int halVersion)",
                "private int cameraInitVersion(int cameraId, int halVersion, int clientUid)",
                "private int cameraInitVersion(int cameraId, int halVersion, int clientUid, int oomScoreOffset)",
                "public int cameraInitUnspecified(int cameraId)",
                "public int cameraInitUnspecified(int cameraId, int clientUid)",
                "public int cameraInitUnspecified(int cameraId, int clientUid, int oomScoreOffset)",
            )
        )
        shim = (
            ROOT / "core/java/android/hardware/camera2/legacy/CameraDeviceUserShim.java"
        ).read_text()
        looper = function(shim, "private static class CameraLooper")
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        fixture = output / "CameraPriorityFixture.java"
        fixture.write_text(PREFIX + methods + "\n}\n" + looper + CASES)
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
            [str(java / "java"), "-cp", str(output), "CameraPriorityFixture"],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
