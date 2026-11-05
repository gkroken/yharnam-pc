/* SPDX-License-Identifier: GPL-2.0-or-later
 * Mouse camera (mouse_camera.h). The camera update and its fields are imedved's (Bloodborne
 * mouse camera, Nexus Mods), as mcrib884 (Supermedo/bloodborne_pc#3) and Jhony-Jhansen
 * (Supermedo/bloodborne_pc#23) ported it. They patched the code from the window's thread while
 * the game ran; here the hooks go in before the game starts and everything runs on the game's
 * own thread, at the start of its camera update:
 *
 * - The camera object is in r13 there (the update reads its pitch next). The mouse motion since
 *   the last update turns it: pitch (target, smoothed and converging) and yaw (target and
 *   converging), the pitch kept within the camera's own limits.
 * - Four stores of the game's own turn (yaw twice, 0x130 and 0x294) are skipped while the mouse
 *   drives the camera, as the mod removes them, and run again as soon as the right stick turns
 *   it (a gamepad or bound keys) or the mouse has been still for half a second: controller play
 *   is the game's own code. The "No camera auto-rotation while moving" tweak
 *   (tweak_no_camera_rotation, the same mod's patch) blanks those four stores for good; the
 *   mouse camera then leaves them blank and only turns the camera.
 * - Locked on, the game turns the camera; a mouse flick is then a right-stick flick, which
 *   switches targets. That is checked once a frame on the main thread (bb_on_main_frame), not
 *   in the camera update: locked on, the update above does not run at all (0 a second), while
 *   the lock field of the camera it last updated stays 1. The flick holds the stick for
 *   FLICK_US, as a tap of a bound key does. BB_MOUSE_DEBUG=1 logs the lock and each flick.
 * - Through the monocular, the motion turns its view instead, at half the rate. */
#include "mouse_camera.h"
#include "input_take.h"

#include "engine_hooks.h"
#include "gpu/bbgpu.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum {
    CAMERA_UPDATE = 0x143ceaa, /* vmovss xmm0, [r13+0x140]: image offsets of game 1.09 */
    MONOCULAR = 0x553e8d0,     /* pointer chain to the monocular's view */
    PITCH = 0x140, YAW = 0x144, PITCH_SMOOTH = 0x150, LOCKED = 0x154, TURN_RATE = 0x130,
    PITCH_MAX = 0x1ec, PITCH_MIN = 0x1f0, PITCH_CONVERGE = 0x26c, YAW_CONVERGE = 0x270,
    STILL_UPDATES = 30,  /* the mouse lets go after this many updates without motion */
    STICK_UPDATES = 20,  /* ... and at once for this many after the stick turned the camera */
};
#define FLICK_US 120000ull       /* the stick is held this long per flick */
#define FLICK_COOLDOWN_US 300000ull /* motion after a flick is dropped this long: one flick, one switch */
static const float COUNTS_PER_RADIAN = 900.0f, FLICK_DECAY_S = 0.1f; /* slow motion decays away */
static const float PI = 3.14159265f, TAU = 6.28318531f;

static const BbCode update_code = {9, 0x025464a80bd36ee0ull};
static const struct {
    uint64_t at;
    BbCode code;
} stores[] = {
    {0x143c6e8, {9, 0x599f4439954122e5ull}}, /* vmovss [r13+0x144], xmm0 */
    {0x143c984, {9, 0x71be1eacbb1f4181ull}}, /* vmovss [r13+0x130], xmm1 */
    {0x143dde6, {9, 0x414d109e95be3994ull}}, /* vmovss [r13+0x294], xmm0 */
    {0x143c870, {9, 0x5e4bc59600d8836dull}}, /* vmovss [r13+0x144], xmm5 */
};
#define STORE_COUNT ((int)(sizeof(stores) / sizeof(*stores)))

/* What tweak_no_camera_rotation leaves at each store: nine one-byte nops. */
static const unsigned char blank_store[9] = {0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90};

static BbSkip *g_skips[STORE_COUNT];
static int g_skipping, g_active;
static float g_flick_counts;     /* main-frame tick only */
static uint64_t g_cooldown_until;
static volatile int g_stick_updates, g_flick_dir; /* written by the pad read, the camera update, the tick */
static volatile uint64_t g_cam, g_cam_updates, g_flick_until;

static uint64_t now_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000u + (uint64_t)t.tv_nsec / 1000u;
}

static int setting(const char *key, int fallback) {
    int value = fallback;
    return bbgpu_setting_get(key, &value) ? value : fallback;
}

static void skip_stores(int skip) {
    if (skip == g_skipping) return;
    g_skipping = skip;
    for (int i = 0; i < STORE_COUNT; ++i) bb_skip(g_skips[i], skip);
}

static float field(unsigned char *cam, int at) {
    float value;
    memcpy(&value, cam + at, 4);
    return value;
}
static void set_field(unsigned char *cam, int at, float value) { memcpy(cam + at, &value, 4); }

static float wrap(float yaw) {
    while (yaw > PI) yaw -= TAU;
    while (yaw < -PI) yaw += TAU;
    return yaw;
}

/* Through the monocular: its view turns instead. 1 when it took the motion. */
static int turn_monocular(float pitch, float yaw) {
    uint64_t p1, p2, p3;
    int32_t on;
    if (!bb_read((uint64_t)(uintptr_t)bb_guest(MONOCULAR), &p1, 8) || !p1 || !bb_read(p1 + 0x68, &p2, 8) || !p2 ||
        !bb_read(p2 + 0x84, &on, 4) || !on || !bb_read(p2 + 0x68, &p3, 8) || !p3)
        return 0;
    float view[2];
    if (!bb_read(p3 + 0x148, view, 8)) return 0;
    view[0] = fminf(fmaxf(view[0] + pitch * 0.5f, -1.94f), 1.71f);
    view[1] = wrap(view[1] + yaw * 0.5f);
    memcpy((void *)(uintptr_t)(p3 + 0x148), view, 8);
    return 1;
}

static BB_GUEST_ABI void on_camera_update(BbRegs *regs) {
    unsigned char *cam = (unsigned char *)(uintptr_t)regs->r13;
    __atomic_store_n(&g_cam, regs->r13, __ATOMIC_RELAXED);
    __atomic_add_fetch(&g_cam_updates, 1, __ATOMIC_RELAXED);
    float dx = 0, dy = 0;
    /* Locked on, the game turns the camera and the motion is left to flick_tick. */
    if (!bbgpu_mouse_captured() || !setting("mouse_camera", 1) || field(cam, LOCKED) == 1.0f) {
        g_active = 0;
        skip_stores(0);
        return;
    }
    bbgpu_mouse_motion_take(&dx, &dy);
    if (bb_input_take_motion()) dx = dy = 0;  /* a plugin took the motion (input_take.h) */
    const float scale = (float)setting("mouse_sensitivity", 10) / 10.0f;
    if (setting("mouse_invert_y", 0)) dy = -dy;
    const float pitch = dy / COUNTS_PER_RADIAN * scale, yaw = dx / COUNTS_PER_RADIAN * scale;

    const int stick = __atomic_load_n(&g_stick_updates, __ATOMIC_RELAXED);
    if (stick) {
        __atomic_store_n(&g_stick_updates, stick - 1, __ATOMIC_RELAXED);
        g_active = 0;
        skip_stores(0);
        return;
    }
    if (dx || dy) g_active = STILL_UPDATES;
    else if (g_active) --g_active;
    if (!g_active) {
        skip_stores(0);
        return;
    }
    skip_stores(1);
    set_field(cam, TURN_RATE, 0.7f); /* the mod's value while its store is skipped */
    if ((!dx && !dy) || turn_monocular(pitch, yaw)) return;

    float min = field(cam, PITCH_MIN), max = field(cam, PITCH_MAX);
    if (!(min < max) || min < -3.0f || max > 3.0f) min = -1.94f, max = 1.71f;
    const float new_pitch = fminf(fmaxf(field(cam, PITCH) + pitch, min), max);
    const float new_yaw = wrap(field(cam, YAW) + yaw);
    set_field(cam, PITCH, new_pitch);
    set_field(cam, PITCH_SMOOTH, new_pitch);
    set_field(cam, PITCH_CONVERGE, new_pitch);
    set_field(cam, YAW, new_yaw);
    set_field(cam, YAW_CONVERGE, new_yaw);
}

/* Once a frame (main thread): locked on, sideways mouse motion adds up to a flick. It decays
 * with FLICK_DECAY_S, so only a quick sweep past mouse_flick_threshold counts switches. */
static void flick_tick(void) {
    static int debug = -1, was_locked;
    static uint64_t debug_next, debug_updates, last_tick;
    if (debug < 0) debug = getenv("BB_MOUSE_DEBUG") && strcmp(getenv("BB_MOUSE_DEBUG"), "0");
    const uint64_t now = now_us(), cam = __atomic_load_n(&g_cam, __ATOMIC_RELAXED);
    const float dt = last_tick && now > last_tick ? (float)(now - last_tick) / 1e6f : 0.0f;
    last_tick = now;
    float lock_field = 0;
    const int field_ok = cam && bb_read(cam + LOCKED, &lock_field, 4);
    const int captured = bbgpu_mouse_captured() && setting("mouse_camera", 1);
    const int locked = captured && field_ok && lock_field == 1.0f;
    if (debug && now >= debug_next) {
        const uint64_t updates = __atomic_load_n(&g_cam_updates, __ATOMIC_RELAXED);
        printf("Mouse flick: captured=%d camera updates/s=%llu lock field=%.6f (read %d) locked=%d\n", captured,
               (unsigned long long)(updates - debug_updates), lock_field, field_ok, locked);
        debug_updates = updates;
        debug_next = now + 1000000;
    }
    if (debug && locked != was_locked) printf("Mouse flick: lock %s\n", locked ? "on" : "off");
    was_locked = locked;
    if (!locked) {
        g_flick_counts = 0;
        return;
    }
    float dx = 0, dy = 0;
    bbgpu_mouse_motion_take(&dx, &dy);
    if (bb_input_take_motion()) dx = dy = 0;
    const int threshold = setting("mouse_flick_threshold", 300);
    if (now < g_cooldown_until || threshold <= 0) {
        g_flick_counts = 0;
        return;
    }
    g_flick_counts = g_flick_counts * expf(-dt / FLICK_DECAY_S) + dx;
    if (fabsf(g_flick_counts) < (float)threshold) return;
    __atomic_store_n(&g_flick_dir, g_flick_counts > 0 ? 1 : -1, __ATOMIC_RELAXED);
    __atomic_store_n(&g_flick_until, now + FLICK_US, __ATOMIC_RELAXED);
    if (debug) printf("Mouse flick: %s (%.0f counts)\n", g_flick_counts > 0 ? "right" : "left", g_flick_counts);
    g_flick_counts = 0;
    g_cooldown_until = now + FLICK_COOLDOWN_US;
}

int bb_mouse_camera_install(void) {
    const char *env = getenv("BB_MOUSE_CAMERA");
    if (env && !strcmp(env, "0")) return 0;
    /* Every site first, so a mismatch leaves the game unchanged. The stores are either the
     * game's or all four blanked by tweak_no_camera_rotation, which is applied before the hooks
     * go in: blanked, there is nothing left to skip (the check used to fail there and leave the
     * mouse camera out, #63 and #108). */
    const unsigned char *update = bb_guest(CAMERA_UPDATE);
    const BbCode blank_code = {sizeof(blank_store), bb_code_hash(blank_store, sizeof(blank_store))};
    int original = bb_code_is(update, update_code), blank = original;
    for (int i = 0; i < STORE_COUNT; ++i) {
        original = original && bb_code_is(bb_guest(stores[i].at), stores[i].code);
        blank = blank && bb_code_is(bb_guest(stores[i].at), blank_code);
    }
    if (!original && !blank) {
        printf("Mouse camera: the camera code is not game 1.09's; not installed\n");
        return 0;
    }
    for (int i = 0; original && i < STORE_COUNT; ++i) {
        g_skips[i] = bb_hook_skip(stores[i].at, stores[i].code);
        if (!g_skips[i]) {
            printf("Mouse camera: %s\n", bb_hooks_error());
            return i;
        }
    }
    if (bb_hook_mid(CAMERA_UPDATE, update_code, on_camera_update)) {
        printf("Mouse camera: %s\n", bb_hooks_error());
        return STORE_COUNT;
    }
    bb_on_main_frame(flick_tick);
    printf("Mouse camera: installed%s (click the game window to use it)\n",
           original ? "" : ", camera auto-rotation off");
    return original ? STORE_COUNT + 1 : 1;
}

int bb_mouse_camera_stick_x(void) {
    if (now_us() >= __atomic_load_n(&g_flick_until, __ATOMIC_RELAXED)) return -1;
    return __atomic_load_n(&g_flick_dir, __ATOMIC_RELAXED) > 0 ? 255 : 0;
}

void bb_mouse_camera_note_stick(uint8_t right_x, uint8_t right_y) {
    if (abs(right_x - 128) > 48 || abs(right_y - 128) > 48)
        __atomic_store_n(&g_stick_updates, STICK_UPDATES, __ATOMIC_RELAXED);
}
