"""Exercise rejected legacy stream transactions and effect metadata from production Java."""

import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(os.environ.get("FRAMEWORK_SOURCE", Path(__file__).resolve().parents[3]))


def method(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


TRANSACTION_PREFIX = r"""
import java.util.*;
public class StreamTransactionFixture {
    static final String TAG="camera"; static final boolean DEBUG=false;
    static class Log { static void d(String tag,String message) {} static void e(String tag,String message) {} }
    static class ServiceSpecificException extends RuntimeException {
        private static final long serialVersionUID=1L;
        final int errorCode;
        ServiceSpecificException(int code,String message) { super(message); errorCode=code; }
    }
    static class RemoteException extends Exception { private static final long serialVersionUID=1L; }
    static class ICameraService { static final int ERROR_DISCONNECTED=1,ERROR_INVALID_OPERATION=2,ERROR_ILLEGAL_ARGUMENT=3; }
    static class ICameraDeviceUser { static final int NORMAL_MODE=0; }
    static class Surface { final int identity; Surface(int value) { identity=value; } }
    static class SparseArray<T> {
        final TreeMap<Integer,T> values=new TreeMap<>();
        void put(int key,T value) { values.put(key,value); }
        int size() { return values.size(); }
        int indexOfKey(int key) { return new ArrayList<>(values.keySet()).indexOf(key); }
        void removeAt(int index) { values.remove(new ArrayList<>(values.keySet()).get(index)); }
        public SparseArray<T> clone() { SparseArray<T> copy=new SparseArray<>(); copy.values.putAll(values); return copy; }
    }
    static class OutputConfiguration {
        static final int ROTATION_0=0;
        final int rotation; final List<Surface> surfaces;
        OutputConfiguration(int value,Surface... targets) { rotation=value; surfaces=Arrays.asList(targets); }
        int getRotation() { return rotation; }
        List<Surface> getSurfaces() { return surfaces; }
        Surface getSurface() { return surfaces.isEmpty() ? null : surfaces.get(0); }
    }
    static class CameraMetadataNative {}
    static class CaptureRequest {}
    static class SubmitInfo {}
    static class LegacyExceptionUtils {
        static void throwOnServiceError(int code) { if(code!=0) throw new ServiceSpecificException(code,"configuration"); }
    }
    static class LegacyCameraDevice {
        boolean closed; int status,waits,flushes,requests;
        SparseArray<Surface> configured=new SparseArray<>();
        boolean isClosed() { return closed; }
        int configureOutputs(SparseArray<Surface> surfaces) { if(status==0) configured=surfaces.clone(); return status; }
        void waitUntilIdle() { ++waits; }
        long flush() { return ++flushes; }
        SubmitInfo submitRequest(CaptureRequest request,boolean streaming) { ++requests; return new SubmitInfo(); }
    }
    final Object mConfigureLock=new Object();
    final SparseArray<Surface> mSurfaces=new SparseArray<>();
    final LegacyCameraDevice mLegacyDevice=new LegacyCameraDevice();
    boolean mConfiguring;
    int mSurfaceIdCounter;
    static void expect(int code,Runnable call) {
        try { call.run(); } catch(ServiceSpecificException error) {
            if(error.errorCode==code) return;
            throw new AssertionError("wrong error: "+error.errorCode);
        }
        throw new AssertionError("expected rejection "+code);
    }
"""
TRANSACTION_CASES = r"""
    public static void main(String[] arguments) throws Exception {
        Surface original=new Surface(1),accepted=new Surface(2),next=new Surface(3);
        OutputConfiguration[] invalid={new OutputConfiguration(0,original,accepted),
            new OutputConfiguration(1,original),new OutputConfiguration(0),
            new OutputConfiguration(0,(Surface)null),null};
        for(OutputConfiguration rejected:invalid) {
            StreamTransactionFixture fixture=new StreamTransactionFixture();
            fixture.beginConfigure();
            int oldId=fixture.createStream(new OutputConfiguration(0,original));
            fixture.endConfigure(0,new CameraMetadataNative(),0);
            fixture.beginConfigure();
            fixture.deleteStream(oldId);
            int acceptedId=fixture.createStream(new OutputConfiguration(0,accepted));
            expect(ICameraService.ERROR_ILLEGAL_ARGUMENT,()->fixture.createStream(rejected));
            if(fixture.mConfiguring) throw new AssertionError("rejected stream retains configuration lock");
            if(fixture.mSurfaces.size()!=1 || fixture.mSurfaces.values.get(acceptedId)!=accepted ||
                fixture.mLegacyDevice.configured.values.get(oldId)!=original)
                throw new AssertionError("accepted stream IDs diverge from caller or HAL mutates on rejection");
            fixture.flush(); fixture.waitUntilIdle(); fixture.submitRequest(new CaptureRequest(),false);
            if(fixture.mLegacyDevice.flushes!=1 || fixture.mLegacyDevice.waits!=1 || fixture.mLegacyDevice.requests!=1)
                throw new AssertionError("post-failure controls remain blocked");
            expect(ICameraService.ERROR_INVALID_OPERATION,()->fixture.endConfigure(0,null,0));
            fixture.beginConfigure();
            int newId=fixture.createStream(new OutputConfiguration(0,next));
            if(newId<=acceptedId) throw new AssertionError("stream ID is reused");
            fixture.endConfigure(0,null,0);
            if(fixture.mConfiguring || fixture.mLegacyDevice.configured.size()!=2 ||
                fixture.mLegacyDevice.configured.values.get(acceptedId)!=accepted ||
                fixture.mLegacyDevice.configured.values.get(newId)!=next)
                throw new AssertionError("later valid configuration fails");
        }
        StreamTransactionFixture fixture=new StreamTransactionFixture();
        expect(ICameraService.ERROR_INVALID_OPERATION,()->fixture.createStream(new OutputConfiguration(0,original)));
        fixture.beginConfigure();
        expect(ICameraService.ERROR_INVALID_OPERATION,fixture::beginConfigure);
        if(!fixture.mConfiguring) throw new AssertionError("nested begin aborts owner transaction");
        fixture.createStream(new OutputConfiguration(0,original));
        expect(ICameraService.ERROR_ILLEGAL_ARGUMENT,()->fixture.endConfigure(1,null,0));
        fixture.beginConfigure(); fixture.mLegacyDevice.status=3;
        expect(3,()->fixture.endConfigure(0,null,0));
        fixture.mLegacyDevice.status=0; fixture.beginConfigure(); fixture.endConfigure(0,null,0);
    }
}
"""
EFFECT_PREFIX = r"""
import java.util.*;
public class EffectMetadataFixture {
    static final int CONTROL_AVAILABLE_EFFECTS=1,CONTROL_EFFECT_MODE_OFF=0;
    static final String[] sLegacyEffectMode={"none","mono","negative","solarize","sepia","posterize","whiteboard","blackboard","aqua"};
    static final int[] sEffectModes={0,1,2,3,4,5,6,7,8};
    static class Parameters { List<String> effects; List<String> getSupportedColorEffects() { return effects; } }
    static class Metadata { int[] effects; void set(int key,int[] value) { effects=value; } }
    static class Log { static void v(String tag,String message) {} }
    static class ArrayUtils {
        static final String TAG="array"; static final boolean DEBUG=false;
"""
EFFECT_CASES = r"""
    public static void main(String[] arguments) {
        List<List<String>> cases=Arrays.asList(null,Collections.emptyList(),Arrays.asList("vendor"),
            Arrays.asList("none"),Arrays.asList("none","mono"),Arrays.asList("vendor","mono"));
        int[][] expected={{0},{0},{0},{0},{0,1},{1}};
        for(int index=0;index<cases.size();++index) {
            Parameters parameters=new Parameters(); parameters.effects=cases.get(index);
            Metadata metadata=new Metadata(); apply(parameters,metadata);
            if(!Arrays.equals(metadata.effects,expected[index]))
                throw new AssertionError("effect metadata for "+cases.get(index)+": "+Arrays.toString(metadata.effects));
        }
    }
}
"""


class LegacyStreamTransactionTest(unittest.TestCase):
    def run_java(self, name, content):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True) / name
        output.mkdir(exist_ok=True)
        source = output / (name + ".java")
        source.write_text(content)
        java = Path(os.environ["JAVA_HOME"]) / "bin"
        subprocess.run(
            [
                str(java / "javac"),
                "-Xlint:all",
                "-Werror",
                "-d",
                str(output),
                str(source),
            ],
            check=True,
        )
        subprocess.run([str(java / "java"), "-cp", str(output), name], check=True)

    def test_rejected_stream_releases_transaction_and_preserves_accepted_ids(self):
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/CameraDeviceUserShim.java"
        ).read_text()
        methods = "\n".join(
            method(source, signature)
            for signature in (
                "public void beginConfigure()",
                "public int[] endConfigure(",
                "public void deleteStream(",
                "public int createStream(",
                "public long flush()",
                "public void waitUntilIdle()",
                "public SubmitInfo submitRequest(",
            )
        )
        self.run_java(
            "StreamTransactionFixture", TRANSACTION_PREFIX + methods + TRANSACTION_CASES
        )

    def test_effect_metadata_supplies_off_for_absent_or_unmapped_control(self):
        source = (
            ROOT / "core/java/android/hardware/camera2/legacy/LegacyMetadataMapper.java"
        ).read_text()
        start = source.index("        List<String> effectModes =")
        end = source.index(
            "        m.set(CONTROL_AVAILABLE_EFFECTS, supportedEffectModes);", start
        )
        end += len("        m.set(CONTROL_AVAILABLE_EFFECTS, supportedEffectModes);")
        array = (
            ROOT / "core/java/android/hardware/camera2/utils/ArrayUtils.java"
        ).read_text()
        conversions = "\n".join(
            method(array, signature)
            for signature in (
                "public static <T> int getArrayIndex(",
                "public static int[] convertStringListToIntArray(",
                "public static List<Integer> convertStringListToIntList(",
            )
        )
        self.run_java(
            "EffectMetadataFixture",
            EFFECT_PREFIX
            + conversions
            + "}\nstatic void apply(Parameters p,Metadata m) {\n"
            + source[start:end]
            + "\n}\n"
            + EFFECT_CASES,
        )


if __name__ == "__main__":
    unittest.main()
