/* SPDX-License-Identifier: GPL-3.0-or-later
 * The game's state for plugins (plugin_game.h). Layouts and addresses after droogie's bbhost
 * (GPL-3.0-or-later): include/bbhost/engine/sprj/event_flag.hpp, src/engine/event_flags.cpp,
 * player_data.cpp and world_chr.cpp; the port's src/rebirth.c uses the same singletons.
 *
 * Every read of the game's memory goes through bb_read, which does not fault on an unmapped
 * address: a plugin may ask before the world exists, or while a load replaces it. */
#include "plugin_game.h"

#include "bbhost_plugin.h"
#include "engine_hooks.h"
#include "plugin_hooks.h"

#include <string.h>
#include <windows.h>

enum {
    EVENT_FLAG_MAN = 0x593b100,  /* SprjEventFlagMan*: its FD4VirtualMemoryFlag is at +0 */
    WRITE_BIT = 0x17cfcc0,       /* SprjEventFlagMan::WriteBit(man, id, on) */
    GAME_DATA_MAN = 0x593b130,   /* GameDataMan*; the local player's record at +0x08 */
    WORLD_CHR_MAN = 0x593e878,   /* WorldChrMan*; the main player ChrIns at +0x60 */
    /* The loading screen, as bbhost reads it (droogie/bbhost src/engine/loading.cpp, GPL-3.0):
     * a "now loading" request byte, and CSNowLoadingHelper*, whose request bytes at +0x50 (this
     * frame) and +0x51 (last frame) are set while the game itself asks for one in play. */
    NOW_LOADING_REQUESTED = 0x596286b,
    NOW_LOADING_HELPER = 0x593e8b8,
};

/* FD4VirtualMemoryFlag: bits per block +0x1c, block stride +0x20, block storage +0x28, block
 * tree +0x38 (its head node; the root at head+0x08). A tree node: left +0x00, right +0x10,
 * sentinel byte +0x19, block index +0x20, storage kind +0x28 (1: index into the storage, 2: a
 * pointer), storage index or pointer +0x30. */
enum { F_BITS = 0x1c, F_STRIDE = 0x20, F_STORAGE = 0x28, F_TREE = 0x38 };
enum { N_LEFT = 0x00, N_ROOT = 0x08, N_RIGHT = 0x10, N_SENTINEL = 0x19, N_INDEX = 0x20, N_KIND = 0x28, N_DATA = 0x30 };

static uint64_t bn_slot(uint64_t bn) {
    unsigned char *image = bb_guest(0);
    const size_t size = bb_image_size();
    uint64_t value = 0;
    if (!image || bn < 0x400000 || bn - 0x400000 + 8 > size) return 0;
    return bb_read((uint64_t)(uintptr_t)(image + (bn - 0x400000)), &value, 8) ? value : 0;
}

static int rd(uint64_t at, void *out, size_t n) { return at && bb_read(at, out, n); }

/* The byte holding a flag and its bit's mask, or 0 when its block does not exist. */
static uint64_t flag_byte(uint32_t id, unsigned char *mask) {
    const uint64_t man = bn_slot(EVENT_FLAG_MAN);
    uint32_t bits = 0, stride = 0;
    uint64_t storage = 0, head = 0, cursor = 0;
    if (!rd(man + F_BITS, &bits, 4) || !bits || !rd(man + F_STRIDE, &stride, 4) || !rd(man + F_STORAGE, &storage, 8) ||
        !rd(man + F_TREE, &head, 8) || !rd(head + N_ROOT, &cursor, 8))
        return 0;
    const uint32_t block = id / bits, bit = id - block * bits;
    uint64_t candidate = 0;
    uint32_t candidate_index = 0;
    /* The lowest node whose index is >= block; bounded, in case the tree is mid-update. */
    for (int depth = 0; cursor && depth < 64; ++depth) {
        unsigned char sentinel = 1;
        uint32_t index = 0;
        if (!rd(cursor + N_SENTINEL, &sentinel, 1) || sentinel || !rd(cursor + N_INDEX, &index, 4)) break;
        if (block <= index) {
            candidate = cursor;
            candidate_index = index;
            if (!rd(cursor + N_LEFT, &cursor, 8)) return 0;
        } else if (!rd(cursor + N_RIGHT, &cursor, 8)) {
            return 0;
        }
    }
    if (!candidate || candidate_index != block) return 0;
    int32_t kind = 0;
    uint64_t where = 0;
    if (!rd(candidate + N_KIND, &kind, 4) || !rd(candidate + N_DATA, &where, 8)) return 0;
    uint64_t data = 0;
    if (kind == 1) {
        const uint64_t offset = (uint64_t)stride * (uint32_t)where;
        if (offset > 0xffffffffu || !storage) return 0;
        data = storage + offset;
    } else if (kind == 2) {
        data = where;
    } else {
        return 0;
    }
    *mask = (unsigned char)(1u << (~bit & 7));
    return data ? data + (bit >> 3) : 0;
}

int bb_game_event_flag_get(uint32_t id, int *value) {
    unsigned char mask = 0, byte = 0;
    const uint64_t at = flag_byte(id, &mask);
    if (!at || !bb_read(at, &byte, 1)) return 1;
    if (value) *value = (byte & mask) != 0;
    return 0;
}

/* --- the watcher's queue ------------------------------------------------------------------------ */

enum { QUEUE = 1024 };
static BbFlagChange g_queue[QUEUE];
static size_t g_head, g_count;
static uint64_t g_lost;
static SRWLOCK g_queue_lock = SRWLOCK_INIT;
static int g_watching;

static void queue_change(uint32_t id, int value) {
    AcquireSRWLockExclusive(&g_queue_lock);
    if (g_count == QUEUE) {
        ++g_lost;
    } else {
        g_queue[(g_head + g_count++) % QUEUE] = (BbFlagChange){id, value};
    }
    ReleaseSRWLockExclusive(&g_queue_lock);
}

size_t bb_game_take_flag_changes(BbFlagChange *out, size_t max, uint64_t *lost) {
    AcquireSRWLockExclusive(&g_queue_lock);
    size_t n = g_count < max ? g_count : max;
    for (size_t i = 0; i < n; ++i) out[i] = g_queue[(g_head + i) % QUEUE];
    g_head = (g_head + n) % QUEUE;
    g_count -= n;
    if (lost) *lost = g_lost;
    ReleaseSRWLockExclusive(&g_queue_lock);
    return n;
}

size_t bb_game_pending_flag_changes(void) {
    AcquireSRWLockShared(&g_queue_lock);
    const size_t n = g_count;
    ReleaseSRWLockShared(&g_queue_lock);
    return n;
}

int bb_game_event_flag_set(uint32_t id, int value) {
    unsigned char mask = 0, was = 0;
    const uint64_t at = flag_byte(id, &mask);
    if (!at || !bb_read(at, &was, 1)) return 1;
    const unsigned char now = value ? (unsigned char)(was | mask) : (unsigned char)(was & ~mask);
    if (now != was) {
        *(volatile unsigned char *)(uintptr_t)at = now;
        if (g_watching) queue_change(id, value != 0);
    }
    return 0;
}

/* The game's own writes: (man, id, on) at the setter's entry, before it writes. */
static int on_write_bit(BbHookCtx *ctx, void *user) {
    (void)user;
    const uint32_t id = (uint32_t)ctx->arg[1];
    const int on = (uint32_t)ctx->arg[2] != 0;
    int was = 0;
    if (!bb_game_event_flag_get(id, &was) && was != on) queue_change(id, on);
    return 0;
}

int bb_game_watch_flags(void) {
    if (g_watching) return 0;
    /* mov r8d, edx; mov ecx, [rdi+0x1c]; xor edx, edx: whole, none rip-relative */
    static const unsigned char prologue[8] = {0x41, 0x89, 0xd0, 0x8b, 0x4f, 0x1c, 0x31, 0xd2};
    const int result = bb_plugin_hook(WRITE_BIT - 0x400000, prologue, sizeof prologue, NULL, on_write_bit, NULL, -1);
    if (!result) g_watching = 1;
    return result;
}

int bb_game_watching_flags(void) { return g_watching; }

/* --- the player ----------------------------------------------------------------------------------- */

typedef struct {
    const char *name;
    int manager;  /* GameDataMan's own field, else the local player's record */
    uint32_t offset;
    int is_signed;
} Stat;

/* bbhost's src/engine/player_data.cpp: the record (GameDataManPlayerRecord, 32-bit) and the
 * manager's counters. */
static const Stat STATS[] = {
    {"hp", 0, 0x14, 1},          {"max_hp", 0, 0x18, 1},     {"stamina", 0, 0x30, 1},
    {"max_stamina", 0, 0x34, 1}, {"vitality", 0, 0x40, 1},   {"endurance", 0, 0x48, 1},
    {"strength", 0, 0x58, 1},    {"skill", 0, 0x60, 1},      {"bloodtinge", 0, 0x68, 1},
    {"arcane", 0, 0x70, 1},      {"insight", 0, 0x84, 1},    {"level", 0, 0x90, 1},
    {"echoes", 0, 0x94, 1},      {"ng_cycle", 1, 0x68, 1},   {"coop_helps", 1, 0x78, 0},
    {"invader_kills", 1, 0x7c, 0}, {"true_deaths", 1, 0x84, 0}, {"deaths", 1, 0x88, 0},
    {"play_time_ms", 1, 0x94, 0},
};

static uint64_t stat_address(const char *name) {
    const Stat *s = NULL;
    for (size_t i = 0; name && i < sizeof STATS / sizeof *STATS; ++i)
        if (!strcmp(STATS[i].name, name)) s = &STATS[i];
    const uint64_t man = s ? bn_slot(GAME_DATA_MAN) : 0;
    uint64_t record = 0;
    if (!man || !rd(man + 8, &record, 8) || !record) return 0;  /* no character: neither counts */
    return (s->manager ? man : record) + s->offset;
}

static int stat_signed(const char *name) {
    for (size_t i = 0; i < sizeof STATS / sizeof *STATS; ++i)
        if (!strcmp(STATS[i].name, name)) return STATS[i].is_signed;
    return 1;
}

int bb_game_player_stat_get(const char *name, int64_t *value) {
    const uint64_t at = stat_address(name);
    uint32_t v = 0;
    if (!at || !bb_read(at, &v, 4)) return 1;
    if (value) *value = stat_signed(name) ? (int64_t)(int32_t)v : (int64_t)v;
    return 0;
}

int bb_game_player_stat_set(const char *name, int64_t value) {
    const uint64_t at = stat_address(name);
    uint32_t check = 0;
    if (!at || !bb_read(at, &check, 4)) return 1;
    const uint32_t v = (uint32_t)value;
    memcpy((void *)(uintptr_t)at, &v, 4);
    return 0;
}

static uint64_t main_player(void) {
    const uint64_t man = bn_slot(WORLD_CHR_MAN);
    uint64_t chr = 0;
    return man && rd(man + 0x60, &chr, 8) ? chr : 0;
}

int bb_game_player_position(float xyz[3], float *yaw) {
    const uint64_t chr = main_player();
    uint64_t modules = 0, transform = 0;
    float v[3] = {0}, y = 0;
    if (!chr || !rd(chr + 0x3b0, &modules, 8) || !rd(modules + 0x68, &transform, 8) || !rd(transform + 0x1e0, v, 12) ||
        !rd(transform + 0x1d4, &y, 4))
        return 1;
    for (int i = 0; i < 3; ++i)
        if (v[i] != v[i] || v[i] > 1e7f || v[i] < -1e7f) return 1;  /* NaN or garbage: not a placed player */
    if (xyz) memcpy(xyz, v, sizeof v);
    if (yaw) *yaw = y;
    return 0;
}

uint64_t bb_game_main_player(void) { return main_player(); }

int bb_game_loading_screen(void) {
    unsigned char *image = bb_guest(0);
    uint8_t requested = 0, helper_bytes[2] = {0, 0};
    if (!image || NOW_LOADING_REQUESTED - 0x400000 + 1 > bb_image_size() ||
        !bb_read((uint64_t)(uintptr_t)(image + (NOW_LOADING_REQUESTED - 0x400000)), &requested, 1))
        return 0;
    const uint64_t helper = bn_slot(NOW_LOADING_HELPER);
    const int in_game = helper && rd(helper + 0x50, helper_bytes, 2) && (helper_bytes[0] || helper_bytes[1]);
    return requested && !in_game;
}

int bb_game_player_block(uint32_t *block) {
    const uint64_t chr = main_player();
    uint32_t b = 0;
    if (!chr || !rd(chr + 0x3f8, &b, 4) || b == 0xffffffffu) return 1;
    if (block) *block = b;
    return 0;
}

/* --- characters and SpEffects --------------------------------------------------------------------- */

static uint64_t rp(uint64_t at) {
    uint64_t v = 0;
    return rd(at, &v, 8) ? v : 0;
}

/* One character's numbers (bbhost's fill_chr, src/host/plugins.cpp): handle +0x08, chr type
 * +0x78, team +0x88; an NPC's NpcParam id behind +0x3c8; from the modules (+0x3b0): data +0x20
 * (hp +0xf8, max hp +0xfc, the model's "c2560" wstring at +0x158, length +0x168, capacity
 * +0x170) and physics +0x68 (position +0x1e0). */
static int fill_chr(uint64_t ins, uint64_t player_vtable, BbChr *c) {
    uint64_t vt = 0;
    if (!rd(ins, &vt, 8) || !vt) return 0;
    memset(c, 0, sizeof *c);
    c->ins = (void *)(uintptr_t)ins;
    rd(ins + 0x08, &c->handle, 4);
    rd(ins + 0x78, &c->chr_type, 4);
    rd(ins + 0x88, &c->team_type, 4);
    c->npc_param = -1;
    if (vt != player_vtable) {
        const uint64_t ref = rp(ins + 0x3c8);
        int32_t id = -1;
        if (ref && rd(ref, &id, 4)) c->npc_param = id;
    }
    const uint64_t modules = rp(ins + 0x3b0);
    const uint64_t data = modules ? rp(modules + 0x20) : 0, phys = modules ? rp(modules + 0x68) : 0;
    if (data) {
        rd(data + 0xf8, &c->hp, 4);
        rd(data + 0xfc, &c->max_hp, 4);
        uint16_t w[16] = {0};
        uint64_t len = 0, cap = 0;
        if (rd(data + 0x168, &len, 8) && rd(data + 0x170, &cap, 8) && len && len <= 16 &&
            rd(cap <= 7 ? data + 0x158 : rp(data + 0x158), w, (size_t)len * 2) && w[0] == 'c') {
            for (uint64_t i = 1; i < len && w[i] >= '0' && w[i] <= '9'; ++i) c->model = c->model * 10 + (uint32_t)(w[i] - '0');
        }
    }
    if (phys) rd(phys + 0x1e0, c->pos, 12);
    return 1;
}

enum { MAX_SEEN = 4096 };

/* Calls each character (the main player first): fn returns nonzero to stop. With `dedup`, each
 * once: that keeps a list of those seen, which is static (too big for a game thread's stack), so
 * only the main thread asks for it (chr_list). Without, a character in two sets comes twice:
 * enough for "is it there", and safe from any thread. port-a87a447-F1. */
static int each_chr(int (*fn)(uint64_t ins, int is_main, void *user), void *user, int dedup) {
    const uint64_t man = bn_slot(WORLD_CHR_MAN);
    const uint64_t main = man ? rp(man + 0x60) : 0;
    if (!main) return 1;
    static uint64_t seen[MAX_SEEN];
    size_t nseen = 0;
    if (dedup) seen[nseen++] = main;
    if (fn(main, 1, user)) return 0;
    /* The handle-indexed sets (WorldChrMan+0x850, 0x3e ChrSet pointers): capacity +0x00,
     * entries +0x08, 0x38 bytes apart. */
    for (int i = 0; i < 0x3e; ++i) {
        const uint64_t set = rp(man + 0x850 + 8u * (unsigned)i);
        uint32_t cap = 0;
        const uint64_t entries = set ? rp(set + 0x08) : 0;
        if (!entries || !rd(set, &cap, 4) || cap > 4096) continue;
        for (uint32_t k = 0; k < cap; ++k) {
            const uint64_t ins = rp(entries + 0x38u * k);
            if (!ins) continue;
            if (dedup) {
                int dup = 0;
                for (size_t s = 0; s < nseen && !dup; ++s) dup = seen[s] == ins;
                if (dup || nseen == MAX_SEEN) continue;
                seen[nseen++] = ins;
            }
            if (fn(ins, 0, user)) return 0;
        }
    }
    return 0;
}

typedef struct {
    BbChr *out;
    size_t max, n;
    uint64_t player_vtable;
} ListCtx;

static int list_one(uint64_t ins, int is_main, void *user) {
    ListCtx *l = user;
    BbChr c;
    if (!fill_chr(ins, l->player_vtable, &c)) return 0;
    c.is_player = is_main;
    if (l->out && l->n < l->max) l->out[l->n] = c;
    ++l->n;
    return 0;
}

int bb_game_chr_list(BbChr *out, size_t max, size_t *count) {
    const uint64_t man = bn_slot(WORLD_CHR_MAN);
    const uint64_t main = man ? rp(man + 0x60) : 0;
    ListCtx l = {out, max, 0, main ? rp(main) : 0};
    if (count) *count = 0;
    if (!main || each_chr(list_one, &l, 1)) return 1;
    if (count) *count = l.n;
    return 0;
}

typedef struct {
    uint64_t want;
    int found;
} FindCtx;

static int find_one(uint64_t ins, int is_main, void *user) {
    (void)is_main;
    FindCtx *f = user;
    f->found = ins == f->want;
    return f->found;
}

static uint64_t chr_or_player(void *chr) {
    const uint64_t man = bn_slot(WORLD_CHR_MAN);
    const uint64_t main = man ? rp(man + 0x60) : 0;
    return chr ? (uint64_t)(uintptr_t)chr : main;
}

int bb_game_chr_valid(void *chr) {
    const uint64_t ins = chr_or_player(chr);
    FindCtx f = {ins, 0};
    return ins && !each_chr(find_one, &f, 0) && f.found;
}

int bb_game_sp_effect_apply(void *chr, int32_t id) {
    if (!bb_game_chr_valid(chr)) return 1;
    const uint64_t ins = chr_or_player(chr);
    const uint64_t vt = rp(ins);
    const uint64_t fn = vt ? rp(vt + 0x3f0) : 0;
    if (!fn) return 1;
    /* As Lua_MultiDoping (BN 0x178bc70) calls it: target, effect, source = the target, then
     * zeros; five 1.0 multipliers in xmm0-4; a zero on the stack (bbhost's api_sp_effect_apply). */
    BbCallRegs r;
    memset(&r, 0, sizeof r);
    r.arg[0] = ins;
    r.arg[1] = (uint32_t)id;
    r.arg[2] = ins;
    for (int i = 0; i < 5; ++i) r.xmm[i] = 0x3f800000u;
    bb_plugin_call((void *)(uintptr_t)fn, &r);
    return 0;
}

int bb_game_sp_effect_has(void *chr, int32_t id) {
    const uint64_t ins = chr_or_player(chr);
    if (!ins || (chr && !bb_game_chr_valid(chr))) return 0;
    /* The SpEffect list (ChrIns+0x1c8): its first node at +0x08; a node's id +0x40, next +0x58. */
    const uint64_t list = rp(ins + 0x1c8);
    uint64_t node = list ? rp(list + 0x08) : 0;
    for (int guard = 0; node && guard < 1024; ++guard) {
        int32_t have = 0;
        if (rd(node + 0x40, &have, 4) && have == id) return 1;
        node = rp(node + 0x58);
    }
    return 0;
}

void bb_game_reset(void) {
    AcquireSRWLockExclusive(&g_queue_lock);
    g_head = g_count = 0;
    g_lost = 0;
    ReleaseSRWLockExclusive(&g_queue_lock);
    g_watching = 0;
}
