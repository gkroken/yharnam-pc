/* SPDX-License-Identifier: GPL-3.0-or-later
 * The game's own text, replaced (bbhost's replace_text, API 11): every reference to a string,
 * each rip-relative lea that loads its address and each pointer to it in the image, names a copy
 * of the new text instead, kept within rel32 reach of the image. The string itself stays as it
 * was, so whatever measures or compares it in place sees no change. A string the code reaches
 * another way (an offset added to another address) keeps its text.
 *
 * As bbhost does it (droogie/bbhost src/engine/image_text.cpp, GPL-3.0). Two differences, both
 * from how this port loads the image: the copies live in a region the hooks reserve beside the
 * image, and the pointers are found by value (an 8-byte slot holding the string's address) rather
 * than from the loader's relocation list. Before the game runs, only the loader's relocations
 * have put absolute addresses into the image, so the two find the same slots. */
#include "plugin_text.h"

#include <string.h>

#include "engine_hooks.h"

typedef struct {
    uint64_t at;        /* the string's image offset */
    unsigned char *to;  /* its copy */
} Moved;

static int find(const Moved *moved, int n, uint64_t at) {
    for (int i = 0; i < n; ++i)
        if (moved[i].at == at) return i;
    return -1;
}

BbTextResult bb_text_replace(const BbTextItem *items, size_t count) {
    BbTextResult out = {0};
    unsigned char *image = bb_guest(0);
    const size_t size = bb_image_size();
    enum { MAX_ITEMS = 4096, MAX_CHARS = 4096 };
    static Moved moved[MAX_ITEMS];
    int n = 0;
    if (!image || !size || !items) return out;

    /* Each new text into memory beside the image. */
    for (size_t i = 0; i < count; ++i) {
        const BbTextItem *t = &items[i];
        size_t len = 0;
        while (t->text && len < MAX_CHARS && t->text[len]) ++len;
        if (!t->text || !len || (t->at & 1) || t->at + 2 > size || !bb_image_is_code(t->at, 2) || n == MAX_ITEMS ||
            find(moved, n, t->at) >= 0) {
            ++out.rejected;
            continue;
        }
        /* Only a string the image holds read-only: here that is inside its r-x segment, whole. */
        size_t old = 0;
        while (t->at + old * 2 + 2 <= size && (image[t->at + old * 2] || image[t->at + old * 2 + 1])) ++old;
        if (!bb_image_is_code(t->at, old * 2 + 2) || bb_image_is_rewritten(t->at, old * 2 + 2)) {
            ++out.rejected;
            continue;
        }
        unsigned char *copy = bb_image_near((len + 1) * 2);
        if (!copy) {
            ++out.rejected;
            continue;
        }
        memcpy(copy, t->text, len * 2);
        copy[len * 2] = copy[len * 2 + 1] = 0;
        moved[n].at = t->at;
        moved[n++].to = copy;
    }
    if (!n) return out;

    int refs[MAX_ITEMS] = {0};
    /* The code: lea r64, [rip + disp32] (REX.W or REX.WR, 8D, mod 00 rm 101) naming a string. */
    for (uint64_t i = 0; i + 7 <= size; ++i) {
        const unsigned char *p = image + i;
        if ((p[0] != 0x48 && p[0] != 0x4c) || p[1] != 0x8d || (p[2] & 0xc7) != 0x05) continue;
        if (!bb_image_is_code(i, 7)) continue;
        int32_t disp;
        memcpy(&disp, p + 3, 4);
        const int64_t target = (int64_t)(i + 7) + disp;
        if (target < 0) continue;
        const int k = find(moved, n, (uint64_t)target);
        if (k < 0) continue;
        if (bb_image_is_rewritten(i, 7)) {  /* a hook or patch owns these bytes */
            ++out.skipped;
            continue;
        }
        const int64_t to = (int64_t)((uintptr_t)moved[k].to - (uintptr_t)(image + i + 7));
        if (to < INT32_MIN || to > INT32_MAX) {
            ++out.skipped;
            continue;
        }
        const int32_t rel = (int32_t)to;
        memcpy(image + i + 3, &rel, 4);
        bb_image_rewritten(i, 7);
        ++out.leas;
        ++refs[k];
        i += 6;
    }
    /* The data: 8-byte slots holding a string's address. */
    for (uint64_t i = 0; i + 8 <= size; i += 8) {
        uint64_t value;
        memcpy(&value, image + i, 8);
        if (value < (uint64_t)(uintptr_t)image || value - (uint64_t)(uintptr_t)image >= size) continue;
        const int k = find(moved, n, value - (uint64_t)(uintptr_t)image);
        if (k < 0) continue;
        if (bb_image_is_rewritten(i, 8)) {
            ++out.skipped;
            continue;
        }
        const uint64_t to = (uint64_t)(uintptr_t)moved[k].to;
        memcpy(image + i, &to, 8);
        bb_image_rewritten(i, 8);
        ++out.pointers;
        ++refs[k];
    }
    for (int k = 0; k < n; ++k) {
        if (refs[k]) ++out.strings;
        else ++out.unreferenced;
    }
    return out;
}
