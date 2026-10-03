package android.hardware.camera2.legacy;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class LegacyStreamUseCaseTest {
    @Test
    public void partialHintsRequireAdvertisement() {
        long[] available = LegacyStreamUseCase.available();
        assertArrayEquals(new long[] {0, 1, 3}, available);
        assertTrue(LegacyStreamUseCase.isSupported(0, null));
        assertTrue(LegacyStreamUseCase.isSupported(1, available));
        assertTrue(LegacyStreamUseCase.isSupported(3, available));
        assertFalse(LegacyStreamUseCase.isSupported(1, null));
        assertFalse(LegacyStreamUseCase.isSupported(2, available));
        assertFalse(LegacyStreamUseCase.isSupported(65536, available));
    }

    @Test
    public void previewReplacementFollowsPurposeRatherThanIndex() {
        assertFalse(LegacyStreamUseCase.usesFifo(1, -2, 0));
        assertFalse(LegacyStreamUseCase.usesFifo(1, -2, 1));
        assertTrue(LegacyStreamUseCase.usesFifo(3, -2, 0));
        assertTrue(LegacyStreamUseCase.usesFifo(3, -2, 1));
    }

    @Test
    public void defaultRetainsQueuePolicy() {
        assertTrue(LegacyStreamUseCase.usesFifo(0, -2, 0));
        assertTrue(LegacyStreamUseCase.usesFifo(0, -2, 1));
    }

    @Test
    public void explicitDiagnosticsRetainSelectedPolicy() {
        assertTrue(LegacyStreamUseCase.usesFifo(1, 1, 1));
        assertFalse(LegacyStreamUseCase.usesFifo(3, 1, 0));
        assertFalse(LegacyStreamUseCase.usesFifo(3, -1, 1));
    }

    @Test
    public void availableHintsReturnIndependentArrays() {
        long[] available = LegacyStreamUseCase.available();
        available[0] = 99;
        assertArrayEquals(new long[] {0, 1, 3}, LegacyStreamUseCase.available());
    }
}
