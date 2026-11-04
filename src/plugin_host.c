/* SPDX-License-Identifier: GPL-3.0-or-later
 * The native plugin host (plugin_host.h). The API table is bbhost's (include/bbhost_plugin.h);
 * the lifecycle, the folders and the switches follow bbhost's src/host/plugins.cpp (droogie,
 * GPL-3.0-or-later), written again for this port's loader and engine hooks.
 *
 * Every field of the table is filled. A function this host does not provide yet returns the
 * failure value its comment in bbhost_plugin.h gives and says so once in the log, so a plugin
 * written for bbhost meets an error it already handles, never a null pointer. */
#include "plugin_host.h"

#include "bbhost_plugin.h"
#include "engine_hooks.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>

enum { MAX_PLUGINS = 64, MAX_CALLBACKS = 256, MAX_DIRS = 8, MAX_INI = 512, NAME_MAX_LEN = 63 };
enum { BN_BASE = 0x400000 };

/* Where the start is: functions marked "from bb_plugin_image on" refuse before it. */
enum Phase { PHASE_NONE, PHASE_INIT, PHASE_IMAGE, PHASE_RUNNING };

typedef struct {
    HMODULE dll;
    char name[NAME_MAX_LEN + 1];
    char file[MAX_PATH * 3];  /* UTF-8, for the log */
    const BbPluginInfo *info;
    int (*init)(const BbHostApi *);
    int (*image)(const BbHostApi *);
    int live;
} Plugin;

typedef struct {
    int owner;  /* index into g_plugins */
    void (*fn)(void *);
    void *user;
} FrameCallback;

static Plugin g_plugins[MAX_PLUGINS];
static int g_count;
static FrameCallback g_frames[MAX_CALLBACKS];
static int g_frame_count;
static SRWLOCK g_lock = SRWLOCK_INIT;
static enum Phase g_phase;
static int g_tick_registered;
static BbHostApi g_api;

/* The plugin whose code is running on this thread (its start, its callback), for log lines
 * and the ownership of what it registers. -1 outside a plugin's call. */
static _Thread_local int t_current = -1;

/* --- small helpers ------------------------------------------------------------------------ */

static void host_log(const char *fmt, ...) {
    char line[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    printf("Plugins: %s\n", line);
    fflush(stdout);
}

static const char *current_name(void) {
    return t_current >= 0 && t_current < g_count ? g_plugins[t_current].name : "?";
}

static void to_utf8(const wchar_t *in, char *out, size_t n) {
    if (!WideCharToMultiByte(CP_UTF8, 0, in, -1, out, (int)n, NULL, NULL) && n) out[0] = 0;
}

static int env_is(const char *name, const char *value) {
    const char *v = getenv(name);
    return v && !strcmp(v, value);
}

static int online_play(void) { return env_is("BB_ONLINE", "1") && !env_is("BB_COOP", "1"); }

/* <data>: the folder above bb-probe.exe's (out\ in a source tree, bin\ in the package). */
static void data_root(wchar_t *out, size_t n) {
    wchar_t exe[MAX_PATH];
    DWORD len = GetModuleFileNameW(NULL, exe, MAX_PATH);
    out[0] = 0;
    if (!len || len >= MAX_PATH) return;
    for (int up = 0; up < 2; ++up) {
        wchar_t *slash = wcsrchr(exe, L'\\');
        if (!slash) return;
        *slash = 0;
    }
    wcsncpy(out, exe, n - 1);
    out[n - 1] = 0;
}

static void user_root(wchar_t *out, size_t n) {
    const wchar_t *env = _wgetenv(L"BB_GPU_USER_DIR");
    if (env && *env) {
        wcsncpy(out, env, n - 1);
        out[n - 1] = 0;
        return;
    }
    data_root(out, n);
    if (*out) wcsncat(out, L"\\user", n - wcslen(out) - 1);
}

/* --- plugins.ini ------------------------------------------------------------------------- */

typedef struct {
    char key[128];   /* "section.key" */
    char value[256];
} IniEntry;
static IniEntry g_ini[MAX_INI];
static int g_ini_count;

static char *trim(char *s) {
    while (*s == ' ' || *s == '\t') ++s;
    char *end = s + strlen(s);
    while (end > s && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n')) *--end = 0;
    return s;
}

static void ini_load(void) {
    g_ini_count = 0;
    wchar_t path[MAX_PATH];
    user_root(path, MAX_PATH);
    if (!*path) return;
    wcsncat(path, L"\\plugins.ini", MAX_PATH - wcslen(path) - 1);
    FILE *f = _wfopen(path, L"rb");
    if (!f) return;
    char line[512], section[64] = "";
    while (fgets(line, sizeof line, f) && g_ini_count < MAX_INI) {
        char *s = trim(line);
        if (!strncmp(s, "\xef\xbb\xbf", 3)) s = trim(s + 3);
        if (!*s || *s == '#' || *s == ';') continue;
        if (*s == '[') {
            char *close = strchr(s, ']');
            if (close) {
                *close = 0;
                snprintf(section, sizeof section, "%s", trim(s + 1));
            }
            continue;
        }
        char *eq = strchr(s, '=');
        if (!eq || !*section) continue;
        *eq = 0;
        IniEntry *e = &g_ini[g_ini_count++];
        snprintf(e->key, sizeof e->key, "%s.%s", section, trim(s));
        snprintf(e->value, sizeof e->value, "%s", trim(eq + 1));
    }
    fclose(f);
}

static const char *ini_get(const char *key) {
    for (int i = g_ini_count - 1; i >= 0; --i)  /* the last line wins */
        if (!_stricmp(g_ini[i].key, key)) return g_ini[i].value;
    return NULL;
}

/* 1 on, 0 off, -1 not said. */
static int ini_switch(const char *name) {
    char key[128];
    snprintf(key, sizeof key, "plugins.%s", name);
    const char *v = ini_get(key);
    if (!v) return -1;
    if (!_stricmp(v, "1") || !_stricmp(v, "true") || !_stricmp(v, "on")) return 1;
    if (!_stricmp(v, "0") || !_stricmp(v, "false") || !_stricmp(v, "off")) return 0;
    return -1;
}

/* --- addresses ----------------------------------------------------------------------------- */

static int eboot_is_109(void) { return bb_image_size() && env_is("BB_EBOOT_109", "1"); }

/* The image's bytes at a Binary Ninja address, n of them, or NULL. */
static unsigned char *image_at(uint64_t bn, size_t n) {
    const size_t size = bb_image_size();
    unsigned char *image = bb_guest(0);
    if (!image || !size || bn < BN_BASE) return NULL;
    const uint64_t offset = bn - BN_BASE;
    if (offset > size || n > size - offset) return NULL;
    return image + offset;
}

/* read() and write(): a Binary Ninja address inside the image, else a runtime address (the
 * game runs natively here, so the pointers it keeps are the process's own). */
static unsigned char *resolve(uint64_t address, size_t n) {
    unsigned char *in_image = image_at(address, n);
    return in_image ? in_image : (unsigned char *)(uintptr_t)address;
}

static int writable(const void *at, size_t n) {
    const unsigned char *p = at, *end = p + n;
    while (p < end) {
        MEMORY_BASIC_INFORMATION mbi;
        if (!VirtualQuery(p, &mbi, sizeof mbi) || mbi.State != MEM_COMMIT) return 0;
        const DWORD rw = PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
        if (!(mbi.Protect & rw) || (mbi.Protect & PAGE_GUARD)) return 0;
        p = (const unsigned char *)mbi.BaseAddress + mbi.RegionSize;
    }
    return 1;
}

/* --- the API: what this host provides -------------------------------------------------------- */

static void api_log(const char *fmt, ...) {
    char line[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt ? fmt : "", ap);
    va_end(ap);
    printf("Plugin %s: %s\n", current_name(), line);
    fflush(stdout);
}

static const char *api_config(const char *key) {
    static _Thread_local char value[256];
    value[0] = 0;
    if (!key) return value;
    /* This host answers a few keys of its own, so a plugin can tell where it runs. */
    if (!_stricmp(key, "host.name")) return strcpy(value, "yharnam-pc");
    const char *v = ini_get(key);
    if (v) snprintf(value, sizeof value, "%s", v);
    return value;
}

static const char *api_data_dir(void) {
    static char dir[MAX_PATH * 3];
    wchar_t w[MAX_PATH];
    user_root(w, MAX_PATH);
    to_utf8(w, dir, sizeof dir);
    return dir;
}

static const char *api_mods_dir(void) {
    static char dir[MAX_PATH * 3];
    const wchar_t *env = _wgetenv(L"BB_MODS_DIR");
    wchar_t w[MAX_PATH];
    if (env && *env) {
        wcsncpy(w, env, MAX_PATH - 1);
        w[MAX_PATH - 1] = 0;
    } else {
        data_root(w, MAX_PATH);
        if (*w) wcsncat(w, L"\\mods", MAX_PATH - wcslen(w) - 1);
    }
    to_utf8(w, dir, sizeof dir);
    return dir;
}

static int api_register_hle(const char *name, void *fn) {
    (void)fn;
    /* This port's system functions are its own runtime (src/runtime_*.c), not bbhost's HLE
     * table: there is nothing to replace them in yet. */
    api_log("register_hle(%s): not supported by this host; the import keeps its own implementation",
            name ? name : "?");
    return 1;
}

static const char *api_eboot_sha256(void) {
    const char *v = getenv("BB_EBOOT_SHA256");
    return g_phase >= PHASE_IMAGE && v ? v : "";
}

static int api_eboot_is_109(void) { return g_phase >= PHASE_IMAGE && eboot_is_109(); }

static uint64_t api_guest_addr(uint64_t bn) {
    if (g_phase < PHASE_IMAGE || !eboot_is_109()) return 0;
    return (uint64_t)(uintptr_t)image_at(bn, 1);
}

static int api_read(uint64_t address, void *out, size_t n) {
    if (g_phase < PHASE_IMAGE || !out || !address) return 1;
    if (!n) return 0;
    return bb_read((uint64_t)(uintptr_t)resolve(address, n), out, n) ? 0 : 1;
}

static int api_write(uint64_t address, const void *data, size_t n) {
    if (g_phase < PHASE_IMAGE || !data || !address) return 1;
    if (!n) return 0;
    unsigned char *at = resolve(address, n);
    /* Code is patch()'s, which checks what it replaces. During bb_plugin_image the whole image is
     * still writable, and in co-op the code stays writable: the segments decide, not the pages.
     * Either kind of address can land in the image (a runtime pointer read out of the game too). */
    const uintptr_t image = (uintptr_t)bb_guest(0), size = bb_image_size(), p = (uintptr_t)at;
    if (n > UINTPTR_MAX - p) return 1;
    const uintptr_t lo = p > image ? p : image, hi = p + n < image + size ? p + n : image + size;
    if (image && lo < hi && bb_image_is_code(lo - image, hi - lo)) return 1;
    if (!writable(at, n)) return 1;
    memcpy(at, data, n);
    return 0;
}

static int api_patch(uint64_t bn, const void *expect, const void *bytes, size_t n) {
    if (g_phase < PHASE_IMAGE || !eboot_is_109() || !expect || !bytes || !n) return 1;
    unsigned char *at = image_at(bn, n);
    if (!at) return 2;
    /* As bbhost: already patched is done (a plugin's second start, or two plugins agreeing). */
    if (memcmp(at, expect, n)) return memcmp(at, bytes, n) ? 1 : 0;
    DWORD old;
    if (!VirtualProtect(at, n, PAGE_EXECUTE_READWRITE, &old)) return 2;
    memcpy(at, bytes, n);
    VirtualProtect(at, n, old, &old);
    FlushInstructionCache(GetCurrentProcess(), at, n);
    return 0;
}

static int api_on_frame(void (*fn)(void *), void *user) {
    if (!fn) return 1;
    AcquireSRWLockExclusive(&g_lock);
    const int ok = g_frame_count < MAX_CALLBACKS;
    if (ok) g_frames[g_frame_count++] = (FrameCallback){t_current, fn, user};
    ReleaseSRWLockExclusive(&g_lock);
    if (!ok) api_log("on_frame: no room for another callback (%d)", MAX_CALLBACKS);
    return ok ? 0 : 1;
}

static const char *api_plugin_dir(const char *name) {
    static _Thread_local char dir[MAX_PATH * 3];
    dir[0] = 0;
    if (!name || !*name || strpbrk(name, "\\/:*?\"<>|") || strstr(name, "..")) return dir;
    wchar_t w[MAX_PATH], wname[NAME_MAX_LEN + 1];
    if (!MultiByteToWideChar(CP_UTF8, 0, name, -1, wname, NAME_MAX_LEN + 1)) return dir;
    user_root(w, MAX_PATH);
    if (!*w) return dir;
    wcsncat(w, L"\\plugins", MAX_PATH - wcslen(w) - 1);
    CreateDirectoryW(w, NULL);
    wcsncat(w, L"\\", MAX_PATH - wcslen(w) - 1);
    wcsncat(w, wname, MAX_PATH - wcslen(w) - 1);
    CreateDirectoryW(w, NULL);
    wcsncat(w, L"\\", MAX_PATH - wcslen(w) - 1);
    to_utf8(w, dir, sizeof dir);
    return dir;
}

/* --- the API: not provided yet ------------------------------------------------------------------ */

/* Says once per function that it is missing; the caller then returns the failure value. */
static void missing(const char *function) {
    static SRWLOCK lock = SRWLOCK_INIT;
    static const char *said[64];
    static int count;
    int first = 1;
    AcquireSRWLockExclusive(&lock);
    for (int i = 0; i < count; ++i)
        if (said[i] == function) first = 0;
    if (first && count < 64) said[count++] = function;  /* the callers pass string literals */
    ReleaseSRWLockExclusive(&lock);
    if (first) api_log("%s is not available in this host yet; it returned the failure value", function);
}

static int api_hook(uint64_t bn, const void *prologue, size_t n, BbHookFn fn, void *user) {
    (void)bn, (void)prologue, (void)n, (void)fn, (void)user;
    missing("hook");
    return 2;
}
static void *api_param_row(const char *table, uint32_t id, size_t *bytes) {
    (void)table, (void)id;
    if (bytes) *bytes = 0;
    missing("param_row");
    return NULL;
}
static int api_param_get(const char *table, uint32_t id, const char *field, char *out, size_t n) {
    (void)table, (void)id, (void)field;
    if (out && n) out[0] = 0;
    missing("param_get");
    return 1;
}
static int api_param_set(const char *table, uint32_t id, const char *field, const char *value) {
    (void)table, (void)id, (void)field, (void)value;
    missing("param_set");
    return 1;
}
static int api_lua_event(const char *name) {
    (void)name;
    missing("lua_event");
    return 1;
}
static int api_event_flag_get(uint32_t id, int *value) {
    (void)id;
    if (value) *value = 0;
    missing("event_flag_get");
    return 1;
}
static int api_event_flag_set(uint32_t id, int value) {
    (void)id, (void)value;
    missing("event_flag_set");
    return 1;
}
static int api_player_stat_get(const char *name, int64_t *value) {
    (void)name;
    if (value) *value = 0;
    missing("player_stat_get");
    return 1;
}
static int api_player_stat_set(const char *name, int64_t value) {
    (void)name, (void)value;
    missing("player_stat_set");
    return 1;
}
static int api_on_event_flag(void (*fn)(uint32_t, int, void *), void *user) {
    (void)fn, (void)user;
    missing("on_event_flag");
    return 1;
}
static int api_player_position(float xyz[3], float *yaw) {
    (void)xyz, (void)yaw;
    missing("player_position");
    return 1;
}
static int api_camera_get(float pos[3], float focus[3]) {
    (void)pos, (void)focus;
    missing("camera_get");
    return 1;
}
static int api_player_block(uint32_t *block) {
    (void)block;
    missing("player_block");
    return 1;
}
static int api_player_warp(uint32_t block, const float xyz[3], float yaw) {
    (void)block, (void)xyz, (void)yaw;
    missing("player_warp");
    return 1;
}
static int api_lamp_warp(int32_t return_point) {
    (void)return_point;
    missing("lamp_warp");
    return 1;
}
static uint64_t api_symbol(const char *name) {
    (void)name;
    missing("symbol");
    return 0;
}
static int api_call_guest(uint64_t bn, BbCallRegs *regs) {
    (void)bn, (void)regs;
    missing("call_guest");
    return 1;
}
static int api_hook2(uint64_t bn, const void *prologue, size_t n, BbHookFn2 fn, void *user) {
    (void)bn, (void)prologue, (void)n, (void)fn, (void)user;
    missing("hook2");
    return 2;
}
static int api_param_ids(const char *table, uint32_t *ids, size_t max, size_t *count) {
    (void)table, (void)ids, (void)max;
    if (count) *count = 0;
    missing("param_ids");
    return 1;
}
static int api_on_world_load(void (*fn)(uint32_t, void *), void *user) {
    (void)fn, (void)user;
    missing("on_world_load");
    return 1;
}
static int api_chr_list(BbChr *out, size_t max, size_t *count) {
    (void)out, (void)max;
    if (count) *count = 0;
    missing("chr_list");
    return 1;
}
static int api_sp_effect_apply(void *chr, int32_t sp_effect) {
    (void)chr, (void)sp_effect;
    missing("sp_effect_apply");
    return 1;
}
static int api_sp_effect_has(void *chr, int32_t sp_effect) {
    (void)chr, (void)sp_effect;
    missing("sp_effect_has");
    return 0;
}
static int api_show_message(const char *text, float seconds) {
    (void)text, (void)seconds;
    missing("show_message");
    return 1;
}
static int api_set_time_scale(float scale) {
    (void)scale;
    missing("set_time_scale");
    return 1;
}
static int api_on_option(const char *name, void (*fn)(const char *, const char *, void *), void *user) {
    (void)name, (void)fn, (void)user;
    missing("on_option");
    return 1;
}
static int api_on_action(const char *name, void (*fn)(const char *, void *), void *user) {
    (void)name, (void)fn, (void)user;
    missing("on_action");
    return 1;
}
static int api_game_file(const char *path, const void **data, size_t *size) {
    (void)path;
    if (data) *data = NULL;
    if (size) *size = 0;
    missing("game_file");
    return 1;
}
static int api_overlay_file(const char *name, const char *path, const void *data, size_t size) {
    (void)name, (void)path, (void)data, (void)size;
    missing("overlay_file");
    return 1;
}
static int api_set_rules(const char *name, const char *rules) {
    (void)name, (void)rules;
    missing("set_rules");
    return 1;
}
static int api_on_world_rules(const char *name, void (*fn)(const char *, void *), void *user) {
    (void)name, (void)fn, (void)user;
    missing("on_world_rules");
    return 1;
}
static int api_visitor(const char *name) {
    (void)name;
    return 0;  /* this host never loads a plugin as a visitor */
}
static int api_replace_text(const BbText *texts, size_t count) {
    (void)texts, (void)count;
    missing("replace_text");
    return -1;
}
static int api_loading_screen(void) {
    missing("loading_screen");
    return 0;
}
static int api_input(BbInput *out) {
    if (out) memset(out, 0, sizeof *out);
    missing("input");
    return 1;
}
static int api_input_take(const uint16_t *scancodes, size_t n, uint32_t buttons, int wheel, int motion) {
    (void)scancodes, (void)n, (void)buttons, (void)wheel, (void)motion;
    missing("input_take");
    return 1;
}
static int api_on_camera(void (*fn)(BbCamera *, void *), void *user) {
    (void)fn, (void)user;
    missing("on_camera");
    return 1;
}
static int api_key_bound(uint16_t scancode) {
    (void)scancode;
    missing("key_bound");
    return 0;
}
static int api_show_text(const char *text, float seconds, float x, float y, float scale, uint32_t align) {
    (void)text, (void)seconds, (void)x, (void)y, (void)scale, (void)align;
    missing("show_text");
    return 1;
}
static int api_on_scene_category(int32_t pass, void (*fn)(uint64_t, void *), void *user) {
    (void)pass, (void)fn, (void)user;
    missing("on_scene_category");
    return 1;
}
static void api_draw_style(int style) {
    (void)style;
    missing("draw_style");
}

static void fill_api(void) {
    g_api = (BbHostApi){
        .version = BB_PLUGIN_API_VERSION, .size = sizeof(BbHostApi),
        .log = api_log, .config = api_config, .data_dir = api_data_dir, .mods_dir = api_mods_dir,
        .register_hle = api_register_hle, .eboot_sha256 = api_eboot_sha256, .eboot_is_109 = api_eboot_is_109,
        .guest_addr = api_guest_addr, .read = api_read, .write = api_write, .patch = api_patch,
        .hook = api_hook, .on_frame = api_on_frame, .param_row = api_param_row, .param_get = api_param_get,
        .param_set = api_param_set, .lua_event = api_lua_event, .event_flag_get = api_event_flag_get,
        .event_flag_set = api_event_flag_set, .player_stat_get = api_player_stat_get,
        .player_stat_set = api_player_stat_set, .on_event_flag = api_on_event_flag,
        .player_position = api_player_position, .camera_get = api_camera_get, .player_block = api_player_block,
        .player_warp = api_player_warp, .lamp_warp = api_lamp_warp, .symbol = api_symbol,
        .call_guest = api_call_guest, .hook2 = api_hook2, .param_ids = api_param_ids,
        .on_world_load = api_on_world_load, .chr_list = api_chr_list, .sp_effect_apply = api_sp_effect_apply,
        .sp_effect_has = api_sp_effect_has, .plugin_dir = api_plugin_dir, .show_message = api_show_message,
        .set_time_scale = api_set_time_scale, .on_option = api_on_option, .on_action = api_on_action,
        .game_file = api_game_file, .overlay_file = api_overlay_file, .set_rules = api_set_rules,
        .on_world_rules = api_on_world_rules, .visitor = api_visitor, .replace_text = api_replace_text,
        .loading_screen = api_loading_screen, .input = api_input, .input_take = api_input_take,
        .on_camera = api_on_camera, .key_bound = api_key_bound, .show_text = api_show_text,
        .on_scene_category = api_on_scene_category, .draw_style = api_draw_style,
    };
}

/* --- loading ----------------------------------------------------------------------------------- */

typedef struct {
    wchar_t path[MAX_PATH];
    wchar_t file[MAX_PATH];  /* the name alone: a later folder's file of that name replaces it */
} Candidate;

static int dir_list(wchar_t dirs[MAX_DIRS][MAX_PATH]) {
    int n = 0;
    const wchar_t *env = _wgetenv(L"BB_PLUGIN_DIRS");
    if (env && *env) {
        wchar_t copy[MAX_PATH * MAX_DIRS];
        wcsncpy(copy, env, sizeof copy / sizeof *copy - 1);
        copy[sizeof copy / sizeof *copy - 1] = 0;
        wchar_t *state = NULL;
        for (wchar_t *part = wcstok(copy, L";", &state); part && n < MAX_DIRS; part = wcstok(NULL, L";", &state)) {
            wcsncpy(dirs[n], part, MAX_PATH - 1);
            dirs[n++][MAX_PATH - 1] = 0;
        }
        return n;
    }
    data_root(dirs[n], MAX_PATH);
    if (*dirs[n]) {
        wcsncat(dirs[n], L"\\plugins", MAX_PATH - wcslen(dirs[n]) - 1);
        ++n;
    }
    user_root(dirs[n], MAX_PATH);
    if (*dirs[n]) {
        wcsncat(dirs[n], L"\\plugins", MAX_PATH - wcslen(dirs[n]) - 1);
        ++n;
    }
    return n;
}

static int candidate_order(const void *a, const void *b) {
    return _wcsicmp(((const Candidate *)a)->file, ((const Candidate *)b)->file);
}

/* The DLLs directly inside the folders (not in subfolders: a plugin keeps the DLLs it needs in
 * a folder of its own), each file name once, sorted by name. */
static int scan(Candidate *out, int max) {
    wchar_t dirs[MAX_DIRS][MAX_PATH];
    const int ndirs = dir_list(dirs);
    int n = 0;
    for (int d = 0; d < ndirs; ++d) {
        wchar_t pattern[MAX_PATH];
        if (_snwprintf(pattern, MAX_PATH, L"%ls\\*.dll", dirs[d]) < 0) continue;
        WIN32_FIND_DATAW found;
        HANDLE h = FindFirstFileW(pattern, &found);
        if (h == INVALID_HANDLE_VALUE) continue;
        do {
            if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
            int at = -1;
            for (int i = 0; i < n; ++i)
                if (!_wcsicmp(out[i].file, found.cFileName)) at = i;
            if (at < 0) {
                if (n == max) continue;
                at = n++;
            }
            wcsncpy(out[at].file, found.cFileName, MAX_PATH - 1);
            out[at].file[MAX_PATH - 1] = 0;
            if (_snwprintf(out[at].path, MAX_PATH, L"%ls\\%ls", dirs[d], found.cFileName) < 0) out[at].path[0] = 0;
        } while (FindNextFileW(h, &found));
        FindClose(h);
    }
    qsort(out, (size_t)n, sizeof *out, candidate_order);
    return n;
}

static int valid_name(const char *name) {
    const size_t len = strlen(name);
    if (!len || len > NAME_MAX_LEN) return 0;
    for (const char *c = name; *c; ++c)
        if (!((*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9') || *c == '_' || *c == '-'))
            return 0;
    return 1;
}

/* Takes away everything a plugin registered, then the DLL: its callbacks must never run once
 * its code is gone. */
static void unload(int index, const char *why) {
    Plugin *p = &g_plugins[index];
    if (!p->live) return;
    AcquireSRWLockExclusive(&g_lock);
    int kept = 0;
    for (int i = 0; i < g_frame_count; ++i)
        if (g_frames[i].owner != index) g_frames[kept++] = g_frames[i];
    g_frame_count = kept;
    ReleaseSRWLockExclusive(&g_lock);
    host_log("%s: %s; unloaded", p->name, why);
    FreeLibrary(p->dll);
    p->dll = NULL;
    p->live = 0;
}

static void load_one(const Candidate *c) {
    char file[MAX_PATH * 3];
    to_utf8(c->path, file, sizeof file);
    if (g_count == MAX_PLUGINS) {
        host_log("%s: left out, %d plugins are the most this host loads", file, MAX_PLUGINS);
        return;
    }
    /* The plugin's own folder is searched first for the DLLs it depends on. */
    HMODULE dll = LoadLibraryExW(c->path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!dll) {
        host_log("%s: could not be loaded (Windows error %lu)", file, (unsigned long)GetLastError());
        return;
    }
    Plugin p = {.dll = dll, .live = 1};
    snprintf(p.file, sizeof p.file, "%s", file);
    p.info = (const BbPluginInfo *)(void *)GetProcAddress(dll, "bb_plugin_info");
    p.init = (int (*)(const BbHostApi *))(void *)GetProcAddress(dll, "bb_plugin_init");
    p.image = (int (*)(const BbHostApi *))(void *)GetProcAddress(dll, "bb_plugin_image");
    const char *why = NULL;
    char stem[MAX_PATH * 3];
    to_utf8(c->file, stem, sizeof stem);
    char *dot = strrchr(stem, '.');
    if (dot) *dot = 0;
    snprintf(p.name, sizeof p.name, "%s", p.info && p.info->name && *p.info->name ? p.info->name : stem);
    if (!p.init && !p.image) why = "exports neither bb_plugin_init nor bb_plugin_image: not a plugin";
    else if (!valid_name(p.info && p.info->name && *p.info->name ? p.info->name : stem))
        why = "its name must be 1-63 letters, digits, '_' or '-'";
    else if (p.info && p.info->api_version > BB_PLUGIN_API_VERSION)
        why = "built for a newer plugin API than this host's";
    else {
        for (int i = 0; i < g_count; ++i)
            if (g_plugins[i].live && !_stricmp(g_plugins[i].name, p.name)) why = "another plugin has its name";
    }
    if (!why) {
        const uint32_t flags = p.info ? p.info->flags : 0;
        const int on = ini_switch(p.name);
        if (on == 0) why = "off in plugins.ini";
        else if (on < 0 && (flags & BB_PLUGIN_OPT_IN)) why = "off until turned on in plugins.ini (opt-in)";
        else if ((flags & BB_PLUGIN_GAMEPLAY) && online_play()) why = "changes the game, and this is online play";
    }
    if (why) {
        if (p.info && p.info->api_version > BB_PLUGIN_API_VERSION)
            host_log("%s (%s): left out, %s (%u > %u)", p.name, file, why, (unsigned)p.info->api_version,
                     (unsigned)BB_PLUGIN_API_VERSION);
        else
            host_log("%s (%s): left out, %s", p.name, file, why);
        FreeLibrary(dll);
        return;
    }
    g_plugins[g_count++] = p;
    host_log("%s %s%s(%s)", p.name, p.info && p.info->version ? p.info->version : "",
             p.info && p.info->version ? " " : "", file);
}

/* Runs one start phase for every live plugin; a nonzero return unloads that plugin. */
static void run_phase(enum Phase phase) {
    g_phase = phase;
    for (int i = 0; i < g_count; ++i) {
        Plugin *p = &g_plugins[i];
        int (*fn)(const BbHostApi *) = phase == PHASE_INIT ? p->init : p->image;
        if (!p->live || !fn) continue;
        t_current = i;
        const int result = fn(&g_api);
        t_current = -1;
        if (result) {
            char why[64];
            snprintf(why, sizeof why, "%s returned %d", phase == PHASE_INIT ? "bb_plugin_init" : "bb_plugin_image",
                     result);
            unload(i, why);
        }
    }
}

int bb_plugins_install(void) {
    if (env_is("BB_PLUGINS", "0")) {
        host_log("none loaded (BB_PLUGINS=0)");
        return 0;
    }
    fill_api();
    ini_load();
    Candidate found[MAX_PLUGINS * 2];
    const int n = scan(found, (int)(sizeof found / sizeof *found));
    for (int i = 0; i < n; ++i) load_one(&found[i]);
    if (!g_count) {
        if (n) host_log("none of %d DLL(s) loaded", n);
        return 0;
    }
    run_phase(PHASE_INIT);
    run_phase(PHASE_IMAGE);
    g_phase = PHASE_RUNNING;
    int live = 0;
    for (int i = 0; i < g_count; ++i) live += g_plugins[i].live;
    if (live && !g_tick_registered) {
        if (bb_main_frame_available()) {
            bb_on_main_frame(bb_plugins_frame);
            g_tick_registered = 1;
        } else {
            host_log("the once-a-frame tick is not available: on_frame callbacks will not run");
        }
    }
    host_log("%d loaded", live);
    return live;
}

void bb_plugins_frame(void) {
    FrameCallback now[MAX_CALLBACKS];
    AcquireSRWLockShared(&g_lock);
    const int n = g_frame_count;
    memcpy(now, g_frames, (size_t)n * sizeof *now);
    ReleaseSRWLockShared(&g_lock);
    for (int i = 0; i < n; ++i) {
        t_current = now[i].owner;
        now[i].fn(now[i].user);
    }
    t_current = -1;
}

void bb_plugins_reset(void) {
    for (int i = 0; i < g_count; ++i)
        if (g_plugins[i].live) {
            FreeLibrary(g_plugins[i].dll);
            g_plugins[i].live = 0;
        }
    g_count = 0;
    g_frame_count = 0;
    g_phase = PHASE_NONE;
    g_ini_count = 0;
}
