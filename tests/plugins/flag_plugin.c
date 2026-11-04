/* SPDX-License-Identifier: GPL-3.0-or-later
 * The plugins tests/test_flag_delivery.c loads, built once per VARIANT:
 *   1 force_on   registers on_event_flag in bb_plugin_image; whenever flag 7 changes to 0, sets
 *                it back to 1
 *   2 force_off  the same, the other way: together they fight over flag 7 forever
 *   3 late       registers on_event_flag only when asked, after the start
 * All three also offer set calls the test makes from other threads. */
#include "bbhost_plugin.h"

#ifndef VARIANT
#error VARIANT must be set
#endif

#if VARIANT == 1
#define NAME "force_on"
#elif VARIANT == 2
#define NAME "force_off"
#else
#define NAME "late"
#endif

static const BbHostApi *g_api;
static int g_seen;  /* changes of flag 7 this plugin was told about */
static int g_loads;
static uint32_t g_last_block;
__attribute__((unused)) static void on_load(uint32_t block, void *user) {
    (void)user;
    ++g_loads;
    g_last_block = block;
}
BB_PLUGIN_EXPORT int world_loads(void) { return g_loads; }
BB_PLUGIN_EXPORT uint32_t world_block(void) { return g_last_block; }
BB_PLUGIN_EXPORT int say(const char *text) { return g_api->show_message(text, 2.0f); }
BB_PLUGIN_EXPORT int place(const char *text) { return g_api->show_text(text, 3.0f, 0.25f, 0.5f, 1.5f, BB_TEXT_RIGHT); }

static void on_flag(uint32_t id, int value, void *user) {
    (void)user;
    if (id != 7) return;
    ++g_seen;
#if VARIANT == 1
    if (!value) g_api->event_flag_set(7, 1);
#elif VARIANT == 2
    if (value) g_api->event_flag_set(7, 0);
#else
    (void)value;
#endif
}

BB_PLUGIN_EXPORT int flag_seen(void) { return g_seen; }
BB_PLUGIN_EXPORT int flag_set(uint32_t id, int value) { return g_api->event_flag_set(id, value); }
BB_PLUGIN_EXPORT int stat_set(int64_t value) { return g_api->player_stat_set("echoes", value); }
BB_PLUGIN_EXPORT int late_register(void) { return g_api->on_event_flag(on_flag, 0); }
BB_PLUGIN_EXPORT int chr_count(size_t *count) { return g_api->chr_list(0, 0, count); }

BB_PLUGIN_EXPORT const BbPluginInfo bb_plugin_info = {BB_PLUGIN_API_VERSION, 0, NAME, NAME, "1", "tests", "flags"};

BB_PLUGIN_EXPORT int bb_plugin_image(const BbHostApi *api) {
    g_api = api;
#if VARIANT == 3
    return api->on_world_load(on_load, 0);
#else
    return api->on_event_flag(on_flag, 0);  /* nonzero (no watcher) would unload: the test sees it */
#endif
}
