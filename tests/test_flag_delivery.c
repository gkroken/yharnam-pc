/* SPDX-License-Identifier: GPL-3.0-or-later
 * The game-state API's rules through real plugins (tests/plugins/flag_plugin.c), on a fake image
 * with a fake flag store, GameDataMan record and flag setter at the real 1.09 addresses:
 * - registering on_event_flag after the start fails while nobody watches (folder E alone);
 * - two plugins forcing one flag opposite ways: each frame delivers one round and returns, the
 *   next frame the next round (port-e230c49-F1);
 * - event_flag_set and player_stat_set refuse off the main thread and work on it (N2). */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "bbhost_plugin.h"
#include "engine_hooks.h"
#include "plugin_game.h"
#include "plugin_host.h"
#include "input_take.h"

#ifndef PLUGIN_DIR_D
#error PLUGIN_DIR_D and PLUGIN_DIR_E name the test plugin folders
#endif

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

static void set_env(const char *name, const char *value) {
    char line[1024];
    snprintf(line, sizeof line, "%s=%s", name, value ? value : "");
    _putenv(line);
}

static void *export_of(const char *dll, const char *name) {
    HMODULE m = GetModuleHandleA(dll);
    return m ? (void *)GetProcAddress(m, name) : NULL;
}

typedef int (*SetFn)(uint32_t, int);
typedef int (*StatFn)(int64_t);
typedef int (*ChrFn)(size_t *);
static SetFn g_set;
static StatFn g_stat;
static ChrFn g_chr;
static int g_off_set = -1, g_off_stat = -1, g_off_chr = -1;

/* The overlay's entry points, as probe.c hands them over: here, recorders. */
static char said[64], placed[64];
static float said_seconds, placed_x, placed_y, placed_scale;
static unsigned placed_align;
static int fake_message(const char *text, float seconds) {
    snprintf(said, sizeof said, "%s", text);
    said_seconds = seconds;
    return 0;
}
static int fake_text(const char *text, float seconds, float x, float y, float scale, unsigned align) {
    (void)seconds;
    snprintf(placed, sizeof placed, "%s", text);
    placed_x = x;
    placed_y = y;
    placed_scale = scale;
    placed_align = align;
    return 0;
}
/* The window's keyboard and mouse, as probe.c hands bbgpu_plugin_input over: here, a recorder
 * that gives one read's worth (the next read finds nothing new). */
static int input_reads;
static int fake_input(uint8_t keys[512], uint32_t *buttons, uint32_t *pressed, float *dx, float *dy, float *wheel,
                      int *playing) {
    ++input_reads;
    memset(keys, 0, 512);
    keys[4] = 1;    /* A */
    keys[511] = 1;  /* the last one */
    *buttons = 2;
    *pressed = input_reads == 1 ? 3 : 0;
    *dx = input_reads == 1 ? 12.5f : 0;
    *dy = -3;
    *wheel = 1;
    *playing = 1;
    return 0;
}
static int fake_input_headless(uint8_t keys[512], uint32_t *buttons, uint32_t *pressed, float *dx, float *dy,
                               float *wheel, int *playing) {
    keys[4] = 1, *buttons = 1, *pressed = 1, *dx = *dy = *wheel = 1, *playing = 1;  /* junk: must not reach the plugin */
    return 1;
}
typedef int (*InputFn)(BbInput *);
static InputFn g_input;
static int g_off_input = -1;

int playing_now(void) { return 1; }

static DWORD WINAPI off_main(LPVOID unused) {
    (void)unused;
    size_t count = 0;
    g_off_set = g_set(7, 1);
    g_off_stat = g_stat(5);
    g_off_chr = g_chr(&count);
    BbInput in;
    g_off_input = g_input ? g_input(&in) : -1;
    return 0;
}

int main(void) {
    char user[MAX_PATH];
    GetTempPathA(MAX_PATH, user);
    snprintf(user + strlen(user), MAX_PATH - strlen(user), "bb-flag-delivery-test-%lu", GetCurrentProcessId());
    CreateDirectoryA(user, NULL);
    set_env("BB_GPU_USER_DIR", user);
    set_env("BB_EBOOT_109", "1");
    set_env("BB_PLUGINS", "");
    set_env("BB_ONLINE", "");

    unsigned char *image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    assert(image);
    CHECK(bb_hooks_init(image, IMAGE_SIZE) == 0);

    /* One flag block (block 0, own pointer) holding flag 7: byte 0, mask 0x01. */
    unsigned char *bits = calloc(1, 128);
    unsigned char *sentinel = calloc(1, 0x38), *root = calloc(1, 0x38), *head = calloc(1, 0x38), *man = calloc(1, 0x88);
    sentinel[0x19] = 1;
    put64(root + 0x00, (uint64_t)(uintptr_t)sentinel);
    put64(root + 0x10, (uint64_t)(uintptr_t)sentinel);
    put32(root + 0x20, 0);
    const int32_t kind = 2;
    memcpy(root + 0x28, &kind, 4);
    put64(root + 0x30, (uint64_t)(uintptr_t)bits);
    put64(head + 0x08, (uint64_t)(uintptr_t)root);
    put32(man + 0x1c, 1000);
    put32(man + 0x20, 128);
    put64(man + 0x38, (uint64_t)(uintptr_t)head);
    put64(AT(0x593b100), (uint64_t)(uintptr_t)man);
    /* The game's setter, with its real prologue (the watcher hooks it), then ret. */
    static const unsigned char setter[] = {0x41, 0x89, 0xd0, 0x8b, 0x4f, 0x1c, 0x31, 0xd2, 0xc3};
    memcpy(AT(0x17cfcc0), setter, sizeof setter);
    /* A loaded character for player_stat_set, and a world with a main player for chr_list. */
    unsigned char *gdm = calloc(1, 0x100), *record = calloc(1, 0x100);
    put64(gdm + 8, (uint64_t)(uintptr_t)record);
    put64(AT(0x593b130), (uint64_t)(uintptr_t)gdm);
    unsigned char *wcm = calloc(1, 0x1000), *player = calloc(1, 0x400), *player_vt = calloc(1, 0x400);
    put64(player, (uint64_t)(uintptr_t)player_vt);
    put64(wcm + 0x60, (uint64_t)(uintptr_t)player);
    put32(player + 0x3f8, 0xffffffffu);  /* between maps until the world-load checks place it */
    put64(AT(0x593e878), (uint64_t)(uintptr_t)wcm);

    /* 1. Folder E alone: nobody registers in bb_plugin_image, so nobody watches. */
    set_env("BB_PLUGIN_DIRS", PLUGIN_DIR_E);
    CHECK(bb_plugins_install() == 1);
    int (*late)(void) = (int (*)(void))export_of("late.dll", "late_register");
    CHECK(late && late() == 1 && !bb_game_watching_flags());
    bb_plugins_reset();

    /* 2. Folders D and E: the two forcers register in bb_plugin_image and the watcher goes in;
     * late registers after the start, which now works because someone watches. */
    set_env("BB_PLUGIN_DIRS", PLUGIN_DIR_D ";" PLUGIN_DIR_E);
    CHECK(bb_plugins_install() == 3);
    CHECK(bb_game_watching_flags());
    late = (int (*)(void))export_of("late.dll", "late_register");
    CHECK(late && late() == 0);
    bb_hooks_seal();
    int (*seen_on)(void) = (int (*)(void))export_of("force_on.dll", "flag_seen");
    int (*seen_off)(void) = (int (*)(void))export_of("force_off.dll", "flag_seen");
    g_set = (SetFn)export_of("force_on.dll", "flag_set");
    g_stat = (StatFn)export_of("force_on.dll", "stat_set");
    g_chr = (ChrFn)export_of("force_on.dll", "chr_count");
    CHECK(seen_on && seen_off && g_set && g_stat && g_chr);
    g_input = (InputFn)export_of("late.dll", "read_input");
    CHECK(g_input);
    bb_plugins_set_input(fake_input);  /* set now: off the main thread it must still refuse */

    /* Off the main thread both sets refuse; on it they work. */
    HANDLE t = CreateThread(NULL, 0, off_main, NULL, 0, NULL);
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
    CHECK(g_off_set == 1 && g_off_stat == 1 && bits[0] == 0);
    CHECK(g_off_chr == 1);                               /* chr_list: main thread only */
    CHECK(g_off_input == 1 && input_reads == 0);         /* input: main thread only, the window untouched */
    size_t chr_count = 0;
    CHECK(g_chr(&chr_count) == 0 && chr_count == 1);      /* on it: the main player */
    uint32_t echoes = 0;
    memcpy(&echoes, record + 0x94, 4);
    CHECK(echoes == 0);
    CHECK(g_stat(77) == 0);
    memcpy(&echoes, record + 0x94, 4);
    CHECK(echoes == 77);

    /* The fight: flag 7 goes on. force_off turns it off, force_on back on, and so on forever. */
    CHECK(g_set(7, 1) == 0 && bits[0] == 0x01);
    bb_plugins_frame();  /* round 1: both hear "7 -> 1"; force_off sets it 0 (queued for next frame) */
    CHECK(seen_on() == 1 && seen_off() == 1);
    CHECK(bits[0] == 0x00);
    bb_plugins_frame();  /* round 2: both hear "7 -> 0"; force_on sets it 1 */
    CHECK(seen_on() == 2 && seen_off() == 2);
    CHECK(bits[0] == 0x01);
    for (int i = 0; i < 10; ++i) bb_plugins_frame();  /* each returns, one round each */
    CHECK(seen_on() == 12 && seen_off() == 12);
    int (*seen_late)(void) = (int (*)(void))export_of("late.dll", "flag_seen");
    CHECK(seen_late && seen_late() == 12);  /* the late registrant heard every round too */

    /* Text: without the overlay's entry points both refuse; with them, they get the text. */
    int (*say)(const char *) = (int (*)(const char *))export_of("late.dll", "say");
    int (*place)(const char *) = (int (*)(const char *))export_of("late.dll", "place");
    CHECK(say && place && say("hello") == 1 && place("there") == 1);
    bb_plugins_set_text(fake_message, fake_text);
    CHECK(say("Round 2") == 0 && !strcmp(said, "Round 2") && said_seconds == 2.0f);
    CHECK(place("x") == 0 && !strcmp(placed, "x") && placed_x == 0.25f && placed_y == 0.5f && placed_scale == 1.5f &&
          placed_align == 2);
    /* Non-finite or non-positive numbers never reach the overlay (port-b148dcb-N1). */
    int (*odd)(float, float, float) = (int (*)(float, float, float))export_of("late.dll", "place_odd");
    const float nan = strtof("nan", NULL), inf = strtof("inf", NULL);
    CHECK(odd && odd(nan, 0.5f, 1.0f) == 1 && odd(0.5f, inf, 1.0f) == 1 && odd(0.5f, 0.5f, nan) == 1 && odd(0.5f, 0.5f, 0.0f) == 1);
    CHECK(!strcmp(placed, "x"));                 /* nothing new arrived */
    CHECK(odd(0.5f, 0.5f, 1.0f) == 0 && !strcmp(placed, "odd"));

    /* Input: what the window reports, field for field; the presses and motion only once. */
    {
        BbInput in;
        memset(&in, 0xab, sizeof in);
        CHECK(g_input(&in) == 0 && input_reads == 1);
        CHECK(in.key[4] == 1 && in.key[511] == 1 && in.key[5] == 0 && in.buttons == 2 && in.pressed == 3);
        CHECK(in.dx == 12.5f && in.dy == -3.0f && in.wheel == 1.0f && in.playing == 1);
        CHECK(g_input(&in) == 0 && in.pressed == 0 && in.dx == 0.0f && in.buttons == 2);
        /* The title or a load: the window says playing, but there's no player, so not playing. */
        put64(wcm + 0x60, 0);
        CHECK(g_input(&in) == 0 && in.playing == 0 && in.buttons == 2);
        put64(wcm + 0x60, (uint64_t)(uintptr_t)player);
        CHECK(g_input(&in) == 0 && in.playing == 1);
        /* Headless (no window): 1, and everything zero whatever the window side left. */
        bb_plugins_set_input(fake_input_headless);
        memset(&in, 0xab, sizeof in);
        CHECK(g_input(&in) == 1 && in.key[4] == 0 && in.buttons == 0 && in.pressed == 0 && in.dx == 0.0f &&
              in.playing == 0);
        bb_plugins_set_input(NULL);
        memset(&in, 0xab, sizeof in);
        CHECK(g_input(&in) == 1 && in.key[4] == 0 && in.playing == 0);
        CHECK(g_input(NULL) == 1);
    }

    /* input_take: through to input_take.c (any thread); bad arguments refused, nothing changed. */
    {
        int (*take)(const uint16_t *, size_t, uint32_t, int) = (int (*)(const uint16_t *, size_t, uint32_t, int))export_of("late.dll", "take");
        CHECK(take);
        bb_input_take_set_playing(NULL);
        const uint16_t f6[] = {63};
        CHECK(take(NULL, 1, 0, 0) == 1 && take(f6, 513, 0, 0) == 1);
        CHECK(take(f6, 1, 1, 1) == 0);
        bool keys[512], scratch[512];
        memset(keys, 1, sizeof keys);
        int nk = 512;
        CHECK(bb_input_take_keys(keys, &nk, scratch) == keys);  /* no playing test set: nothing taken */
        bb_input_take_set_playing(playing_now);
        const bool *seen = bb_input_take_keys(keys, &nk, scratch);
        CHECK(seen == scratch && !seen[63] && seen[62] && bb_input_take_buttons() == 1 && bb_input_take_motion());
        CHECK(take(NULL, 0, 0, 0) == 0 && !bb_input_take_active());  /* nothing taken: all given back */
        bb_input_take_set_playing(NULL);
    }

    /* World loads: the player's block at ChrIns+0x3f8. Late registered in bb_plugin_image. */
    int (*loads)(void) = (int (*)(void))export_of("late.dll", "world_loads");
    uint32_t (*load_block)(void) = (uint32_t (*)(void))export_of("late.dll", "world_block");
    CHECK(loads && load_block);
    put32(player + 0x3f8, 0xffffffffu);        /* between maps: nothing yet */
    bb_plugins_frame();
    CHECK(loads() == 0);
    put32(player + 0x3f8, 0x18010000);         /* placed in the Hunter's Dream */
    bb_plugins_frame();
    CHECK(loads() == 1 && load_block() == 0x18010000);
    bb_plugins_frame();                        /* the same player, the same block: no load */
    CHECK(loads() == 1);
    put32(player + 0x3f8, 0x18000000);         /* a warp to another block */
    bb_plugins_frame();
    CHECK(loads() == 2 && load_block() == 0x18000000);
    unsigned char *player2 = calloc(1, 0x400);  /* a load: a new main player, the same block */
    memcpy(player2, player, 0x400);
    put64(wcm + 0x60, (uint64_t)(uintptr_t)player2);
    bb_plugins_frame();
    CHECK(loads() == 3);
    put64(wcm + 0x60, 0);                      /* no player: nothing */
    bb_plugins_frame();
    CHECK(loads() == 3);
    put64(wcm + 0x60, (uint64_t)(uintptr_t)player2);  /* back: a new placement after none */
    bb_plugins_frame();
    CHECK(loads() == 4);

    printf("flag delivery: %d checks passed\n", checks);
    return 0;
}
