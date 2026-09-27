/*
 * Copyright (C) 2014 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package android.hardware.camera2.legacy;

import android.hardware.Camera;
import android.util.Size;

import java.util.Arrays;
import java.util.List;

import static com.android.internal.util.Preconditions.checkNotNull;

/** Utility methods for camera API1 parameters. */
@SuppressWarnings("deprecation")
public class ParameterUtils {
    private static final int ZOOM_RATIO_MULTIPLIER = 100;

    /** Convert camera API1 sizes into framework sizes. */
    public static Size[] convertSizeListToArray(List<Camera.Size> sizeList) {
        checkNotNull(sizeList, "sizeList must not be null");

        Size[] sizes = new Size[sizeList.size()];
        int index = 0;
        for (Camera.Size size : sizeList) {
            sizes[index++] = new Size(size.width, size.height);
        }
        return sizes;
    }

    /** Return the largest supported picture size by area. */
    public static Size getLargestSupportedJpegSizeByArea(Camera.Parameters parameters) {
        checkNotNull(parameters, "parameters must not be null");

        Size[] jpegSizes = convertSizeListToArray(parameters.getSupportedPictureSizes());
        return android.hardware.camera2.utils.SizeAreaComparator.findLargestByArea(
                Arrays.asList(jpegSizes));
    }

    /** Return the maximum zoom ratio reported by the API1 camera. */
    public static float getMaxZoomRatio(Camera.Parameters parameters) {
        if (!parameters.isZoomSupported()) {
            return 1.0f;
        }

        List<Integer> zoomRatios = parameters.getZoomRatios();
        int maximumRatio = zoomRatios.get(zoomRatios.size() - 1);
        return maximumRatio * 1.0f / ZOOM_RATIO_MULTIPLIER;
    }

    private ParameterUtils() {
        throw new AssertionError();
    }
}
