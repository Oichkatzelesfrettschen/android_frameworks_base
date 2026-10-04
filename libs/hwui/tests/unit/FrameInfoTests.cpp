// SPDX-License-Identifier: Apache-2.0

#include <gtest/gtest.h>

#include <utility>

#include "FrameInfo.h"

using namespace android::uirenderer;

// dumpsys gfxinfo framestats prints FrameInfoNames as the header of rows that
// FrameInfo fills by FrameInfoIndex, so each name sits at its own index.
TEST(FrameInfo, namesFollowIndexOrder) {
    const std::pair<FrameInfoIndex, const char*> expected[] = {
            {FrameInfoIndex::Flags, "Flags"},
            {FrameInfoIndex::FrameTimelineVsyncId, "FrameTimelineVsyncId"},
            {FrameInfoIndex::IntendedVsync, "IntendedVsync"},
            {FrameInfoIndex::Vsync, "Vsync"},
            {FrameInfoIndex::InputEventId, "InputEventId"},
            {FrameInfoIndex::HandleInputStart, "HandleInputStart"},
            {FrameInfoIndex::AnimationStart, "AnimationStart"},
            {FrameInfoIndex::PerformTraversalsStart, "PerformTraversalsStart"},
            {FrameInfoIndex::DrawStart, "DrawStart"},
            {FrameInfoIndex::FrameDeadline, "FrameDeadline"},
            {FrameInfoIndex::FrameStartTime, "FrameStartTime"},
            {FrameInfoIndex::FrameInterval, "FrameInterval"},
            {FrameInfoIndex::SyncQueued, "SyncQueued"},
            {FrameInfoIndex::SyncStart, "SyncStart"},
            {FrameInfoIndex::IssueDrawCommandsStart, "IssueDrawCommandsStart"},
            {FrameInfoIndex::SwapBuffers, "SwapBuffers"},
            {FrameInfoIndex::FrameCompleted, "FrameCompleted"},
            {FrameInfoIndex::DequeueBufferDuration, "DequeueBufferDuration"},
            {FrameInfoIndex::QueueBufferDuration, "QueueBufferDuration"},
            {FrameInfoIndex::GpuCompleted, "GpuCompleted"},
            {FrameInfoIndex::SwapBuffersCompleted, "SwapBuffersCompleted"},
            {FrameInfoIndex::DisplayPresentTime, "DisplayPresentTime"},
            {FrameInfoIndex::CommandSubmissionCompleted, "CommandSubmissionCompleted"},
    };
    static_assert(std::size(expected) == static_cast<size_t>(FrameInfoIndex::NumIndexes));
    for (const auto& [index, name] : expected) {
        EXPECT_STREQ(name, FrameInfoNames[static_cast<int>(index)]);
    }
}
