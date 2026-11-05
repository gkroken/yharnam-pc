/* SPDX-License-Identifier: GPL-3.0-or-later
 * Inputs a plugin takes from the game (bbhost's input_take): keys, mouse buttons, the wheel and
 * the mouse's motion that, while the player is playing, no longer reach what they are bound to.
 * The menus keep every input. No SDL here: the pad read (runtime_pad.c) and the mouse camera
 * (mouse_camera.c) ask, and tests drive it directly. */
#ifndef BB_INPUT_TAKE_H
#define BB_INPUT_TAKE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum { BB_TAKE_KEYS = 512 };

/* Replaces what is taken (any thread). Scancodes outside 1..511 are ignored. buttons: BbInput's
 * bits (0 left, 1 right, 2 middle, 3 back, 4 forward). Nothing taken gives everything back. */
void bb_input_take_set(const uint16_t *scancodes, size_t n, uint32_t buttons, int wheel, int motion);

/* Whether the player is playing (in the world, no menu or text box): set by probe.c. Without
 * it nothing is ever taken. */
void bb_input_take_set_playing(int (*playing)(void));

/* 1 when something is taken and the player is playing: the filters below apply. */
int bb_input_take_active(void);

/* The keyboard state the game should see: keys itself when nothing is taken, else a copy in
 * scratch (BB_TAKE_KEYS entries) with the taken keys up. *nkeys is clamped to BB_TAKE_KEYS for
 * a copy. */
const bool *bb_input_take_keys(const bool *keys, int *nkeys, bool scratch[BB_TAKE_KEYS]);

/* The mouse buttons the game should not see (BbInput's bits), 0 when not active. */
uint32_t bb_input_take_buttons(void);
/* 1 when the wheel's notches, or the mouse's motion, should not reach the game. */
int bb_input_take_wheel(void);
int bb_input_take_motion(void);

#ifdef __cplusplus
}
#endif

#endif
