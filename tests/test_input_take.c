/* SPDX-License-Identifier: GPL-3.0-or-later
 * src/input_take.c: what plugins take from the game. The filters the pad read and the mouse
 * camera apply, driven directly: keys, buttons, wheel and motion; only while playing; a call
 * replaces the last; nothing taken gives everything back. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "input_take.h"

static int checks;
#define CHECK(cond)                                                                              \
    do {                                                                                         \
        ++checks;                                                                                \
        if (!(cond)) {                                                                           \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #cond);             \
            exit(1);                                                                             \
        }                                                                                        \
    } while (0)

static int g_playing;
static int playing(void) { return g_playing; }

int main(void) {
    bool keys[600], scratch[BB_TAKE_KEYS];
    memset(keys, 1, sizeof keys);  /* everything held, past 512 too (SDL's array may be longer) */
    int n = 600;

    /* Nothing taken, no playing test: the game's own array, untouched. */
    CHECK(bb_input_take_keys(keys, &n, scratch) == keys && n == 600);
    CHECK(!bb_input_take_active() && bb_input_take_buttons() == 0 && !bb_input_take_wheel() && !bb_input_take_motion());

    /* Taken, but without a playing test (headless, tests of other parts): nothing is taken. */
    const uint16_t f6_g[] = {63, 10};
    bb_input_take_set(f6_g, 2, 1u << 1, 1, 1);
    CHECK(!bb_input_take_active() && bb_input_take_keys(keys, &n, scratch) == keys);

    /* Playing: F6 and G read up, everything else as held, and the copy stops at 512. */
    bb_input_take_set_playing(playing);
    g_playing = 1;
    CHECK(bb_input_take_active());
    const bool *seen = bb_input_take_keys(keys, &n, scratch);
    CHECK(seen == scratch && n == BB_TAKE_KEYS);
    CHECK(!seen[63] && !seen[10] && seen[62] && seen[64] && seen[4] && seen[511] && seen[0]);
    CHECK(keys[63] && keys[10]);  /* the game's own array is never written */
    CHECK(bb_input_take_buttons() == 2 && bb_input_take_wheel() && bb_input_take_motion());

    /* A shorter array (SDL may report fewer keys): only those are copied, the rest up. */
    int short_n = 20;
    seen = bb_input_take_keys(keys, &short_n, scratch);
    CHECK(short_n == 20 && !seen[10] && seen[19] && !seen[20] && !seen[63]);

    /* In a menu (not playing): the menus keep every input. */
    g_playing = 0;
    n = 600;
    CHECK(bb_input_take_keys(keys, &n, scratch) == keys && n == 600);
    CHECK(bb_input_take_buttons() == 0 && !bb_input_take_wheel() && !bb_input_take_motion());
    g_playing = 1;

    /* A call replaces the last: now only the wheel; the keys and buttons come back. */
    bb_input_take_set(NULL, 0, 0, 1, 0);
    n = 600;
    seen = bb_input_take_keys(keys, &n, scratch);
    CHECK(seen[63] && seen[10] && bb_input_take_buttons() == 0 && bb_input_take_wheel() && !bb_input_take_motion());

    /* Out-of-range scancodes and button bits are ignored; 0 and 512+ never taken. */
    const uint16_t odd[] = {0, 512, 700, 511};
    bb_input_take_set(odd, 4, 0xffffffffu, 0, 0);
    n = 600;
    seen = bb_input_take_keys(keys, &n, scratch);
    CHECK(seen[0] && !seen[511] && seen[510] && bb_input_take_buttons() == 0x1f);

    /* Nothing taken gives everything back (and active is off, so the pad reads the array as is). */
    bb_input_take_set(NULL, 0, 0, 0, 0);
    n = 600;
    CHECK(!bb_input_take_active() && bb_input_take_keys(keys, &n, scratch) == keys && n == 600);
    CHECK(bb_input_take_keys(NULL, &n, scratch) == NULL);

    printf("input take: %d checks passed\n", checks);
    return 0;
}
