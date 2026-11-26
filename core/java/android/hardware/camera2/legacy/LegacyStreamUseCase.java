package android.hardware.camera2.legacy;

import android.hardware.camera2.CameraMetadata;

/** Queue policy for the downstream purpose declared in an output configuration. */
final class LegacyStreamUseCase {
    static final long DEFAULT = CameraMetadata.SCALER_AVAILABLE_STREAM_USE_CASES_DEFAULT;
    private static final long PREVIEW = CameraMetadata.SCALER_AVAILABLE_STREAM_USE_CASES_PREVIEW;
    private static final long VIDEO_RECORD =
            CameraMetadata.SCALER_AVAILABLE_STREAM_USE_CASES_VIDEO_RECORD;

    private LegacyStreamUseCase() {}

    static long[] available() {
        return new long[] {DEFAULT, PREVIEW, VIDEO_RECORD};
    }

    static boolean isSupported(long useCase, long[] available) {
        if (useCase == DEFAULT) return true;
        if (available != null) {
            for (long candidate : available) {
                if (candidate == useCase) return true;
            }
        }
        return false;
    }

    static boolean usesFifo(long useCase, int selectedOutput, int outputIndex) {
        if (selectedOutput == -2) return useCase != PREVIEW;
        return selectedOutput >= 0 && selectedOutput == outputIndex;
    }
}
