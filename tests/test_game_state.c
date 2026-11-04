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

#include "bbhost_plugin.h"
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

/* port-a87a447-F1: sp_effect_has on another thread while chr_list runs on this one. */
static volatile LONG race_stop;
static volatile LONG race_wrong;
static void *race_npc;
static DWORD WINAPI race_has(LPVOID unused) {
    (void)unused;
    while (!race_stop)
        if (bb_game_sp_effect_has(race_npc, 2200) != 1) InterlockedIncrement(&race_wrong);
    return 0;
}

/* The fake ChrIns vtable's +0x3f0 (the game's apply-SpEffect): records how it was called. */
static uint64_t applied_ins, applied_id, applied_source;
static float applied_scale;
static BB_GUEST_ABI void fake_apply(uint64_t ins, uint64_t id, uint64_t source, uint64_t a3, uint64_t a4, uint64_t a5,
                                    float scale) {
    (void)a3, (void)a4, (void)a5;
    applied_ins = ins;
    applied_id = id;
    applied_source = source;
    applied_scale = scale;
}

/* A ChrIns: vtable, handle; modules (+0x3b0) with data (+0x20: hp, max hp, the model wstring)
 * and physics (+0x68: position). */
static unsigned char *chr_ins(unsigned char *vtable, uint32_t handle, int32_t hp, const char *model, float x) {
    unsigned char *c = calloc(1, 0x400), *modules = calloc(1, 0x100), *data = calloc(1, 0x200), *phys = calloc(1, 0x200);
    put64(c, (uint64_t)(uintptr_t)vtable);
    put32(c + 0x08, handle);
    put64(c + 0x3b0, (uint64_t)(uintptr_t)modules);
    put64(modules + 0x20, (uint64_t)(uintptr_t)data);
    put64(modules + 0x68, (uint64_t)(uintptr_t)phys);
    put32(data + 0xf8, (uint32_t)hp);
    put32(data + 0xfc, 1000);
    const size_t len = strlen(model);
    for (size_t i = 0; i < len; ++i) data[0x158 + 2 * i] = (unsigned char)model[i];  /* inline, capacity 7 */
    put64(data + 0x168, len);
    put64(data + 0x170, 7);
    putf(phys + 0x1e0, x);
    return c;
}

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

    /* Characters: the main player (wcm+0x60, from above) and two sets at +0x850: an NPC in both
     * (listed once), an empty slot, the player again (skipped). */
    unsigned char *player_vt = calloc(1, 0x400), *npc_vt = calloc(1, 0x400);
    put64(player_vt + 0x3f0, (uint64_t)(uintptr_t)fake_apply);
    put64(npc_vt + 0x3f0, (uint64_t)(uintptr_t)fake_apply);
    put64(chr, (uint64_t)(uintptr_t)player_vt);
    unsigned char *npc = chr_ins(npc_vt, 0x10000123, 250, "c2560", 7.5f);
    unsigned char *npc_ref = calloc(1, 16);
    put32(npc_ref, 256000);
    put64(npc + 0x3c8, (uint64_t)(uintptr_t)npc_ref);
    unsigned char *set1 = calloc(1, 16), *set2 = calloc(1, 16), *entries1 = calloc(3, 0x38), *entries2 = calloc(2, 0x38);
    put32(set1, 3);
    put64(set1 + 8, (uint64_t)(uintptr_t)entries1);
    put64(entries1 + 0x38, (uint64_t)(uintptr_t)npc);
    put64(entries1 + 0x70, (uint64_t)(uintptr_t)chr);
    put32(set2, 2);
    put64(set2 + 8, (uint64_t)(uintptr_t)entries2);
    put64(entries2, (uint64_t)(uintptr_t)npc);
    put64(wcm + 0x850 + 8 * 2, (uint64_t)(uintptr_t)set1);
    put64(wcm + 0x850 + 8 * 9, (uint64_t)(uintptr_t)set2);
    BbChr list[8];
    size_t n_chr = 0;
    CHECK(bb_game_chr_list(list, 8, &n_chr) == 0 && n_chr == 2);
    CHECK(list[0].ins == chr && list[0].is_player == 1 && list[0].npc_param == -1);
    CHECK(list[1].ins == npc && list[1].is_player == 0 && list[1].npc_param == 256000);
    CHECK(list[1].handle == 0x10000123 && list[1].hp == 250 && list[1].max_hp == 1000 && list[1].model == 2560);
    CHECK(list[1].pos[0] == 7.5f);
    CHECK(bb_game_chr_list(NULL, 0, &n_chr) == 0 && n_chr == 2);   /* count only */
    CHECK(bb_game_chr_list(list, 1, &n_chr) == 0 && n_chr == 2);   /* one written, count still 2 */

    /* SpEffects: has, through the list at +0x1c8 (first node +0x08; id +0x40, next +0x58). */
    unsigned char *list_head = calloc(1, 16), *node1 = calloc(1, 0x60), *node2 = calloc(1, 0x60);
    put64(npc + 0x1c8, (uint64_t)(uintptr_t)list_head);
    put64(list_head + 8, (uint64_t)(uintptr_t)node1);
    put32(node1 + 0x40, 2200);
    put64(node1 + 0x58, (uint64_t)(uintptr_t)node2);
    put32(node2 + 0x40, 3010);
    CHECK(bb_game_sp_effect_has(npc, 3010) == 1 && bb_game_sp_effect_has(npc, 2200) == 1);
    CHECK(bb_game_sp_effect_has(npc, 1234) == 0);
    CHECK(bb_game_sp_effect_has(NULL, 2200) == 0);                 /* the player has no list */
    unsigned char *stranger = chr_ins(npc_vt, 1, 1, "c1000", 0);
    put64(stranger + 0x1c8, (uint64_t)(uintptr_t)list_head);
    CHECK(bb_game_sp_effect_has(stranger, 2200) == 0);             /* not in the world: not looked at */

    /* Two threads: has on a worker, chr_list here (a smoke test). In a world that holds still
     * both walks write the same pointers in the same order, so the old shared dedup list could
     * not be caught here; the fix is structural: has uses no shared state, and chr_list is
     * main-thread only (flag-delivery-test checks the refusal). */
    race_npc = npc;
    HANDLE racer = CreateThread(NULL, 0, race_has, NULL, 0, NULL);
    int lists_wrong = 0;
    for (int i = 0; i < 20000; ++i) {
        size_t c = 0;
        if (bb_game_chr_list(list, 8, &c) || c != 2 || list[1].ins != npc) ++lists_wrong;
    }
    InterlockedExchange(&race_stop, 1);
    WaitForSingleObject(racer, INFINITE);
    CloseHandle(racer);
    CHECK(lists_wrong == 0 && race_wrong == 0);

    /* Apply: the vtable's +0x3f0, target = source, 1.0 in xmm0. */
    CHECK(bb_game_chr_valid(NULL) && bb_game_chr_valid(npc) && !bb_game_chr_valid(stranger));
    CHECK(bb_game_sp_effect_apply(npc, 2200) == 0);
    CHECK(applied_ins == (uint64_t)(uintptr_t)npc && applied_id == 2200 && applied_source == applied_ins);
    CHECK(applied_scale == 1.0f);
    CHECK(bb_game_sp_effect_apply(NULL, 3010) == 0 && applied_ins == (uint64_t)(uintptr_t)chr && applied_id == 3010);
    applied_id = 0;
    CHECK(bb_game_sp_effect_apply(stranger, 2200) == 1 && applied_id == 0);  /* never called */
    put64(wcm + 0x60, 0);                                           /* no world */
    CHECK(bb_game_chr_list(list, 8, &n_chr) == 1 && n_chr == 0);
    CHECK(bb_game_sp_effect_apply(NULL, 2200) == 1);

    printf("game state: %d checks passed\n", checks);
    return 0;
}
