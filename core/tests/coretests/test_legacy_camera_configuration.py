"""Exercise production EGL admission, constructor policy, GPS, and template blocks."""

import os
from pathlib import Path
import subprocess
import unittest

from test_legacy_camera_boundaries import ROOT, function
from test_legacy_camera_admission import RENDER_PREFIX


EGL_ADDITIONS = r"""
    Object mEGLDisplay,mConfigs;
    static class EGL14 {
        static final int EGL_NONE=0, EGL_SUCCESS=0x3000, EGL_BAD_NATIVE_WINDOW=0x300b, EGL_BAD_SURFACE=0x300d;
        static int error=EGL_SUCCESS;
        static Object eglCreateWindowSurface(Object display,Object config,Surface surface,int[] attributes,int offset) {
            error=surface.mode==4 ? EGL_BAD_NATIVE_WINDOW : surface.mode==5 ? EGL_BAD_SURFACE : surface.mode==6 ? 0x3003 : EGL_SUCCESS;
            return error==EGL_SUCCESS ? new Object() : null;
        }
        static int eglGetError() { int result=error; error=EGL_SUCCESS; return result; }
    }
    private void checkEglError(String message) {
        if(EGL14.eglGetError()!=EGL14.EGL_SUCCESS) throw new IllegalStateException(message);
    }
    static List<Pair<Surface,Size>> outputs(int... modes) {
        List<Pair<Surface,Size>> result=new ArrayList<>();
        for(int mode:modes) result.add(new Pair<>(new Surface(mode),new Size()));
        return result;
    }
    public static void main(String[] args) {
        for(int[] modes:new int[][]{{4},{5},{4,5},{4,0},{0,5},{4,3}}) {
            SurfaceAdmissionFixture renderer=new SurfaceAdmissionFixture();
            renderer.configureSurfaces(outputs(modes));
            int regular=0,conversion=0;
            for(int mode:modes) { if(mode==0) regular++; if(mode==3) conversion++; }
            int admitted=regular+conversion>0 ? 1 : 0;
            if(renderer.mSurfaces.size()!=regular || renderer.mConversionSurfaces.size()!=conversion ||
                renderer.initialized!=admitted || renderer.current!=admitted ||
                (renderer.mSurfaceTexture!=null)!=(admitted==1)) throw new AssertionError(Arrays.toString(modes));
            renderer.configureSurfaces(outputs(0));
            if(renderer.mSurfaceTexture==null) throw new AssertionError("reopen");
        }
        boolean fatal=false;
        try { new SurfaceAdmissionFixture().configureSurfaces(outputs(6)); }
        catch(IllegalStateException expected) { fatal=true; }
        if(!fatal) throw new AssertionError("non-abandonment EGL failure is hidden");
    }
}
"""
APP_OPS_PREFIX = r"""
public class LegacyAppOpsFixture {
    static int initialization,policyCalls;
    static class Camera {
        static final int EACCES=13,ENODEV=19,ENOSYS=38,EOPNOTSUPP=95,EINVAL=22,EBUSY=16,EUSERS=87;
        int cameraInitVersion(int id,int version) { return initialization; }
        boolean checkInitErrors(int value) { return value!=0; }
        void initAppOps() { policyCalls++; }
"""
APP_OPS_CASES = r"""
    }
    public static void main(String[] args) {
        for(int error:new int[]{0,-13,-19,-38,-95,-22,-16,-87,-99}) {
            initialization=error; policyCalls=0;
            boolean failed=false;
            try { new Camera(0,256); } catch(RuntimeException expected) { failed=true; }
            if(failed!=(error!=0) || policyCalls!=(error==0 ? 1 : 0)) throw new AssertionError("AppOps initialization: "+error);
        }
    }
}
"""
GPS_PREFIX = r"""
import java.util.*;
public class GpsRequestFixture {
    static final String TAG="camera"; static final int JPEG_GPS_LOCATION=1;
    static class Log { static void w(String tag,String text) {} }
    static class Location {
        final String provider; final long time;
        Location(String provider,long time) { this.provider=provider; this.time=time; }
        String getProvider() { return provider; } long getTime() { return time; }
        double getAltitude() { return 3; } double getLatitude() { return 1; } double getLongitude() { return 2; }
    }
    static class Request { Location location; Location get(int key) { return location; } }
    static class Parameters {
        Map<String,Object> gps=new HashMap<>();
        void setGpsAltitude(double value) { gps.put("altitude",value); }
        void setGpsLatitude(double value) { gps.put("latitude",value); }
        void setGpsLongitude(double value) { gps.put("longitude",value); }
        void setGpsProcessingMethod(String value) { gps.put("provider",value); }
        void setGpsTimestamp(long value) { gps.put("time",value); }
        void removeGpsData() { gps.clear(); }
    }
"""
GPS_CASES = r"""
    public static void main(String[] args) {
        Parameters parameters=new Parameters(); Request request=new Request();
        for(Location incomplete:new Location[]{null,new Location(null,1000),new Location("gps",0)}) {
            request.location=new Location("gps",9000); apply(request,parameters);
            if(parameters.gps.size()!=5 || !parameters.gps.get("time").equals(9L)) throw new AssertionError("complete GPS");
            request.location=incomplete; apply(request,parameters);
            if(!parameters.gps.isEmpty()) throw new AssertionError("stale GPS survives rejected location");
        }
    }
}
"""
TEMPLATE_PREFIX = r"""
import java.util.*;
public class AntibandingTemplateFixture {
    static final int CONTROL_AE_ANTIBANDING_MODE_AUTO=3, CONTROL_AE_AVAILABLE_ANTIBANDING_MODES=10;
    static class CaptureRequest { static final int CONTROL_AE_ANTIBANDING_MODE=20; }
    static class CameraCharacteristics { int[] modes; int[] get(int key) { return modes; } }
    static class CameraMetadataNative { Integer mode; void set(int key,int value) { mode=value; } }
    static class ArrayUtils { static boolean contains(int[] values,int requested) { for(int value:values) if(value==requested) return true; return false; } }
"""
TEMPLATE_CASES = r"""
    public static void main(String[] args) {
        for(int[] modes:new int[][]{null,{}, {0},{1},{2},{0,2},{0,1,2,3},{3}}) {
            CameraCharacteristics characteristics=new CameraCharacteristics(); characteristics.modes=modes;
            CameraMetadataNative result=new CameraMetadataNative(); apply(characteristics,result);
            if(modes==null || modes.length==0) { if(result.mode!=null) throw new AssertionError("empty capability advertises mode"); }
            else if(result.mode==null || !ArrayUtils.contains(modes,result.mode) ||
                    (ArrayUtils.contains(modes,3) && result.mode!=3)) throw new AssertionError("unsupported template mode");
        }
    }
}
"""


class LegacyCameraConfiguration(unittest.TestCase):
    def run_java(self, name, content):
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
        subprocess.run([str(java / "java"), "-cp", str(output), name], check=True)

    def test_egl_creation_removes_only_abandoned_outputs(self):
        source = (
            ROOT
            / "core/java/android/hardware/camera2/legacy/SurfaceTextureRenderer.java"
        ).read_text()
        prefix = RENDER_PREFIX.replace(
            "return surface.mode>=2;", "return surface.mode==2 || surface.mode==3;"
        )
        prefix = prefix.replace(
            "static class Log {",
            "static class Log { static void e(String tag,String message) {}",
        )
        prefix = prefix.replace(
            "    void configureEGLOutputSurfaces(Collection<EGLSurfaceHolder> surfaces) { regular+=surfaces.size(); }",
            "",
        )
        methods = "\n".join(
            function(source, signature)
            for signature in (
                "public void configureSurfaces(",
                "private void configureEGLOutputSurfaces(",
                "private void checkEglDrawError(",
            )
        )
        self.run_java("SurfaceAdmissionFixture", prefix + methods + EGL_ADDITIONS)

    def test_legacy_constructor_initializes_appops_after_success(self):
        source = (ROOT / "core/java/android/hardware/Camera.java").read_text()
        self.run_java(
            "LegacyAppOpsFixture",
            APP_OPS_PREFIX
            + function(source, "private Camera(int cameraId, int halVersion)")
            + APP_OPS_CASES,
        )

    def test_incomplete_location_clears_previous_gps(self):
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/LegacyRequestMapper.java"
        ).read_text()
        block = function(source, "// jpeg.gpsLocation")
        content = GPS_PREFIX + function(
            source, "private static boolean checkForCompleteGpsData("
        )
        content += (
            "static void apply(Request request,Parameters params) {\n"
            + block
            + "\n}"
            + GPS_CASES
        )
        self.run_java("GpsRequestFixture", content)

    def test_template_antibanding_belongs_to_capabilities(self):
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/LegacyMetadataMapper.java"
        ).read_text()
        start = source.index(
            "// control.aeAntibandingMode",
            source.index("public static CameraMetadataNative createRequestTemplate("),
        )
        end = source.index("// control.aeExposureCompensation", start)
        self.run_java(
            "AntibandingTemplateFixture",
            TEMPLATE_PREFIX
            + "static void apply(CameraCharacteristics c,CameraMetadataNative m) {\n"
            + source[start:end]
            + "\n}"
            + TEMPLATE_CASES,
        )


if __name__ == "__main__":
    unittest.main()
