/* SPDX-License-Identifier: GPL-3.0-or-later
 * src/plugin_text.c on a fake image: the leas and pointers that name a string now name a copy of
 * the new text; the string itself, other strings' references, and bytes a hook owns are left
 * alone; what isn't a whole read-only string of the image is refused. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "engine_hooks.h"
#include "plugin_text.h"

static int checks;
#define CHECK(cond)                                                                              \
    do {                                                                                         \
        ++checks;                                                                                \
        if (!(cond)) {                                                                           \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #cond);             \
            exit(1);                                                                             \
        }                                                                                        \
    } while (0)

enum { IMAGE = 0x100000, CODE = 0x80000 };
static unsigned char *image;

static void put_utf16(uint64_t at, const char *s) {
    for (size_t i = 0;; ++i) {
        image[at + i * 2] = (unsigned char)s[i];
        image[at + i * 2 + 1] = 0;
        if (!s[i]) break;
    }
}
/* lea r64, [rip + disp32] at `at` naming `target`: rex 48 or 4c, modrm reg bits. */
static void put_lea(uint64_t at, unsigned char rex, unsigned char reg, uint64_t target) {
    image[at] = rex, image[at + 1] = 0x8d, image[at + 2] = (unsigned char)(0x05 | reg << 3);
    const int32_t disp = (int32_t)((int64_t)target - (int64_t)(at + 7));
    memcpy(image + at + 3, &disp, 4);
}
static const unsigned char *lea_target(uint64_t at) {
    int32_t disp;
    memcpy(&disp, image + at + 3, 4);
    return image + at + 7 + disp;
}
static int utf16_is(const unsigned char *p, const char *s) {
    for (size_t i = 0;; ++i) {
        if (p[i * 2] != (unsigned char)s[i] || p[i * 2 + 1]) return 0;
        if (!s[i]) return 1;
    }
}

int main(void) {
    image = VirtualAlloc(NULL, IMAGE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    CHECK(image && bb_hooks_init(image, IMAGE) == 0);
    bb_image_code(0, CODE);  /* code and read-only data, as the eboot's r-x segment; the rest data */

    put_utf16(0x1000, "OLD TEXT");
    put_utf16(0x1100, "OTHER");
    put_utf16(0x1200, "NOBODY");                     /* named by nothing */
    put_lea(0x2000, 0x48, 0, 0x1000);                /* lea rax */
    put_lea(0x2100, 0x4c, 1, 0x1000);                /* lea r9 */
    put_lea(0x2200, 0x48, 2, 0x1100);                /* another string's */
    put_lea(0x2300, 0x48, 0, 0x1000);                /* inside a hook's bytes */
    bb_image_rewritten(0x2302, 4);
    image[0x2400] = 0x48, image[0x2401] = 0x8d, image[0x2402] = 0x44;  /* lea with a SIB: not ours */
    const uint64_t at_old = (uint64_t)(uintptr_t)(image + 0x1000);
    memcpy(image + 0x90000, &at_old, 8);             /* a pointer in the data */
    memcpy(image + 0x10008, &at_old, 8);             /* and one in the r-x segment's data */
    memcpy(image + 0x90013, &at_old, 8);             /* unaligned: not a slot */
    unsigned char before_2200[7], before_old[18];
    memcpy(before_2200, image + 0x2200, 7);
    memcpy(before_old, image + 0x1000, 18);

    static const uint16_t new_text[] = {'N', 'E', 'W', 0}, empty[] = {0}, more[] = {'X', 0};
    const BbTextItem items[] = {
        {0x1000, new_text},
        {0x1200, more},
        {0x1001, new_text},   /* odd */
        {0x90100, new_text},  /* in the writable data */
        {0x1100, empty},      /* empty */
        {0x1000, more},       /* twice */
        {IMAGE + 0x10, new_text},
        {0x1100, NULL},
    };
    const BbTextResult r = bb_text_replace(items, sizeof items / sizeof *items);
    CHECK(r.strings == 1 && r.unreferenced == 1 && r.rejected == 6);
    CHECK(r.leas == 2 && r.pointers == 2 && r.skipped == 1);

    const unsigned char *copy = lea_target(0x2000);
    CHECK(copy != image + 0x1000 && utf16_is(copy, "NEW"));
    CHECK(lea_target(0x2100) == copy && image[0x2100] == 0x4c && image[0x2102] == 0x0d);  /* r9 kept */
    CHECK(!memcmp(image + 0x2200, before_2200, 7));          /* the other string's lea */
    CHECK(lea_target(0x2300) == image + 0x1000);             /* the hook's bytes untouched */
    uint64_t slot;
    memcpy(&slot, image + 0x90000, 8);
    CHECK(slot == (uint64_t)(uintptr_t)copy);
    memcpy(&slot, image + 0x10008, 8);
    CHECK(slot == (uint64_t)(uintptr_t)copy);
    memcpy(&slot, image + 0x90013, 8);
    CHECK(slot == at_old);                                   /* unaligned left */
    CHECK(!memcmp(image + 0x1000, before_old, 18));          /* the string itself unchanged */
    CHECK(bb_image_is_rewritten(0x2000, 7) && bb_image_is_rewritten(0x90000, 8));  /* recorded */
    const int64_t reach = (int64_t)((uintptr_t)copy - (uintptr_t)image);
    CHECK(reach > -0x7ff00000LL && reach < 0x7ff00000LL);

    /* A second replacement of the same string (another plugin): refused as already done, the
     * first copy stays named (port-128d4b0-N2). An unreferenced string isn't "done". */
    static const uint16_t second[] = {'S', 'E', 'C', 0};
    const BbTextItem again[] = {{0x1000, second}, {0x1200, second}};
    const BbTextResult r2 = bb_text_replace(again, 2);
    CHECK(r2.already == 1 && r2.leas == 0 && r2.unreferenced == 1 && r2.rejected == 0);
    CHECK(lea_target(0x2000) == copy && utf16_is(copy, "NEW"));

    /* Nothing, or nothing valid: nothing changes. */
    const BbTextResult none = bb_text_replace(items, 0);
    CHECK(none.strings == 0 && none.leas == 0 && none.rejected == 0);
    CHECK(bb_text_replace(NULL, 3).rejected == 0);

    printf("text: %d checks passed\n", checks);
    return 0;
}
