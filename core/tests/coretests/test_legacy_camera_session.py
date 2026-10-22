"""Exercise production flush admission and exact EGL output configuration."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function
from test_legacy_camera_completion import STUBS
from test_legacy_camera_admission import RENDER_PREFIX


QUEUE_STUBS = {
    "android/hardware/camera2/CaptureRequest.java": "package android.hardware.camera2; public class CaptureRequest {}",
    "android/hardware/camera2/utils/SubmitInfo.java": """package android.hardware.camera2.utils;
        public class SubmitInfo { public final int id; public final long frame;
        public SubmitInfo(int id,long frame) { this.id=id; this.frame=frame; } }""",
    "android/hardware/camera2/legacy/BurstHolder.java": """package android.hardware.camera2.legacy;
        import java.util.*; import android.hardware.camera2.CaptureRequest;
        public class BurstHolder {
            final int id,count; final boolean repeating;
            BurstHolder(int id,boolean repeating,CaptureRequest[] requests,List<Long> jpegIds) {
                this.id=id; this.repeating=repeating; count=requests.length;
            }
            public int getNumberOfRequests() { return count; }
            public int getRequestId() { return id; }
            public boolean isRepeating() { return repeating; }
            public List<RequestHolder> produceRequestHolders(long first) {
                List<RequestHolder> holders=new ArrayList<>();
                for(int index=0;index<count;index++) holders.add(new RequestHolder(first+index,true,false));
                return holders;
            }
        }""",
}
FLUSH_PREFIX = r"""
package android.hardware.camera2.legacy;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.impl.CameraDeviceImpl;
import android.util.*;
public class FlushAdmissionFixture {
    static final String TAG="camera";
    final Object mIdleLock=new Object();
    final RequestQueue mRequestQueue=new RequestQueue(new ArrayList<>());
    final CameraDeviceState mDeviceState=new CameraDeviceState();
    final CaptureCollector mCaptureCollector=new CaptureCollector(2,mDeviceState);
    static CaptureRequest[] burst(int size) {
        CaptureRequest[] requests=new CaptureRequest[size];
        Arrays.setAll(requests,index->new CaptureRequest()); return requests;
    }
    static void require(boolean condition,String message) { if(!condition) throw new AssertionError(message); }
    boolean admit(RequestQueue.RequestQueueEntry entry,RequestHolder holder) throws Exception {
        boolean admitted=mCaptureCollector.queueRequest(holder,new LegacyRequest(),2,TimeUnit.SECONDS,entry::isCurrent);
        if(!admitted) { holder.failRequest(); mDeviceState.setCaptureStart(holder,0,3); }
        return admitted;
    }
"""
FLUSH_CASES = r"""
    public static void main(String[] arguments) throws Exception {
        for(boolean repeating:new boolean[]{false,true}) {
            FlushAdmissionFixture fixture=new FlushAdmissionFixture();
            fixture.mRequestQueue.submit(burst(3),repeating);
            RequestQueue.RequestQueueEntry active=fixture.mRequestQueue.getNext();
            List<RequestHolder> holders=active.getBurstHolder().produceRequestHolders(active.getFrameNumber());
            require(fixture.admit(active,holders.get(0)),"first holder admission");
            fixture.mRequestQueue.submit(burst(2),false);
            require(fixture.flush()==(repeating ? 2 : -1),"repeating last frame");
            require(fixture.mDeviceState.requestErrors==3,"active plus queued failure count");
            require(!fixture.admit(active,holders.get(1)) && !fixture.admit(active,holders.get(2)),"unstarted burst survives flush");
            require(fixture.mDeviceState.requestErrors==5,"one error per frame");
            require(fixture.mCaptureCollector.waitForEmpty(0,TimeUnit.SECONDS),"collector retains flushed frame");
            fixture.mRequestQueue.submit(burst(1),false);
            RequestQueue.RequestQueueEntry next=fixture.mRequestQueue.getNext();
            require(next.getFrameNumber()==5 && next.isCurrent(),"new submission generation/frame");
            require(fixture.admit(next,next.getBurstHolder().produceRequestHolders(5).get(0)),"new submission gets canceled");
            fixture.mCaptureCollector.previewCaptured(10); fixture.mCaptureCollector.previewProduced();
        }
        // Cancellation wakes an admission waiting for capacity, including mixed JPEG/preview.
        for(boolean jpeg:new boolean[]{false,true}) {
            FlushAdmissionFixture fixture=new FlushAdmissionFixture();
            fixture.mRequestQueue.submit(burst(3),false);
            RequestQueue.RequestQueueEntry active=fixture.mRequestQueue.getNext();
            fixture.admit(active,new RequestHolder(0,true,false));
            fixture.admit(active,new RequestHolder(1,true,false));
            CountDownLatch capacityCheck=new CountDownLatch(1);
            AtomicBoolean accepted=new AtomicBoolean(true);
            AtomicReference<Throwable> failure=new AtomicReference<>();
            Thread waiter=new Thread(()->{
                try { accepted.set(fixture.mCaptureCollector.queueRequest(new RequestHolder(2,true,jpeg),
                        new LegacyRequest(),2,TimeUnit.SECONDS,()->{capacityCheck.countDown(); return active.isCurrent();})); }
                catch(Throwable error) { failure.set(error); }
            });
            waiter.start(); require(capacityCheck.await(2,TimeUnit.SECONDS),"capacity wait starts");
            fixture.flush(); waiter.join(3000);
            require(!waiter.isAlive() && failure.get()==null && !accepted.get(),"waiting admission crosses flush");
            require(fixture.mCaptureCollector.waitForEmpty(0,TimeUnit.SECONDS),"partial mixed admission leaks");
        }
        // Flush invalidates a burst while admission owns the collector lock.
        FlushAdmissionFixture fixture=new FlushAdmissionFixture();
        fixture.mRequestQueue.submit(burst(1),false);
        RequestQueue.RequestQueueEntry active=fixture.mRequestQueue.getNext();
        CountDownLatch checked=new CountDownLatch(1),continueAdmission=new CountDownLatch(1);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        Thread admission=new Thread(()->{
            try { fixture.mCaptureCollector.queueRequest(new RequestHolder(0,true,false),new LegacyRequest(),
                    2,TimeUnit.SECONDS,()->{
                        boolean current=active.isCurrent(); checked.countDown();
                        try { if(!continueAdmission.await(2,TimeUnit.SECONDS)) throw new AssertionError("release timeout"); }
                        catch(InterruptedException error) { throw new AssertionError(error); }
                        return current;
                    }); }
            catch(Throwable error) { failure.set(error); }
        });
        admission.start(); require(checked.await(2,TimeUnit.SECONDS),"locked admission starts");
        Thread flusher=new Thread(fixture::flush); flusher.start();
        long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(2);
        while(active.isCurrent() && System.nanoTime()<deadline) Thread.yield();
        require(!active.isCurrent(),"flush invalidates before collector lock");
        continueAdmission.countDown(); admission.join(3000); flusher.join(3000);
        require(!admission.isAlive() && !flusher.isAlive() && failure.get()==null,"flush/admission deadlock");
        require(fixture.mCaptureCollector.waitForEmpty(0,TimeUnit.SECONDS) && fixture.mDeviceState.requestErrors==1,
                "admitted old generation evades collector failure");
    }
}
"""
RENDER_EXTRA = r"""
    Object mEGLDisplay,mConfigs;
    static class EGL14 {
        static final Object EGL_NO_SURFACE=new Object();
        static final int EGL_NONE=0;
        static int error;
        static int destroyed;
        static Object eglCreateWindowSurface(Object display,Object config,Surface surface,int[] attributes,int offset) {
            error=surface.mode==4 ? 1 : 0; return error==0 ? surface : EGL_NO_SURFACE;
        }
        static void eglDestroySurface(Object display,Object surface) { destroyed++; }
    }
    void checkEglDrawError(String operation) throws LegacyExceptionUtils.BufferQueueAbandonedException {
        if(EGL14.error!=0) throw new LegacyExceptionUtils.BufferQueueAbandonedException();
    }
    void makeCurrent(Object surface) throws LegacyExceptionUtils.BufferQueueAbandonedException {
        current++; if(((Surface)surface).mode==7) throw new LegacyExceptionUtils.BufferQueueAbandonedException();
    }
    static List<Pair<Surface,Size>> outputs(int... modes) {
        List<Pair<Surface,Size>> result=new ArrayList<>();
        for(int mode:modes) result.add(new Pair<>(new Surface(mode),new Size())); return result;
    }
    public static void main(String[] args) {
        for(int[] modes:new int[][]{{4},{4,0},{0,4},{7},{7,7},{7,0},{7,3},{0},{3},{0,3}}) {
            SurfaceAdmissionFixture renderer=new SurfaceAdmissionFixture();
            EGL14.destroyed=0;
            boolean expected=Arrays.stream(modes).allMatch(mode->mode==0 || mode==3);
            boolean accepted=renderer.configureSurfaces(outputs(modes));
            if(accepted!=expected || renderer.initialized!=(expected ? 1 : 0) ||
                (renderer.mSurfaceTexture!=null)!=expected) throw new AssertionError("admission "+Arrays.toString(modes));
            if(!expected && (!renderer.mSurfaces.isEmpty() || !renderer.mConversionSurfaces.isEmpty()))
                throw new AssertionError("partial configuration remains published");
            int retries=0;
            for(int mode:modes) { if(mode==4) continue; retries++; if(mode!=7) break; }
            if(renderer.current!=retries) throw new AssertionError("makeCurrent retry "+Arrays.toString(modes));
            if(EGL14.destroyed!=(int)Arrays.stream(modes).filter(mode->mode==7).count())
                throw new AssertionError("abandoned EGL resource leaks");
            if(!renderer.configureSurfaces(outputs(0)) || renderer.mSurfaceTexture==null)
                throw new AssertionError("valid configuration retry");
        }
    }
}
"""


class LegacyCameraSession(unittest.TestCase):
    def run_java(self, name, sources):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True) / name
        output.mkdir(exist_ok=True)
        paths = []
        for relative, content in sources.items():
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            paths.append(path)
        java = Path(os.environ["JAVA_HOME"]) / "bin"
        subprocess.run([str(java / "javac"), "-Xlint:all", "-Werror", "-d", str(output), *map(str, paths)], check=True)
        subprocess.run([str(java / "java"), "-cp", str(output), name], check=True)

    def test_flush_cancels_materialized_and_waiting_burst_requests(self):
        sources = dict(STUBS)
        sources.update(QUEUE_STUBS)
        sources["android/util/Log.java"] = sources["android/util/Log.java"].replace(
            "public static void d(", "public static void i(String tag,String message) {} public static void d(")
        directory = ROOT / "core/java/android/hardware/camera2/legacy"
        for name in ("RequestQueue", "CaptureCollector"):
            sources[f"android/hardware/camera2/legacy/{name}.java"] = (directory / (name + ".java")).read_text()
        manager = (directory / "RequestThreadManager.java").read_text()
        sources["android/hardware/camera2/legacy/FlushAdmissionFixture.java"] = (
            FLUSH_PREFIX + function(manager, "public long flush()") + FLUSH_CASES)
        self.assertIn("activeBurst::isCurrent", manager)
        self.assertIn("if (!activeBurst.isCurrent())", manager)
        self.run_java("android.hardware.camera2.legacy.FlushAdmissionFixture", sources)

    def test_egl_rejection_and_make_current_retry_preserve_exact_output_contract(self):
        source = (ROOT / "core/java/android/hardware/camera2/legacy/SurfaceTextureRenderer.java").read_text()
        prefix = RENDER_PREFIX.replace("return surface.mode>=2;", "return surface.mode==2 || surface.mode==3;")
        for line in (
            "    void configureEGLOutputSurfaces(Collection<EGLSurfaceHolder> surfaces) { regular+=surfaces.size(); }\n",
            "    void destroyOutputSurface(EGLSurfaceHolder holder) {}\n",
            "    void makeCurrent(Object surface) throws LegacyExceptionUtils.BufferQueueAbandonedException { current++; }\n",
        ):
            prefix = prefix.replace(line, "")
        prefix = prefix.replace("converted+=surfaces.size();", "converted+=surfaces.size(); for(EGLSurfaceHolder holder:surfaces) holder.eglSurface=holder.surface;")
        methods = "\n".join(function(source, signature) for signature in (
            "public boolean configureSurfaces(", "private boolean makeCurrentOutput(",
            "private void destroyOutputSurface(", "private void configureEGLOutputSurfaces("))
        self.run_java("SurfaceAdmissionFixture", {"SurfaceAdmissionFixture.java": prefix + methods + RENDER_EXTRA})


if __name__ == "__main__":
    unittest.main()
