"""Exercise legacy JPEG admission and fatal-error idle release."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function


OUTPUT_PREFIX = r"""
import java.util.*;
public class LegacyOutputFixture {
    static final String TAG="camera";
    static final int BAD_VALUE=-22;
    static class LegacyExceptionUtils { static final int NO_ERROR=0, INVALID_OPERATION=-38; }
    static class LegacyMetadataMapper { static final int HAL_PIXEL_FORMAT_BLOB=33; }
    static class ImageFormat { static final int PRIVATE=34, YUV_420_888=35, JPEG=256; }
    static class Size {
        final int width,height;
        Size(int width,int height) { this.width=width; this.height=height; }
        int getWidth() { return width; } int getHeight() { return height; }
        @Override public boolean equals(Object other) {
            return other instanceof Size && width==((Size)other).width && height==((Size)other).height;
        }
        @Override public int hashCode() { return Objects.hash(width,height); }
    }
    static class Surface {
        final int format,dataspace; final Size size;
        int writes; boolean abandoned;
        Surface(int format,int dataspace,int width) { this.format=format; this.dataspace=dataspace; size=new Size(width,480); }
        boolean isValid() { return true; }
    }
    static class Pair<First,Second> { final First first; final Second second;
        Pair(First first,Second second) { this.first=first; this.second=second; } }
    static class SparseArray<Value> {
        final List<Value> values=new ArrayList<>();
        int size() { return values.size(); } Value valueAt(int index) { return values.get(index); }
    }
    static class BufferQueueAbandonedException extends Exception { private static final long serialVersionUID=1; }
    static class StreamConfigurationMap {
        static final int HAL_DATASPACE_V0_JFIF=0x08c20000;
        Size[] getOutputSizes(int format) {
            return format==ImageFormat.JPEG || format==ImageFormat.YUV_420_888 ?
                new Size[]{new Size(640,480),new Size(800,480)} : null;
        }
    }
    static class CameraCharacteristics {
        static final int SCALER_STREAM_CONFIGURATION_MAP=0;
        StreamConfigurationMap get(int key) { return new StreamConfigurationMap(); }
    }
    static class ArrayUtils { static boolean contains(Size[] values,Size size) {
        return values!=null && Arrays.asList(values).contains(size); } }
    static class Log {
        static void e(String tag,String text) {} static void e(String tag,String text,Exception error) {}
    }
    static class State { int configurations,idleTransitions;
        boolean setConfiguring() { configurations++; return true; } boolean setIdle() { idleTransitions++; return true; } }
    static class Manager { int configurations; boolean accepted=true;
        boolean configure(List<Pair<Surface,Size>> surfaces) { configurations++; return accepted; } }
    CameraCharacteristics mStaticCharacteristics=new CameraCharacteristics();
    State mDeviceState=new State(); Manager mRequestThreadManager=new Manager();
    SparseArray<Surface> mConfiguredSurfaces;
    static Size getSurfaceSize(Surface surface) { return surface.size; }
    static int detectSurfaceType(Surface surface) { return surface.format; }
    static int detectSurfaceDataspace(Surface surface) throws BufferQueueAbandonedException {
        if(surface.abandoned) throw new BufferQueueAbandonedException(); return surface.dataspace;
    }
    static boolean isFlexibleConsumer(Surface surface) { return false; }
    static Size findClosestSize(Size requested,Size[] available) { return null; }
    static void setSurfaceDimens(Surface surface,int width,int height) throws BufferQueueAbandonedException { surface.writes++; }
    static Surface jpeg(int width) { return new Surface(33,StreamConfigurationMap.HAL_DATASPACE_V0_JFIF,width); }
    static SparseArray<Surface> outputs(Surface... surfaces) {
        SparseArray<Surface> outputs=new SparseArray<>(); outputs.values.addAll(Arrays.asList(surfaces)); return outputs;
    }
"""
OUTPUT_CASES = r"""
    static void verify(SparseArray<Surface> outputs,boolean validation,boolean expected) {
        LegacyOutputFixture device=new LegacyOutputFixture();
        int result=device.configureOutputs(outputs,validation);
        if((result==0)!=expected) throw new AssertionError("admission "+result+" expected "+expected);
        int configurations=expected && !validation ? 1 : 0;
        if(device.mDeviceState.configurations!=configurations || device.mRequestThreadManager.configurations!=configurations)
            throw new AssertionError("invalid configuration reaches device");
        if(validation) for(Surface surface:outputs.values) if(surface.writes!=0) throw new AssertionError("validation mutates surface");
    }
    public static void main(String[] args) {
        if(args[0].equals("manager")) {
            LegacyOutputFixture device=new LegacyOutputFixture();
            device.mConfiguredSurfaces=outputs(jpeg(640));
            SparseArray<Surface> candidate=outputs(new Surface(35,0,640));
            device.mRequestThreadManager.accepted=false;
            if(device.configureOutputs(candidate,false)!=BAD_VALUE || device.mConfiguredSurfaces!=null ||
                device.mDeviceState.idleTransitions!=1) throw new AssertionError("manager failure retains configuration");
            device.mRequestThreadManager.accepted=true;
            if(device.configureOutputs(candidate,false)!=0 || device.mConfiguredSurfaces!=candidate ||
                device.mDeviceState.idleTransitions!=2) throw new AssertionError("valid configuration retry");
            return;
        }
        for(boolean validation:new boolean[]{true,false}) {
            if(args[0].equals("dataspace")) {
                verify(outputs(jpeg(640)),validation,true);
                for(int dataspace:new int[]{0,0x1000,0x1002,0x1003,0xdead})
                    verify(outputs(new Surface(33,dataspace,640)),validation,false);
                Surface abandoned=jpeg(640); abandoned.abandoned=true;
                verify(outputs(abandoned),validation,false);
                verify(outputs(new Surface(35,0,640),jpeg(640)),validation,true);
            } else {
                verify(outputs(jpeg(640)),validation,true);
                verify(outputs(jpeg(640),jpeg(640)),validation,false);
                verify(outputs(jpeg(640),jpeg(800)),validation,false);
                verify(outputs(new Surface(35,0,640),jpeg(640),jpeg(800)),validation,false);
                verify(outputs(new Surface(35,0,640),jpeg(800)),validation,true);
                verify(outputs(new Surface(35,0,640)),validation,true);
                verify(outputs(),validation,true);
            }
        }
    }
}
"""
IDLE_PREFIX = r"""
import java.util.*;
import java.util.concurrent.*;
public class LegacyIdleFixture {
    static final boolean DEBUG=false; static final String TAG="camera";
    static class Log { static void d(String tag,String text) {} }
    static class CameraDeviceImpl { static class CameraDeviceCallbacks {
        static final int ERROR_CAMERA_DISCONNECTED=0, ERROR_CAMERA_DEVICE=1, ERROR_CAMERA_SERVICE=2,
            ERROR_CAMERA_REQUEST=3, ERROR_CAMERA_RESULT=4, ERROR_CAMERA_BUFFER=5, ERROR_CAMERA_DISABLED=6;
    } }
    static class RequestHolder { int getRequestId() { return 7; } }
    static class CaptureResultExtras {}
    static class RemoteException extends Exception { private static final long serialVersionUID=1; }
    static class Callbacks {
        int received=-1;
        void onDeviceError(int error,CaptureResultExtras extras) throws RemoteException { received=error; }
    }
    static class Handler { List<Runnable> pending=new ArrayList<>();
        void post(Runnable task) { pending.add(task); } }
    static class Condition {
        boolean opened;
        synchronized void open() { opened=true; notifyAll(); }
        synchronized void block(CountDownLatch entered) throws InterruptedException {
            entered.countDown(); while(!opened) wait();
        }
    }
    Condition mIdle=new Condition(); Handler mResultHandler=new Handler(); Callbacks mDeviceCallbacks=new Callbacks();
    CaptureResultExtras getExtrasFromRequest(RequestHolder holder,int error,Object argument) { return new CaptureResultExtras(); }
"""
IDLE_CASES = r"""
    public static void main(String[] args) throws Exception {
        for(int error:new int[]{0,1,2,3,4,5,6}) {
            LegacyIdleFixture device=new LegacyIdleFixture();
            CountDownLatch entered=new CountDownLatch(1),released=new CountDownLatch(1);
            Thread waiter=new Thread(() -> {
                try { device.mIdle.block(entered); released.countDown(); }
                catch(InterruptedException failure) { throw new AssertionError(failure); }
            });
            waiter.setDaemon(true); waiter.start();
            if(!entered.await(2,TimeUnit.SECONDS)) throw new AssertionError("waiter start");
            device.onError(error,null,new RequestHolder());
            boolean fatal=error==0 || error==1 || error==2 || error==6;
            if(device.mIdle.opened!=fatal) throw new AssertionError("idle classification "+error);
            if(fatal && !released.await(2,TimeUnit.SECONDS)) throw new AssertionError("fatal idle wait "+error);
            if(device.mDeviceCallbacks.received!=-1 || device.mResultHandler.pending.size()!=1)
                throw new AssertionError("callback ordering");
            device.mResultHandler.pending.get(0).run();
            if(device.mDeviceCallbacks.received!=error) throw new AssertionError("callback error value");
            device.mIdle.open(); waiter.join(2000);
            if(waiter.isAlive()) throw new AssertionError("waiter cleanup");
        }
    }
}
"""


class LegacyOutputContract(unittest.TestCase):
    def run_java(self, name, content, *arguments):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True) / name
        output.mkdir(exist_ok=True)
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
        subprocess.run(
            [str(java / "java"), "-cp", str(output), name, *arguments], check=True
        )

    def source(self):
        return (
            ROOT / "core/java/android/hardware/camera2/legacy/LegacyCameraDevice.java"
        ).read_text()

    def output_case(self, scenario):
        method = function(
            self.source(),
            "public int configureOutputs(SparseArray<Surface> outputs, boolean validateSurfacesOnly)",
        )
        self.run_java(
            "LegacyOutputFixture", OUTPUT_PREFIX + method + OUTPUT_CASES, scenario
        )

    def test_blob_dataspace_matches_jpeg_encoding(self):
        self.output_case("dataspace")

    def test_configuration_accepts_at_most_one_jpeg(self):
        self.output_case("count")

    def test_failed_manager_clears_configuration_and_allows_retry(self):
        self.output_case("manager")

    def test_disabled_camera_releases_idle_before_callback(self):
        method = function(self.source(), "public void onError(final int errorCode,")
        self.run_java("LegacyIdleFixture", IDLE_PREFIX + method + IDLE_CASES)


if __name__ == "__main__":
    unittest.main()
