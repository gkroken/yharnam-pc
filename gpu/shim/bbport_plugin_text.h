// SPDX-License-Identifier: GPL-3.0-or-later
// Text native plugins put over the game (show_message, show_text in include/bbhost_plugin.h):
// one banner and one placed line, each replacing the last, fading over its final half second.
// Set from any thread (bbgpu_plugin_message, bbgpu_plugin_text in gpu/bbgpu.h); the overlay
// (bbport_overlay.cpp) draws them on the present thread.
#pragma once

namespace BbPluginText {

void Show(const char* text, float seconds);
void ShowText(const char* text, float seconds, float x, float y, float scale, unsigned align);
bool Busy();
void Draw(float ui_scale);

} // namespace BbPluginText
