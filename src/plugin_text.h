/* SPDX-License-Identifier: GPL-3.0-or-later
 * The game's own UTF-16 strings, replaced where the code and data name them (plugin_text.c). */
#ifndef BB_PLUGIN_TEXT_H
#define BB_PLUGIN_TEXT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint64_t at;           /* the game's string, by image offset (Binary Ninja address - 0x400000) */
    const uint16_t *text;  /* UTF-16, ended by a 0; up to 4096 characters are used */
} BbTextItem;

typedef struct {
    size_t strings;       /* strings with a reference moved */
    size_t leas;          /* instructions changed */
    size_t pointers;      /* pointers changed */
    size_t unreferenced;  /* strings nothing was found to name */
    size_t skipped;       /* references left alone: a hook or patch owns those bytes, or out of reach */
    size_t rejected;      /* not a whole read-only string of the image, empty, twice, or no room */
    size_t already;       /* replaced by an earlier call (a plugin before): the first one stays */
} BbTextResult;

/* Before the game runs (the plugins' image phase), on the image as loaded. */
BbTextResult bb_text_replace(const BbTextItem *items, size_t count);

#ifdef __cplusplus
}
#endif

#endif
