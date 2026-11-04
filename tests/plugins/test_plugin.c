/* SPDX-License-Identifier: GPL-3.0-or-later
 * The plugins tests/test_plugin_host.c loads. One source, built once per VARIANT (gpu/CMakeLists.txt):
 *   1 good       records what the API does in each phase, ticks, patches the test image
 *   2 good       the same file name in a later folder: must replace variant 1
 *   3 fails      registers a frame callback, then fails bb_plugin_image: must be unloaded with it
 *   4 initfail   fails bb_plugin_init
 *   5 notplugin  exports neither entry point
 *   6 future     built for API 99
 *   7 optin      BB_PLUGIN_OPT_IN: reads its option from plugins.ini
 *   8 gameplay   BB_PLUGIN_GAMEPLAY: left out of online play
 *   9 badname    an invalid info->name
 *  10 zdup       another file whose info->name is "gameplay": left out as a duplicate
 * Variant 2 calls itself "good_b", so only the folders' override, not the duplicate-name rule,
 * can explain that it loads instead of variant 1. */
#include "bbhost_plugin.h"

#include <stdio.h>
#include <string.h>

#ifndef VARIANT
#error VARIANT must be set
#endif

static const BbHostApi *g_api;
static int g_frames;
/* What each check saw; tests/test_plugin_host.c reads them through test_results(). */
static long long g_results[64];
static char g_option[64];
#if VARIANT == 1 || VARIANT == 2
static const char g_readonly[16] = "read-only data";
#endif

BB_PLUGIN_EXPORT int test_variant(void) { return VARIANT; }
BB_PLUGIN_EXPORT int test_frames(void) { return g_frames; }
BB_PLUGIN_EXPORT const long long *test_results(void) { return g_results; }
BB_PLUGIN_EXPORT const char *test_option(void) { return g_option; }
BB_PLUGIN_EXPORT const BbHostApi *test_api(void) { return g_api; }

#if VARIANT != 5

static void on_frame(void *user) {
    (void)user;
    ++g_frames;
}

#if VARIANT == 1 || VARIANT == 2
static int noop2(BbHookCtx *ctx, void *user) {
    (void)ctx, (void)user;
    return 0;
}
#endif

#if VARIANT == 1
#define NAME "good"
#elif VARIANT == 2
#define NAME "good_b"
#elif VARIANT == 3
#define NAME "fails"
#elif VARIANT == 4
#define NAME "initfail"
#elif VARIANT == 6
#define NAME "future"
#elif VARIANT == 7
#define NAME "optin"
#elif VARIANT == 9
#define NAME "bad name!"
#else
#define NAME "gameplay"
#endif

BB_PLUGIN_EXPORT const BbPluginInfo bb_plugin_info = {
    VARIANT == 6 ? 99u : BB_PLUGIN_API_VERSION,
    VARIANT == 7 ? BB_PLUGIN_OPT_IN : VARIANT == 8 || VARIANT == 10 ? BB_PLUGIN_GAMEPLAY : 0u,
    NAME, "Test " NAME, "1.2.3", "tests", "A plugin for tests/test_plugin_host.c."};

BB_PLUGIN_EXPORT int bb_plugin_init(const BbHostApi *api) {
    g_api = api;
    g_results[0] = api->version;
    g_results[1] = api->size;
    /* Image functions refuse before the image phase. */
    g_results[2] = (long long)api->guest_addr(0x400010);
    /* ...and so do the other image functions, though this host's image exists already. */
    static const unsigned char nop2[2] = {0x90, 0x90}, int3[2] = {0xcc, 0xcc};
    unsigned char got[2] = {0};
    static int cell;
    g_results[40] = api->patch(0x400010, nop2, int3, 2);
    g_results[41] = api->read(0x400010, got, 2);
    g_results[42] = api->write((uint64_t)(uintptr_t)&cell, nop2, 1);
    g_results[43] = api->eboot_sha256()[0] == 0;
    g_results[44] = api->eboot_is_109();
    api->log("init of variant %d", VARIANT);
    return VARIANT == 4 ? 3 : 0;
}

BB_PLUGIN_EXPORT int bb_plugin_image(const BbHostApi *api) {
    g_results[3] = api->eboot_is_109();
    g_results[4] = api->on_frame(on_frame, 0);
#if VARIANT == 3
    return 1;
#endif
#if VARIANT == 7
    snprintf(g_option, sizeof g_option, "%s", api->config("optin.color"));
#endif
#if VARIANT == 1 || VARIANT == 2
    static const unsigned char nops[2] = {0x90, 0x90}, traps[2] = {0xcc, 0xcc}, wrong[2] = {0x12, 0x34};
    g_results[5] = (long long)api->guest_addr(0x400010);
    g_results[6] = api->patch(0x400010, wrong, traps, 2);        /* 1: bytes differ */
    g_results[7] = api->patch(0x400010, nops, traps, 2);         /* 0 */
    g_results[8] = api->patch(0x400000 + 0x100000, nops, traps, 2); /* 2: outside the image */
    unsigned char back[2] = {0};
    g_results[9] = api->read(0x400010, back, 2);                 /* 0, the patched bytes */
    g_results[10] = back[0] | back[1] << 8;
    /* A runtime address outside the image: this plugin's own data. */
    char copy[16] = {0};
    g_results[11] = api->read((uint64_t)(uintptr_t)g_readonly, copy, sizeof copy);
    g_results[12] = memcmp(copy, g_readonly, sizeof copy) == 0;
    static int cell;
    const int value = 77;
    g_results[13] = api->write((uint64_t)(uintptr_t)&cell, &value, sizeof value);
    g_results[14] = cell;
    g_results[15] = api->write((uint64_t)(uintptr_t)g_readonly, "x", 1); /* 1: read-only page */
    /* Functions this host does not have yet answer with their failure values. */
    int flag = 5;
    g_results[16] = api->event_flag_get(100, &flag);
    g_results[17] = flag;
    size_t bytes = 9;
    g_results[18] = api->param_row("EquipParamWeapon", 1, &bytes) == NULL;
    g_results[19] = (long long)bytes;
    g_results[20] = api->hook(0x400010, nops, 14, 0, 0);
    g_results[21] = api->replace_text(0, 0);
    g_results[22] = api->register_hle("sceKernelUsleep", 0);
    g_results[23] = (long long)api->symbol("WORLD_CHR_MAN_SINGLETON_PTR");
    g_results[24] = api->plugin_dir(NAME)[0] != 0;
    g_results[25] = strcmp(api->config("host.name"), "yharnam-pc") == 0;
    g_results[26] = api->plugin_dir("../escape")[0] == 0;
    g_results[27] = api->plugin_dir("..")[0] == 0;
    /* The test marks the image's first 0x8000 bytes as code (bb_image_code). */
    g_results[28] = api->write(0x400020, &value, sizeof value);           /* 1: code */
    g_results[29] = api->write(0x400000 + 0x9000, &value, sizeof value);  /* 0: data */
    g_results[30] = api->patch(0x400010, nops, traps, 2);                 /* 0: already patched */
    g_results[31] = api->write(api->guest_addr(0x400020), &value, sizeof value); /* 1: code, by runtime address */
    g_results[32] = api->write(0x400000 + 0x7ffe, &value, sizeof value);  /* 1: straddles code and data */
    g_results[33] = strcmp(api->eboot_sha256(), "00ff00ff") == 0;
    /* Not the 1.09 build: no hook (port-1840f95-F2). Only tried then, so 1.09 runs place none. */
    g_results[36] = api->eboot_is_109() ? -1 : api->hook2(0x400000 + 0x1000, NULL, 5, noop2, 0);
    /* A prologue with a TCB load while no displacement is known (the test sets it only in
     * scenario 6): refused, 2, and only tried on 1.09. */
    g_results[37] = api->eboot_is_109() ? api->hook2(0x400000 + 0x2000, NULL, 13, noop2, 0) : -1;
    /* The test marks a second code range at 0xa000: a write from data into it (port-cd2a2bf-N1). */
    g_results[34] = api->write(0x400000 + 0x9ffe, &value, sizeof value);  /* 1: data running into code */
    g_results[35] = api->write(0x400000 + 0x9ff8, &value, sizeof value);  /* 0: ends just before it */
#endif
    return 0;
}

#endif
