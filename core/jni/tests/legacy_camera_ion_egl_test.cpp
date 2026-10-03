#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <sys/stat.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#include "LegacyCameraIonBuffer.h"

using namespace android;

static void require(bool passed, const char* gate) {
    if (!passed) {
        std::fprintf(stderr, "FAIL: %s EGL=0x%x GL=0x%x\n", gate, eglGetError(), glGetError());
        std::exit(1);
    }
    std::printf("PASS: %s\n", gate);
}

static GLuint shader(GLenum type, const char* source) {
    GLuint result = glCreateShader(type);
    glShaderSource(result, 1, &source, nullptr);
    glCompileShader(result);
    GLint status = GL_FALSE;
    glGetShaderiv(result, GL_COMPILE_STATUS, &status);
    require(status == GL_TRUE, "shader compile");
    return result;
}

static int channel(double value) {
    return std::clamp(static_cast<int>(std::lround(value)), 0, 255);
}

static void completeSampling(EGLDisplay display, PFNEGLCREATESYNCKHRPROC createSync,
                             PFNEGLCLIENTWAITSYNCKHRPROC waitSync,
                             PFNEGLDESTROYSYNCKHRPROC destroySync) {
    EGLSyncKHR completion = createSync(display, EGL_SYNC_FENCE_KHR, nullptr);
    require(completion != EGL_NO_SYNC_KHR, "KHR completion creation");
    glFlush();
    bool completed = false;
    EGLint waitError = EGL_SUCCESS;
    std::thread releaseThread([&] {
        completed = waitSync(display, completion, 0, 100000000) == EGL_CONDITION_SATISFIED_KHR;
        waitError = eglGetError();
        completed = destroySync(display, completion) == EGL_TRUE && completed;
        eglReleaseThread();
    });
    releaseThread.join();
    std::printf("cross-thread completion EGL error=0x%x\n", waitError);
    require(completed && waitError == EGL_SUCCESS, "KHR completion wait/destroy on release thread");
}

static void checkImageRing(LegacyCameraIonBuffer& importer, EGLDisplay display, EGLConfig config,
                           EGLContext context, EGLSurface firstSurface, GLuint texture,
                           uint32_t width, uint32_t height, PFNEGLCREATEIMAGEKHRPROC createImage,
                           PFNEGLDESTROYIMAGEKHRPROC destroyImage,
                           PFNGLEGLIMAGETARGETTEXTURE2DOESPROC bindImage,
                           PFNEGLCREATESYNCKHRPROC createSync, PFNEGLCLIENTWAITSYNCKHRPROC waitSync,
                           PFNEGLDESTROYSYNCKHRPROC destroySync) {
    constexpr size_t slotCount = 16;
    const uint32_t magnifiedWidth = width + 128;
    const uint32_t magnifiedHeight = height + 64;
    const EGLint surfaceAttributes[] = {EGL_WIDTH, static_cast<EGLint>(magnifiedWidth), EGL_HEIGHT,
                                        static_cast<EGLint>(magnifiedHeight), EGL_NONE};
    EGLSurface secondSurface = eglCreatePbufferSurface(display, config, surfaceAttributes);
    require(secondSurface != EGL_NO_SURFACE, "ring second render target");
    std::vector<sp<GraphicBuffer>> sources;
    std::vector<sp<GraphicBuffer>> imports;
    std::vector<EGLImageKHR> images;
    const EGLint imageAttributes[] = {EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE};
    for (size_t slot = 0; slot < slotCount; ++slot) {
        sp<GraphicBuffer> source =
                new GraphicBuffer(width, height, LegacyCameraIonBuffer::kVenusFormat, 1,
                                  LegacyCameraIonBuffer::kUsage, "ION image ring source");
        require(source->initCheck() == NO_ERROR, "ring source allocation");
        sp<GraphicBuffer> imported =
                importer.import(source->handle->data[0], importer.allocationSize(), 0);
        require(imported != nullptr, "ring source import");
        EGLImageKHR image = createImage(display, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID,
                                        imported->getNativeBuffer(), imageAttributes);
        require(image != EGL_NO_IMAGE_KHR, "ring EGLImage creation");
        sources.push_back(source);
        imports.push_back(imported);
        images.push_back(image);
    }
    for (int pass = 0; pass < 2; ++pass) {
        for (size_t slot = 0; slot < slotCount; ++slot) {
            android_ycbcr planes{};
            require(sources[slot]->lockYCbCr(GRALLOC_USAGE_SW_WRITE_OFTEN, &planes) == NO_ERROR,
                    "ring source lock");
            for (uint32_t row = 0; row < height; ++row) {
                auto* output = static_cast<unsigned char*>(planes.y) + row * planes.ystride;
                for (uint32_t column = 0; column < width; ++column) {
                    const uint32_t region =
                            (row >= height / 2 ? 2 : 0) + (column >= width / 2 ? 1 : 0);
                    output[column] = 32 + slot * 7 + ((region + pass) % 4) * 8;
                }
            }
            const int cb = 92 + slot * 3;
            const int cr = 164 - slot * 2;
            for (uint32_t row = 0; row < height / 2; ++row) {
                for (uint32_t column = 0; column < width / 2; ++column) {
                    static_cast<unsigned char*>(
                            planes.cb)[row * planes.cstride + column * planes.chroma_step] = cb;
                    static_cast<unsigned char*>(
                            planes.cr)[row * planes.cstride + column * planes.chroma_step] = cr;
                }
            }
            require(sources[slot]->unlock() == NO_ERROR, "ring source cache clean");
        }
        for (size_t sequence = 0; sequence < slotCount; ++sequence) {
            // Coprime step and pool size visit every slot before repeating.
            const size_t slot = (sequence * 5 + pass) % slotCount;
            int largestError = 0;
            for (int target = 0; target < 2; ++target) {
                EGLSurface surface = target == 0 ? firstSurface : secondSurface;
                require(eglMakeCurrent(display, surface, surface, context) == EGL_TRUE,
                        "ring render-target switch");
                glActiveTexture(GL_TEXTURE0);
                glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture);
                glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER,
                                pass == 0 ? GL_NEAREST : GL_LINEAR);
                bindImage(GL_TEXTURE_EXTERNAL_OES, images[slot]);
                require(glGetError() == GL_NO_ERROR, "ring image rebind");
                const uint32_t targetWidth = target == 0 ? width : magnifiedWidth;
                const uint32_t targetHeight = target == 0 ? height : magnifiedHeight;
                glViewport(0, 0, targetWidth, targetHeight);
                glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
                completeSampling(display, createSync, waitSync, destroySync);
                const int cb = 92 + slot * 3;
                const int cr = 164 - slot * 2;
                for (uint32_t region = 0; region < 4; ++region) {
                    unsigned char sample[4]{};
                    const GLint column = targetWidth / 4 + (region % 2) * (targetWidth / 2);
                    const GLint row = targetHeight / 4 + (region / 2) * (targetHeight / 2);
                    glReadPixels(column, row, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, sample);
                    require(glGetError() == GL_NO_ERROR, "ring sample readback");
                    const double luminance =
                            1.164383 * (32 + slot * 7 + ((region + pass) % 4) * 8 - 16);
                    const int expected[] = {channel(luminance + 1.596027 * (cr - 128)),
                                            channel(luminance - 0.391762 * (cb - 128) -
                                                    0.812968 * (cr - 128)),
                                            channel(luminance + 2.017232 * (cb - 128))};
                    for (int component = 0; component < 3; ++component) {
                        largestError = std::max(largestError,
                                                std::abs(sample[component] - expected[component]));
                    }
                }
            }
            std::printf("ring pass=%d slot=%zu targets=2 maximum BT.601 error=%d LSB\n", pass, slot,
                        largestError);
            require(largestError <= 3, "ring BT.601 samples within 3 LSB");
        }
    }
    glFinish();
    for (EGLImageKHR image : images) {
        require(destroyImage(display, image) == EGL_TRUE, "ring EGLImage destruction");
    }
    imports.clear();
    sources.clear();
    require(eglMakeCurrent(display, firstSurface, firstSurface, context) == EGL_TRUE,
            "ring first render target restored");
    require(eglDestroySurface(display, secondSurface) == EGL_TRUE,
            "ring second render target destruction");
    std::puts("PASS: sixteen persistent ION images, two targets, nearest/linear sampling");
}

int main(int argumentCount, char** arguments) {
    const bool checkRing = argumentCount == 2 && std::strcmp(arguments[1], "--ring") == 0;
    if (argumentCount != 1 && !checkRing) {
        std::fprintf(stderr, "Usage: %s [--ring]\n", arguments[0]);
        return 2;
    }
    constexpr uint32_t width = 1920;
    constexpr uint32_t height = 1080;
    LegacyCameraIonBuffer importer;
    require(importer.initialize(width, height) == NO_ERROR, "Venus gralloc ABI/layout");
    require(importer.allocationSize() == 3145728, "Venus allocation size");
    sp<GraphicBuffer> original =
            new GraphicBuffer(width, height, LegacyCameraIonBuffer::kVenusFormat, 1,
                              LegacyCameraIonBuffer::kUsage, "ION EGL alias probe");
    require(original->initCheck() == NO_ERROR, "source allocation");
    sp<GraphicBuffer> imported = importer.import(original->handle->data[0], 3145728, 0);
    require(imported != nullptr, "ION handle import");
    // msm8974 dma-buf exports share an inode; changed-pattern reads verify allocation aliasing.
    struct stat originalStat{}, importedStat{};
    require(fstat(original->handle->data[0], &originalStat) == 0 &&
                    fstat(imported->handle->data[0], &importedStat) == 0 &&
                    originalStat.st_dev == importedStat.st_dev &&
                    originalStat.st_ino == importedStat.st_ino,
            "source/import descriptor class consistency");
    require(importer.import(original->handle->data[0], 3145728, 128) == nullptr,
            "nonzero offset rejection");
    require(importer.import(original->handle->data[0], 3145727, 0) == nullptr,
            "allocation size rejection");

    EGLDisplay display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    require(eglInitialize(display, nullptr, nullptr) == EGL_TRUE, "EGL initialize");
    const EGLint configAttributes[] = {EGL_SURFACE_TYPE,
                                       EGL_PBUFFER_BIT,
                                       EGL_RENDERABLE_TYPE,
                                       EGL_OPENGL_ES2_BIT,
                                       EGL_RED_SIZE,
                                       8,
                                       EGL_GREEN_SIZE,
                                       8,
                                       EGL_BLUE_SIZE,
                                       8,
                                       EGL_ALPHA_SIZE,
                                       8,
                                       EGL_NONE};
    EGLConfig config{};
    EGLint count = 0;
    require(eglChooseConfig(display, configAttributes, &config, 1, &count) && count == 1,
            "EGL pbuffer config");
    const EGLint contextAttributes[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttributes);
    const EGLint pbufferAttributes[] = {EGL_WIDTH, static_cast<EGLint>(width), EGL_HEIGHT,
                                        static_cast<EGLint>(height), EGL_NONE};
    EGLSurface surface = eglCreatePbufferSurface(display, config, pbufferAttributes);
    require(context != EGL_NO_CONTEXT && surface != EGL_NO_SURFACE &&
                    eglMakeCurrent(display, surface, surface, context),
            "EGL current context");
    std::printf("GPU: %s; EGL extensions: %s\n", glGetString(GL_RENDERER),
                eglQueryString(display, EGL_EXTENSIONS));
    auto createImage =
            reinterpret_cast<PFNEGLCREATEIMAGEKHRPROC>(eglGetProcAddress("eglCreateImageKHR"));
    auto destroyImage =
            reinterpret_cast<PFNEGLDESTROYIMAGEKHRPROC>(eglGetProcAddress("eglDestroyImageKHR"));
    auto bindImage = reinterpret_cast<PFNGLEGLIMAGETARGETTEXTURE2DOESPROC>(
            eglGetProcAddress("glEGLImageTargetTexture2DOES"));
    auto createSync =
            reinterpret_cast<PFNEGLCREATESYNCKHRPROC>(eglGetProcAddress("eglCreateSyncKHR"));
    auto waitSync = reinterpret_cast<PFNEGLCLIENTWAITSYNCKHRPROC>(
            eglGetProcAddress("eglClientWaitSyncKHR"));
    auto destroySync =
            reinterpret_cast<PFNEGLDESTROYSYNCKHRPROC>(eglGetProcAddress("eglDestroySyncKHR"));
    require(createImage && destroyImage && bindImage && createSync && waitSync && destroySync,
            "EGLImage and KHR completion entry points");
    require(strstr(eglQueryString(display, EGL_EXTENSIONS), "EGL_KHR_fence_sync") != nullptr,
            "KHR completion extension advertised");
    const EGLint imageAttributes[] = {EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE};
    EGLImageKHR image = createImage(display, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID,
                                    imported->getNativeBuffer(), imageAttributes);
    require(image != EGL_NO_IMAGE_KHR, "imported ION EGLImage");
    GLuint texture = 0;
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    bindImage(GL_TEXTURE_EXTERNAL_OES, image);
    require(glGetError() == GL_NO_ERROR, "external texture bind");
    GLuint vertex = shader(GL_VERTEX_SHADER,
                           "attribute vec2 p; varying vec2 uv; void main(){uv=(p+1.0)*0.5;"
                           "gl_Position=vec4(p,0.0,1.0);}");
    GLuint fragment = shader(GL_FRAGMENT_SHADER,
                             "#extension GL_OES_EGL_image_external : require\n"
                             "precision highp float; varying vec2 uv; uniform samplerExternalOES t;"
                             "void main(){gl_FragColor=texture2D(t,uv);}");
    GLuint program = glCreateProgram();
    glAttachShader(program, vertex);
    glAttachShader(program, fragment);
    glLinkProgram(program);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    require(linked == GL_TRUE, "program link");
    glUseProgram(program);
    const GLfloat vertices[] = {-1, -1, 1, -1, -1, 1, 1, 1};
    GLint position = glGetAttribLocation(program, "p");
    glVertexAttribPointer(position, 2, GL_FLOAT, GL_FALSE, 0, vertices);
    glEnableVertexAttribArray(position);
    glUniform1i(glGetUniformLocation(program, "t"), 0);
    glViewport(0, 0, width, height);
    std::vector<unsigned char> pixels(width * height * 4);
    for (int arm = 0; arm < 2; ++arm) {
        android_ycbcr planes{};
        require(original->lockYCbCr(GRALLOC_USAGE_SW_WRITE_OFTEN, &planes) == NO_ERROR,
                "pattern source lock");
        const unsigned char luma[] = {16, 81, 145, 235};
        for (uint32_t row = 0; row < height; ++row) {
            auto* output = static_cast<unsigned char*>(planes.y) + row * planes.ystride;
            for (uint32_t column = 0; column < width; ++column) {
                output[column] =
                        luma[arm == 0 ? (row >= height / 2 ? 2 : 0) + (column >= width / 2 ? 1 : 0)
                                      : (row + column) % 4];
            }
        }
        const int cb = arm == 0 ? 128 : 90;
        const int cr = arm == 0 ? 128 : 180;
        for (uint32_t row = 0; row < height / 2; ++row) {
            for (uint32_t column = 0; column < width / 2; ++column) {
                static_cast<unsigned char*>(
                        planes.cb)[row * planes.cstride + column * planes.chroma_step] = cb;
                static_cast<unsigned char*>(
                        planes.cr)[row * planes.cstride + column * planes.chroma_step] = cr;
            }
        }
        require(original->unlock() == NO_ERROR, "pattern source cache clean");
        bindImage(GL_TEXTURE_EXTERNAL_OES, image);
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        completeSampling(display, createSync, waitSync, destroySync);
        glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
        require(glGetError() == GL_NO_ERROR, "pattern draw/readback");
        int largestError = 0;
        for (uint32_t row = 2; row + 2 < height; ++row) {
            for (uint32_t column = 2; column + 2 < width; ++column) {
                const uint32_t region = arm == 0
                        ? (row >= height / 2 ? 2 : 0) + (column >= width / 2 ? 1 : 0)
                        : (row + column) % 4;
                const double luminance = 1.164383 * (luma[region] - 16);
                const int expected[] = {channel(luminance + 1.596027 * (cr - 128)),
                                        channel(luminance - 0.391762 * (cb - 128) -
                                                0.812968 * (cr - 128)),
                                        channel(luminance + 2.017232 * (cb - 128))};
                for (int component = 0; component < 3; ++component) {
                    largestError =
                            std::max(largestError,
                                     std::abs(pixels[(row * width + column) * 4 + component] -
                                              expected[component]));
                }
            }
        }
        std::printf("pattern arm=%d maximum BT.601 error=%d LSB\n", arm, largestError);
        require(largestError <= 3, "BT.601 readback within 3 LSB");
    }
    if (checkRing) {
        checkImageRing(importer, display, config, context, surface, texture, width, height,
                       createImage, destroyImage, bindImage, createSync, waitSync, destroySync);
    }
    glFinish();
    glDeleteTextures(1, &texture);
    require(destroyImage(display, image) == EGL_TRUE, "EGLImage destroy");
    glDeleteProgram(program);
    glDeleteShader(vertex);
    glDeleteShader(fragment);
    imported.clear();
    original.clear();
    eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroySurface(display, surface);
    eglDestroyContext(display, context);
    eglTerminate(display);
    std::puts("PASS: ION EGL alias/color/lifetime probe");
    return 0;
}
