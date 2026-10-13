"""Compile and exercise production AHardwareBuffer staging with a host gralloc fixture.

Set TEST_OUTPUT_DIR to an existing retained build directory. CXX selects the host
compiler; CXXFLAGS may select a 32-bit ABI to exercise its size_t overflow boundary.
"""

import os
from pathlib import Path
import shlex
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "HardwareBitmapUploader.cpp"


def production_function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    offset = opening + 1
    while depth:
        depth += (source[offset] == "{") - (source[offset] == "}")
        offset += 1
    return source[start:offset]


FIXTURE = r"""
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>
#define ALOGW(...) ((void)0)
enum { kGray_8_SkColorType, kRGBA_8888_SkColorType, kRGB_565_SkColorType,
       kRGBA_F16_SkColorType, kUnsupported_SkColorType };
enum { AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM = 1,
       AHARDWAREBUFFER_FORMAT_R5G6B5_UNORM = 4,
       AHARDWAREBUFFER_FORMAT_R16G16B16A16_FLOAT = 22 };
constexpr uint64_t AHARDWAREBUFFER_USAGE_CPU_WRITE_RARELY = 0x20;
constexpr uint64_t AHARDWAREBUFFER_USAGE_CPU_WRITE_NEVER = 0;
constexpr uint64_t AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE = 0x100;
struct AHardwareBuffer_Desc {
    uint32_t width, height, layers, format;
    uint64_t usage;
    uint32_t stride;
};
struct AHardwareBuffer {
    AHardwareBuffer_Desc desc;
    std::vector<uint8_t> pixels;
    int lockStatus = 0, unlockStatus = 0, locks = 0, unlocks = 0;
    bool nullMapping = false;
};
struct SkBitmap {
    int widthValue = 2, heightValue = 2, type = kRGBA_8888_SkColorType;
    size_t stride = 12;
    const void* pixels;
    int width() const { return widthValue; }
    int height() const { return heightValue; }
    int colorType() const { return type; }
    size_t rowBytes() const { return stride; }
    const void* getPixels() const { return pixels; }
};
static int failures;
static void check(bool result, const char* name) {
    if (!result) { fprintf(stderr, "FAIL: %s\n", name); failures++; }
}
static bool cpuProperty;
namespace base {
bool GetBoolProperty(const char* property, bool initial) {
    check(strcmp(property, "ro.hwui.hardware_bitmap_cpu_upload") == 0, "property name");
    check(!initial, "property default");
    return cpuProperty;
}
}
void AHardwareBuffer_describe(AHardwareBuffer* buffer, AHardwareBuffer_Desc* desc) {
    *desc = buffer->desc;
}
int AHardwareBuffer_lock(AHardwareBuffer* buffer, uint64_t usage, int fence,
                         const void* region, void** address) {
    buffer->locks++;
    check(usage == AHARDWAREBUFFER_USAGE_CPU_WRITE_RARELY, "CPU write usage");
    check(fence == -1 && region == nullptr, "full buffer synchronous lock");
    *address = buffer->nullMapping ? nullptr : buffer->pixels.data();
    return buffer->lockStatus;
}
int AHardwareBuffer_unlock(AHardwareBuffer* buffer, int* fence) {
    buffer->unlocks++;
    check(fence == nullptr, "synchronous unlock");
    return buffer->unlockStatus;
}
"""

CASES = r"""
int main(int argc, char**) {
    cpuProperty = argc > 1;
    check(useCpuBitmapUpload() == cpuProperty, "property selection");
    check(hardwareBitmapUsage() == (AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE |
          (cpuProperty ? AHARDWAREBUFFER_USAGE_CPU_WRITE_RARELY : 0)), "allocation usage");
    for (int type : {kRGBA_8888_SkColorType, kRGB_565_SkColorType, kRGBA_F16_SkColorType,
                     kGray_8_SkColorType}) {
        size_t sourcePixel = type == kRGB_565_SkColorType ? 2 :
                type == kRGBA_F16_SkColorType ? 8 : type == kGray_8_SkColorType ? 1 : 4;
        size_t destinationPixel = type == kGray_8_SkColorType ? 4 : sourcePixel;
        uint32_t format = type == kRGB_565_SkColorType ? AHARDWAREBUFFER_FORMAT_R5G6B5_UNORM :
                type == kRGBA_F16_SkColorType ? AHARDWAREBUFFER_FORMAT_R16G16B16A16_FLOAT :
                AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM;
        for (uint32_t height : {1U, 3U}) {
            size_t sourceStride = 2 * sourcePixel + 7;
            std::vector<uint8_t> source(sourceStride * height, 0xA7);
            for (uint32_t row = 0; row < height; row++) {
                for (size_t byte = 0; byte < 2 * sourcePixel; byte++) {
                    source[row * sourceStride + byte] = static_cast<uint8_t>(11 + row + byte);
                }
            }
            SkBitmap bitmap{2, static_cast<int>(height), type, sourceStride, source.data()};
            AHardwareBuffer buffer{{2, height, 1, format, hardwareBitmapUsage(), 5},
                                    std::vector<uint8_t>(5 * destinationPixel * height + 16, 0xCC)};
            check(uploadHardwareBitmapWithCpu(bitmap, &buffer), "padded upload succeeds");
            check(buffer.locks == 1 && buffer.unlocks == 1, "mapping lifecycle");
            for (uint32_t row = 0; row < height; row++) {
                for (size_t byte = 0; byte < 5 * destinationPixel; byte++) {
                    uint8_t expected = 0xCC;
                    if (byte < 2 * destinationPixel) {
                        expected = type == kGray_8_SkColorType ?
                            (byte % 4 == 3 ? 255 : source[row * sourceStride + byte / 4]) :
                            source[row * sourceStride + byte];
                    }
                    check(buffer.pixels[row * 5 * destinationPixel + byte] == expected,
                          "pixel contents and destination padding");
                }
            }
            for (size_t tail = 5 * destinationPixel * height; tail < buffer.pixels.size(); tail++) {
                check(buffer.pixels[tail] == 0xCC, "destination tail guard");
            }
        }
    }
    uint8_t source[32] = {};
    for (int scenario = 0; scenario < 16; scenario++) {
        SkBitmap bitmap{2, 2, kRGBA_8888_SkColorType, 12, source};
        AHardwareBuffer buffer{{2, 2, 1, AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM,
                                hardwareBitmapUsage(), 4}, std::vector<uint8_t>(32, 0xCC)};
        switch (scenario) {
            case 0: bitmap.widthValue = 0; break;
            case 1: bitmap.heightValue = -1; break;
            case 2: bitmap.pixels = nullptr; break;
            case 3: buffer.desc.width = 3; break;
            case 4: buffer.desc.height = 3; break;
            case 5: buffer.desc.layers = 2; break;
            case 6: buffer.desc.stride = 1; break;
            case 7: buffer.desc.format = AHARDWAREBUFFER_FORMAT_R5G6B5_UNORM; break;
            case 8: bitmap.type = kUnsupported_SkColorType; break;
            case 9: bitmap.stride = 7; break;
            case 10: bitmap.stride = std::numeric_limits<size_t>::max(); break;
            case 11: buffer.lockStatus = -22; break;
            case 12: buffer.nullMapping = true; break;
            case 13: buffer.unlockStatus = -5; break;
            case 14:
                bitmap.heightValue = 3; buffer.desc.height = 3;
                bitmap.stride = std::numeric_limits<size_t>::max() / 2;
                break;
            case 15:
                if (sizeof(size_t) > 4) continue;
                buffer.desc.stride = std::numeric_limits<uint32_t>::max();
                break;
        }
        check(!uploadHardwareBitmapWithCpu(bitmap, &buffer), "invalid upload fails");
        check(buffer.locks == (scenario >= 11 && scenario <= 13 ? 1 : 0), "lock admission");
        check(buffer.unlocks == (scenario == 12 || scenario == 13 ? 1 : 0), "error cleanup");
        if (scenario != 13) {
            for (uint8_t byte : buffer.pixels) check(byte == 0xCC, "failed upload preserves pixels");
        }
    }
    return failures ? 1 : 0;
}
"""


class BitmapCpuUploadTest(unittest.TestCase):
    def test_production_staging(self):
        output = Path(os.environ["TEST_OUTPUT_DIR"]).resolve(strict=True)
        source = SOURCE.read_text()
        functions = "\n".join(
            production_function(source, signature)
            for signature in (
                "static bool useCpuBitmapUpload()",
                "static uint64_t hardwareBitmapUsage()",
                "static bool uploadHardwareBitmapWithCpu(",
            )
        )
        fixture = output / "bitmap_cpu_upload_fixture.cpp"
        binary = output / "bitmap_cpu_upload_fixture"
        fixture.write_text(FIXTURE + functions + CASES)
        command = shlex.split(os.environ.get("CXX", "c++")) + shlex.split(
            os.environ.get("CXXFLAGS", "")
        )
        command += [
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=undefined",
            "-fno-sanitize-recover=all",
            str(fixture),
            "-o",
            str(binary),
        ]
        subprocess.run(command, check=True)
        subprocess.run([str(binary)], check=True)
        subprocess.run([str(binary), "cpu"], check=True)

    def test_allocation_and_dispatch_contract(self):
        source = SOURCE.read_text()
        fp16 = production_function(
            source, "bool HardwareBitmapUploader::hasFP16Support()"
        )
        allocation = production_function(
            source, "sk_sp<Bitmap> HardwareBitmapUploader::allocateHardwareBitmap("
        )
        initialize = production_function(
            source, "void HardwareBitmapUploader::initialize()"
        )
        self.assertIn(".usage = hardwareBitmapUsage(),", fp16)
        self.assertIn(".usage = hardwareBitmapUsage(),", allocation)
        self.assertIn("if (useCpuBitmapUpload()) {", initialize)
        self.assertIn(
            "if (useCpuBitmapUpload()) {\n"
            "        if (!uploadHardwareBitmapWithCpu(bitmap, ahb.get())) {\n"
            "            return nullptr;\n"
            "        }\n"
            "    } else {\n"
            "        createUploader(usingGL);",
            allocation,
        )


if __name__ == "__main__":
    unittest.main()
