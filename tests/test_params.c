/* SPDX-License-Identifier: GPL-3.0-or-later
 * plugin_params.c against a fake SoloParamRepository at its real 1.09 address: tables by name
 * (a long name behind a pointer, a short one inline), rows by id, fields by name through a
 * definition table like the one scripts/paramdefs.py writes:
 * - every type: s32, f32, s8, a bitfield pair sharing a byte, a dummy8 array, a fixstr;
 * - range checks, bitfields keeping their neighbours, array bounds, read-only strings;
 * - a definition whose row size isn't the table's: its fields refused, its rows still found;
 * - no definitions at all; missing tables, rows and fields. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "engine_hooks.h"
#include "plugin_params.h"

enum { IMAGE_SIZE = 0x5600000 };
#define AT(bn) (image + ((bn) - 0x400000))

static int checks;
#define CHECK(cond)                                                                              \
    do {                                                                                         \
        ++checks;                                                                                \
        if (!(cond)) {                                                                           \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #cond);             \
            exit(1);                                                                             \
        }                                                                                        \
    } while (0)

static void put64(unsigned char *at, uint64_t v) { memcpy(at, &v, 8); }

/* A param file (format 5, 64-bit row table) with rows of 16 bytes for the ids given. */
static unsigned char *param_file(const char *type, const uint32_t *ids, int rows) {
    unsigned char *f = calloc(1, 0x200);
    const uint16_t n = (uint16_t)rows;
    memcpy(f + 0xa, &n, 2);
    memcpy(f + 0xc, type, strlen(type));
    f[0x2d] = 5;
    f[0x2e] = 2;
    for (int i = 0; i < rows; ++i) {
        memcpy(f + 0x40 + 0x18 * i, &ids[i], 4);
        put64(f + 0x48 + 0x18 * i, 0x100 + 16u * (unsigned)i);
    }
    return f;
}

/* A cap named `name` holding `file`. */
static unsigned char *cap(const char *name, unsigned char *file) {
    unsigned char *c = calloc(1, 0x80), *holder = calloc(1, 0x80);
    const size_t len = strlen(name);
    uint16_t *wide = calloc(len + 1, 2);
    for (size_t i = 0; i < len; ++i) wide[i] = (uint16_t)name[i];
    if (len < 8) {
        memcpy(c + 0x18, wide, (len + 1) * 2);  /* inline */
        put64(c + 0x30, 7);
    } else {
        put64(c + 0x18, (uint64_t)(uintptr_t)wide);
        put64(c + 0x30, len);
    }
    put64(holder + 0x70, (uint64_t)(uintptr_t)file);
    put64(c + 0x70, (uint64_t)(uintptr_t)holder);
    return c;
}

static const char *get(const char *table, uint32_t id, const char *field) {
    static char out[64];
    return bb_param_get(table, id, field, out, sizeof out) ? NULL : out;
}

int main(void) {
    unsigned char *image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    CHECK(bb_hooks_init(image, IMAGE_SIZE) == 0);

    /* The definitions, as paramdefs.py writes them. */
    char defs[MAX_PATH];
    GetTempPathA(MAX_PATH, defs);
    snprintf(defs + strlen(defs), MAX_PATH - strlen(defs), "bb-params-test-%lu.txt", GetCurrentProcessId());
    FILE *f = fopen(defs, "wb");
    assert(f);
    fputs("T TEST_PARAM_ST 16\n"
          "F hp s32 0 4 1 0 0\n"
          "F speed f32 4 4 1 0 0\n"
          "F small s8 8 1 1 0 0\n"
          "F flagA u8 9 1 1 0 1\n"
          "F flagB u8 9 1 1 1 3\n"
          "F pad dummy8 10 2 2 0 0\n"
          "F tag fixstr 12 4 4 0 0\n"
          "T BAD_PARAM_ST 20\n"
          "F hp s32 0 4 1 0 0\n", f);
    fclose(f);

    /* Nothing loaded yet. */
    char out[64];
    size_t bytes = 99, count = 99;
    CHECK(bb_param_row("TestParam", 10, &bytes) == NULL && bytes == 0);
    CHECK(bb_param_get("TestParam", 10, "hp", out, sizeof out) == 1 && out[0] == 0);
    CHECK(bb_param_ids("TestParam", NULL, 0, &count) == 1 && count == 0);

    static const uint32_t ids[3] = {10, 20, 30};
    unsigned char *test = param_file("TEST_PARAM_ST", ids, 3), *bad = param_file("BAD_PARAM_ST", ids, 3),
                  *shortf = param_file("TEST_PARAM_ST", ids, 1);
    unsigned char *repo = calloc(1, 0x70 + 63 * 0x48);
    const int32_t two = 2, one = 1;
    memcpy(repo + 0x70 + 3 * 0x48, &two, 4);
    put64(repo + 0x70 + 3 * 0x48 + 8, (uint64_t)(uintptr_t)cap("TestParam", test));
    put64(repo + 0x70 + 3 * 0x48 + 16, (uint64_t)(uintptr_t)cap("BadParam", bad));
    memcpy(repo + 0x70 + 40 * 0x48, &one, 4);
    put64(repo + 0x70 + 40 * 0x48 + 8, (uint64_t)(uintptr_t)cap("Short", shortf));
    put64(AT(0x5940340), (uint64_t)(uintptr_t)repo);

    /* Row 20 (the second): hp -5, speed 1.5, small -3, flags 1 and 6 in byte 9, pad {7, 9}, "ABC". */
    unsigned char *r20 = test + 0x110;
    const int32_t hp = -5;
    const float speed = 1.5f;
    memcpy(r20, &hp, 4);
    memcpy(r20 + 4, &speed, 4);
    r20[8] = (unsigned char)(int8_t)-3;
    r20[9] = 1 | (6 << 1);
    r20[10] = 7;
    r20[11] = 9;
    memcpy(r20 + 12, "ABC", 4);

    /* Rows without definitions loaded: found, with the table's stride. */
    _putenv("BB_PARAMDEFS=");
    CHECK(bb_param_row("TestParam", 20, &bytes) == r20 && bytes == 16);
    CHECK(get("TestParam", 20, "hp") == NULL);       /* no definitions: no fields */
    bb_params_reset();

    char env[MAX_PATH + 16];
    snprintf(env, sizeof env, "BB_PARAMDEFS=%s", defs);
    _putenv(env);
    CHECK(bb_params_load(NULL) == 2);

    /* Reads. */
    CHECK(!strcmp(get("TestParam", 20, "hp"), "-5"));
    CHECK(!strcmp(get("TestParam", 20, "speed"), "1.5"));
    CHECK(!strcmp(get("TestParam", 20, "small"), "-3"));
    CHECK(!strcmp(get("TestParam", 20, "flagA"), "1"));
    CHECK(!strcmp(get("TestParam", 20, "flagB"), "6"));
    CHECK(!strcmp(get("TestParam", 20, "pad[1]"), "9"));
    CHECK(!strcmp(get("TestParam", 20, "pad"), "7"));    /* no index: element 0 */
    CHECK(!strcmp(get("TestParam", 20, "tag"), "ABC"));
    CHECK(get("TestParam", 20, "pad[2]") == NULL);       /* past the array */
    CHECK(get("TestParam", 20, "pad[x]") == NULL);
    CHECK(get("TestParam", 20, "mana") == NULL);         /* no such field */
    CHECK(get("TestParam", 25, "hp") == NULL);           /* no such row */
    CHECK(get("NoParam", 20, "hp") == NULL);             /* no such table */
    CHECK(get("TestParam", 10, "hp") && get("TestParam", 30, "hp"));  /* first and last rows */

    /* Writes. */
    CHECK(bb_param_set("TestParam", 20, "hp", "1234") == 0 && !strcmp(get("TestParam", 20, "hp"), "1234"));
    CHECK(bb_param_set("TestParam", 20, "speed", "0.25") == 0 && !strcmp(get("TestParam", 20, "speed"), "0.25"));
    CHECK(bb_param_set("TestParam", 20, "flagB", "5") == 0);
    CHECK(!strcmp(get("TestParam", 20, "flagB"), "5") && !strcmp(get("TestParam", 20, "flagA"), "1"));  /* A kept */
    CHECK(r20[9] == (1 | (5 << 1)));
    CHECK(bb_param_set("TestParam", 20, "flagB", "8") == 1);    /* 3 bits: 0..7 */
    CHECK(bb_param_set("TestParam", 20, "small", "200") == 1);  /* s8: -128..127 */
    CHECK(bb_param_set("TestParam", 20, "small", "-128") == 0 && r20[8] == 0x80);
    CHECK(bb_param_set("TestParam", 20, "hp", "abc") == 1);
    CHECK(bb_param_set("TestParam", 20, "hp", "") == 1);
    CHECK(bb_param_set("TestParam", 20, "speed", "1e40") == 1);
    CHECK(bb_param_set("TestParam", 20, "pad[1]", "0x20") == 0 && r20[11] == 0x20 && r20[10] == 7);
    CHECK(bb_param_set("TestParam", 20, "pad[1]", "256") == 1);
    /* A fixed string is read-only, even given text that parses as a number. */
    CHECK(bb_param_set("TestParam", 20, "tag", "65") == 1 && !strcmp(get("TestParam", 20, "tag"), "ABC"));

    /* A definition whose rows are 20 bytes against a 16-byte table: fields refused, rows found. */
    CHECK(get("BadParam", 20, "hp") == NULL);
    CHECK(bb_param_set("BadParam", 20, "hp", "1") == 1);
    CHECK(bb_param_row("BadParam", 20, &bytes) == bad + 0x110 && bytes == 16);

    /* The short, inline-named table of one row: no stride to compare, so the definition is used. */
    CHECK(bb_param_row("Short", 10, &bytes) == shortf + 0x100 && bytes == 0);
    CHECK(get("Short", 10, "hp") != NULL);

    /* Ids. */
    uint32_t got[8] = {0};
    CHECK(bb_param_ids("TestParam", got, 8, &count) == 0 && count == 3 && got[0] == 10 && got[1] == 20 && got[2] == 30);
    CHECK(bb_param_ids("TestParam", got, 2, &count) == 0 && count == 3);  /* max 2 written, count still 3 */
    CHECK(bb_param_ids("TestParam", NULL, 0, &count) == 0 && count == 3);
    CHECK(bb_param_ids("NoParam", got, 8, &count) == 1 && count == 0);

    DeleteFileA(defs);
    printf("params: %d checks passed\n", checks);
    return 0;
}
