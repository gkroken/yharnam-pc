/* SPDX-License-Identifier: GPL-3.0-or-later
 * input_take.h: what plugins take from the game. Set from any thread, read by the pad read and
 * the mouse camera; each piece is its own atomic, so a reader may see one call's keys with the
 * last call's buttons for a moment, never a torn value. */
#include "input_take.h"

#include <string.h>

static uint32_t g_keys[BB_TAKE_KEYS / 32];
static uint32_t g_buttons;
static int g_wheel, g_motion, g_any;
static int (*g_playing)(void);

#define LOAD(x) __atomic_load_n(&(x), __ATOMIC_ACQUIRE)
#define STORE(x, v) __atomic_store_n(&(x), (v), __ATOMIC_RELEASE)

void bb_input_take_set(const uint16_t *scancodes, size_t n, uint32_t buttons, int wheel, int motion) {
    uint32_t keys[BB_TAKE_KEYS / 32] = {0};
    int any = 0;
    for (size_t i = 0; scancodes && i < n; ++i) {
        const unsigned code = scancodes[i];
        if (code == 0 || code >= BB_TAKE_KEYS) continue;
        keys[code / 32] |= 1u << (code % 32);
        any = 1;
    }
    buttons &= 0x1f;
    for (size_t w = 0; w < BB_TAKE_KEYS / 32; ++w) STORE(g_keys[w], keys[w]);
    STORE(g_buttons, buttons);
    STORE(g_wheel, wheel != 0);
    STORE(g_motion, motion != 0);
    STORE(g_any, any || buttons || wheel || motion);
}

void bb_input_take_set_playing(int (*playing)(void)) { __atomic_store_n(&g_playing, playing, __ATOMIC_RELEASE); }

int bb_input_take_active(void) {
    int (*playing)(void) = __atomic_load_n(&g_playing, __ATOMIC_ACQUIRE);
    return LOAD(g_any) && playing && playing();
}

const bool *bb_input_take_keys(const bool *keys, int *nkeys, bool scratch[BB_TAKE_KEYS]) {
    if (!keys || !bb_input_take_active()) return keys;
    const int n = *nkeys < BB_TAKE_KEYS ? (*nkeys > 0 ? *nkeys : 0) : BB_TAKE_KEYS;
    memset(scratch, 0, BB_TAKE_KEYS * sizeof *scratch);
    memcpy(scratch, keys, (size_t)n * sizeof *scratch);
    for (int code = 1; code < n; ++code)
        if (LOAD(g_keys[code / 32]) >> (code % 32) & 1) scratch[code] = false;
    *nkeys = n;
    return scratch;
}

uint32_t bb_input_take_buttons(void) { return bb_input_take_active() ? LOAD(g_buttons) : 0; }
int bb_input_take_wheel(void) { return bb_input_take_active() && LOAD(g_wheel); }
int bb_input_take_motion(void) { return bb_input_take_active() && LOAD(g_motion); }
