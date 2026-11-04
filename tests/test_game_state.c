/* SPDX-License-Identifier: GPL-3.0-or-later
 * plugin_game.c against fake game structures, at the real 1.09 addresses of a fake image:
 * - event flags read through the flag store's block tree (a block in shared storage, one behind
 *   a pointer, a missing one), with the game's bit order;
 * - set, and the change reported once;
 * - the watcher hook on the game's setter (a fake with the real prologue) queues only real
 *   changes;
 * - the player's stats by name (signed and unsigned, record and manager);
 * - position, facing and block;
 * - every "not there yet" case answering 1. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "engine_hooks.h"
#include "plugin_game.h"

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
static void put32(unsigned char *at, uint32_t v) { memcpy(at, &v, 4); }
static void putf(unsigned char *at, float v) { memcpy(at, &v, 4); }

/* A tree node (FD4VirtualMemoryFlagBlockNode, 0x38 bytes). */
static unsigned char *node(unsigned char *left, unsigned char *right, int sentinel, uint32_t index, int32_t kind,
                           uint64_t data) {
    unsigned char *n = calloc(1, 0x38);
    put64(n + 0x00, (uint64_t)(uintptr_t)left);
    put64(n + 0x10, (uint64_t)(uintptr_t)right);
    n[0x19] = (unsigned char)sentinel;
    put32(n + 0x20, index);
    memcpy(n + 0x28, &kind, 4);
    put64(n + 0x30, data);
    return n;
}

typedef void (BB_GUEST_ABI *WriteBit)(void *, uint32_t, uint32_t);

int main(void) {
    unsigned char *image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    assert(image);
    CHECK(bb_hooks_init(image, IMAGE_SIZE) == 0);

    /* Nothing loaded yet: every reader answers 1. */
    int v = -1;
    int64_t s = -1;
    float xyz[3];
    uint32_t block = 0;
    CHECK(bb_game_event_flag_get(1000, &v) == 1);
    CHECK(bb_game_event_flag_set(1000, 1) == 1);
    CHECK(bb_game_player_stat_get("level", &s) == 1);
    CHECK(bb_game_player_position(xyz, NULL) == 1);
    CHECK(bb_game_player_block(&block) == 1);

    /* The flag store: 1000 bits a block, 128-byte stride. Block 0 is index 3 in the shared
     * storage; block 5 is behind its own pointer; block 2 does not exist. */
    unsigned char *storage = calloc(8, 128), *own = calloc(1, 128);
    unsigned char *sentinel = node(NULL, NULL, 1, 0, 0, 0);
    unsigned char *b5 = node(sentinel, sentinel, 0, 5, 2, (uint64_t)(uintptr_t)own);
    unsigned char *b0 = node(sentinel, sentinel, 0, 0, 1, 3);
    unsigned char *root = node(b0, b5, 0, 1, 1, 7);  /* block 1, a third block, as the root */
    unsigned char *head = node(NULL, NULL, 1, 0, 0, 0);
    put64(head + 0x08, (uint64_t)(uintptr_t)root);
    unsigned char *man = calloc(1, 0x88);
    put32(man + 0x1c, 1000);
    put32(man + 0x20, 128);
    put64(man + 0x28, (uint64_t)(uintptr_t)storage);
    put64(man + 0x38, (uint64_t)(uintptr_t)head);
    put64(AT(0x593b100), (uint64_t)(uintptr_t)man);

    /* Flag 7: block 0, bit 7 -> byte 0, mask 1 << (~7 & 7) = 0x01 (the game's MSB-first order). */
    storage[3 * 128 + 0] = 0x01;
    CHECK(bb_game_event_flag_get(7, &v) == 0 && v == 1);
    CHECK(bb_game_event_flag_get(0, &v) == 0 && v == 0);   /* bit 0 -> mask 0x80, clear */
    storage[3 * 128 + 0] |= 0x80;
    CHECK(bb_game_event_flag_get(0, &v) == 0 && v == 1);
    /* Flag 5010: block 5 (own pointer), bit 10 -> byte 1, mask 1 << (~10 & 7) = 0x20. */
    own[1] = 0x20;
    CHECK(bb_game_event_flag_get(5010, &v) == 0 && v == 1);
    CHECK(bb_game_event_flag_get(5011, &v) == 0 && v == 0);
    /* Flag 1999: block 1 (the root), index 7 in the storage, bit 999 -> byte 124, mask 0x01. */
    storage[7 * 128 + 124] = 0x01;
    CHECK(bb_game_event_flag_get(1999, &v) == 0 && v == 1);
    CHECK(bb_game_event_flag_get(2500, &v) == 1);           /* block 2: missing */
    CHECK(bb_game_event_flag_get(9000, &v) == 1);           /* block 9: past every node */

    /* Set: the bit changes; without a watcher nothing is queued. */
    BbFlagChange changes[8];
    uint64_t lost = 0;
    CHECK(bb_game_event_flag_set(5011, 1) == 0 && (own[1] & 0x10));
    CHECK(bb_game_take_flag_changes(changes, 8, &lost) == 0);

    /* The watcher: a fake setter at the real address, with the real 8-byte prologue, then ret. */
    static const unsigned char setter[] = {0x41, 0x89, 0xd0, 0x8b, 0x4f, 0x1c, 0x31, 0xd2, 0xc3};
    memcpy(AT(0x17cfcc0), setter, sizeof setter);
    CHECK(bb_game_watch_flags() == 0 && bb_game_watching_flags());
    CHECK(bb_game_watch_flags() == 0);                      /* again: already watching */
    bb_hooks_seal();
    WriteBit write_bit = (WriteBit)(void *)AT(0x17cfcc0);
    write_bit(man, 7, 0);      /* 7 is on: a change to 0 */
    write_bit(man, 5010, 1);   /* already on: not a change */
    write_bit(man, 2500, 1);   /* no block: not reported */
    CHECK(bb_game_event_flag_set(1999, 0) == 0);  /* a plugin's own write: reported */
    CHECK(bb_game_event_flag_set(1999, 0) == 0);  /* the same again: not a change */
    size_t n = bb_game_take_flag_changes(changes, 8, &lost);
    CHECK(n == 2 && lost == 0);
    CHECK(changes[0].id == 7 && changes[0].value == 0);
    CHECK(changes[1].id == 1999 && changes[1].value == 0);
    CHECK(bb_game_take_flag_changes(changes, 8, &lost) == 0);
    /* Overflow: 1024 fit, the rest are counted lost. */
    for (int i = 1; i <= 1030; ++i) bb_game_event_flag_set(5000, i % 2);  /* 0 -> 1 -> 0 ...: each a change */
    size_t total = 0;
    while ((n = bb_game_take_flag_changes(changes, 8, &lost)) != 0) total += n;
    CHECK(total == 1024 && lost == 6);

    /* The player: GameDataMan with its record at +8. */
    unsigned char *gdm = calloc(1, 0x100), *record = calloc(1, 0x100);
    put64(gdm + 8, (uint64_t)(uintptr_t)record);
    put64(AT(0x593b130), (uint64_t)(uintptr_t)gdm);
    put32(record + 0x90, 42);                 /* level */
    put32(record + 0x14, (uint32_t)-5);       /* hp, signed */
    put32(gdm + 0x88, 0xfffffff0u);           /* deaths, unsigned, on the manager */
    CHECK(bb_game_player_stat_get("level", &s) == 0 && s == 42);
    CHECK(bb_game_player_stat_get("hp", &s) == 0 && s == -5);
    CHECK(bb_game_player_stat_get("deaths", &s) == 0 && s == 0xfffffff0ll);
    CHECK(bb_game_player_stat_get("charisma", &s) == 1);
    CHECK(bb_game_player_stat_set("echoes", 12345) == 0);
    uint32_t echoes = 0;
    memcpy(&echoes, record + 0x94, 4);
    CHECK(echoes == 12345);
    put64(gdm + 8, 0);                        /* no character: nothing, not even the counters */
    CHECK(bb_game_player_stat_get("deaths", &s) == 1);

    /* Position and block: WorldChrMan -> +0x60 player -> +0x3b0 modules -> +0x68 transform. */
    unsigned char *wcm = calloc(1, 0x100), *chr = calloc(1, 0x400), *modules = calloc(1, 0x100),
                  *transform = calloc(1, 0x200);
    put64(AT(0x593e878), (uint64_t)(uintptr_t)wcm);
    put64(wcm + 0x60, (uint64_t)(uintptr_t)chr);
    put64(chr + 0x3b0, (uint64_t)(uintptr_t)modules);
    put64(modules + 0x68, (uint64_t)(uintptr_t)transform);
    putf(transform + 0x1e0, 1.5f);
    putf(transform + 0x1e4, -2.0f);
    putf(transform + 0x1e8, 300.25f);
    putf(transform + 0x1d4, 3.0f);
    put32(chr + 0x3f8, 0x18010000);
    float yaw = 0;
    CHECK(bb_game_player_position(xyz, &yaw) == 0 && xyz[0] == 1.5f && xyz[1] == -2.0f && xyz[2] == 300.25f && yaw == 3.0f);
    CHECK(bb_game_player_block(&block) == 0 && block == 0x18010000);
    put32(chr + 0x3f8, 0xffffffffu);
    CHECK(bb_game_player_block(&block) == 1);   /* between maps */
    putf(transform + 0x1e4, 0.0f / 0.0f);
    CHECK(bb_game_player_position(xyz, &yaw) == 1);  /* NaN: not a placed player */

    printf("game state: %d checks passed\n", checks);
    return 0;
}
