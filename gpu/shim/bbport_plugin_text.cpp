// SPDX-License-Identifier: GPL-3.0-or-later
// Text native plugins put over the game (show_message, show_text: include/bbhost_plugin.h), drawn
// by the overlay (bbport_overlay.cpp). One banner and one placed line, as bbhost has them; a new
// one replaces the last. Set from any thread, drawn on the present thread.
#include "bbport_plugin_text.h"

#include <algorithm>
#include <chrono>
#include <mutex>
#include <string>

#include "imgui.h"

namespace BbPluginText {
namespace {

using Clock = std::chrono::steady_clock;

struct Item {
    std::string text;
    Clock::time_point until{};
    float seconds = 0.0f;
    float x = 0.5f, y = 0.8f, scale = 1.0f;
    unsigned align = 1;  // BB_TEXT_CENTRE
};

std::mutex mutex;
Item banner, line;

constexpr float FADE = 0.5f;  // seconds, at the end

float Remaining(const Item& item, Clock::time_point now) {
    if (item.text.empty() || now >= item.until) return 0.0f;
    return std::chrono::duration<float>(item.until - now).count();
}

void Set(Item& item, const char* text, float seconds) {
    item.text = text ? text : "";
    if (item.text.size() > 400) item.text.resize(400);
    item.seconds = std::clamp(seconds, 0.1f, 60.0f);
    item.until = Clock::now() + std::chrono::milliseconds(static_cast<long long>(item.seconds * 1000.0f));
}

// One item: a dark rounded box behind the text, both fading out over the last FADE seconds.
void DrawItem(ImDrawList* draw, const Item& item, float remaining, float ui_scale, bool is_banner) {
    const ImVec2 display = ImGui::GetIO().DisplaySize;
    const float alpha = std::min(1.0f, remaining / FADE);
    const float size = ImGui::GetFontSize() * (is_banner ? 1.6f : std::clamp(item.scale, 0.25f, 4.0f));
    ImFont* font = ImGui::GetFont();
    const ImVec2 text = font->CalcTextSizeA(size, display.x * 0.9f, display.x * 0.9f, item.text.c_str());
    const float pad = 12.0f * ui_scale;
    const ImVec2 box(text.x + 2 * pad, text.y + 2 * pad);
    ImVec2 at;
    if (is_banner) {
        at = ImVec2((display.x - box.x) * 0.5f, display.y * 0.72f);
    } else {
        const float px = std::clamp(item.x, 0.0f, 1.0f) * display.x, py = std::clamp(item.y, 0.0f, 1.0f) * display.y;
        const unsigned side = item.align & 3u;
        at.x = side == 1 ? px - box.x * 0.5f : side == 2 ? px - box.x : px;
        at.y = (item.align & 4u) ? py - box.y : py;
        at.x = std::clamp(at.x, 0.0f, std::max(0.0f, display.x - box.x));
        at.y = std::clamp(at.y, 0.0f, std::max(0.0f, display.y - box.y));
    }
    draw->AddRectFilled(at, ImVec2(at.x + box.x, at.y + box.y), IM_COL32(10, 8, 6, static_cast<int>(200 * alpha)),
                        6.0f * ui_scale);
    draw->AddText(font, size, ImVec2(at.x + pad, at.y + pad), IM_COL32(236, 228, 210, static_cast<int>(255 * alpha)),
                  item.text.c_str(), nullptr, display.x * 0.9f);
}

} // namespace

void Show(const char* text, float seconds) {
    std::scoped_lock lock{mutex};
    Set(banner, text, seconds);
}

void ShowText(const char* text, float seconds, float x, float y, float scale, unsigned align) {
    std::scoped_lock lock{mutex};
    Set(line, text, seconds);
    line.x = x;
    line.y = y;
    line.scale = scale;
    line.align = align;
}

bool Busy() {
    std::scoped_lock lock{mutex};
    const auto now = Clock::now();
    return Remaining(banner, now) > 0.0f || Remaining(line, now) > 0.0f;
}

void Draw(float ui_scale) {
    Item b, l;
    {
        std::scoped_lock lock{mutex};
        b = banner;
        l = line;
    }
    const auto now = Clock::now();
    ImDrawList* draw = ImGui::GetForegroundDrawList();
    if (const float r = Remaining(l, now); r > 0.0f) DrawItem(draw, l, r, ui_scale, false);
    if (const float r = Remaining(b, now); r > 0.0f) DrawItem(draw, b, r, ui_scale, true);
}

} // namespace BbPluginText

extern "C" int bbgpu_plugin_message(const char* text, float seconds) {
    if (!text) return 1;
    BbPluginText::Show(text, seconds);
    return 0;
}

extern "C" int bbgpu_plugin_text(const char* text, float seconds, float x, float y, float scale, unsigned align) {
    if (!text) return 1;
    BbPluginText::ShowText(text, seconds, x, y, scale, align);
    return 0;
}
