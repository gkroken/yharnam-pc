/* SPDX-License-Identifier: GPL-3.0-or-later
 * plugin_host.c against the test plugins (tests/plugins/test_plugin.c) on a small fake image:
 * which plugins load and which are left out, and why; the folders' override order; the
 * lifecycle (init before image, image functions refused before the image phase, a failing
 * phase unloads the plugin together with what it registered); the frame tick; the memory
 * functions on the image and on runtime addresses; the API functions this host lacks answering
 * with their failure values; plugins.ini, BB_PLUGINS=0 and online play. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "bbhost_plugin.h"
#include "engine_hooks.h"
#include "plugin_host.h"

#ifndef PLUGIN_DIR_A
#error PLUGIN_DIR_A and PLUGIN_DIR_B name the test plugin folders
#endif

enum { IMAGE_SIZE = 0x10000 };

static int checks;
#define CHECK(cond)                                                                              \
    do {                                                                                         \
        ++checks;                                                                                \
        if (!(cond)) {                                                                           \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #cond);             \
            exit(1);                                                                             \
        }                                                                                        \
    } while (0)

static void set_env(const char *name, const char *value) {
    char line[1024];
    snprintf(line, sizeof line, "%s=%s", name, value ? value : "");
    _putenv(line);  /* an empty value removes it */
}

static HMODULE loaded(const char *dll) { return GetModuleHandleA(dll); }

static void *export_of(const char *dll, const char *name) {
    HMODULE m = loaded(dll);
    return m ? (void *)GetProcAddress(m, name) : NULL;
}

static int variant_of(const char *dll) {
    int (*fn)(void) = (int (*)(void))export_of(dll, "test_variant");
    return fn ? fn() : -1;
}

static int frames_of(const char *dll) {
    int (*fn)(void) = (int (*)(void))export_of(dll, "test_frames");
    return fn ? fn() : -1;
}

static const long long *results_of(const char *dll) {
    const long long *(*fn)(void) = (const long long *(*)(void))export_of(dll, "test_results");
    return fn ? fn() : NULL;
}

static void write_ini(const char *dir, const char *text) {
    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\plugins.ini", dir);
    FILE *f = fopen(path, "wb");
    assert(f);
    fputs(text, f);
    fclose(f);
}

int main(void) {
    /* A user folder of the test's own: plugins.ini and plugin_dir() go there. */
    char user[MAX_PATH];
    GetTempPathA(MAX_PATH, user);
    snprintf(user + strlen(user), MAX_PATH - strlen(user), "bb-plugin-host-test-%lu", GetCurrentProcessId());
    CreateDirectoryA(user, NULL);
    set_env("BB_GPU_USER_DIR", user);
    set_env("BB_PLUGIN_DIRS", PLUGIN_DIR_A ";" PLUGIN_DIR_B);
    set_env("BB_EBOOT_109", "1");
    set_env("BB_ONLINE", "");
    set_env("BB_PLUGINS", "");

    unsigned char *image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(image);
    memset(image, 0x90, IMAGE_SIZE);
    CHECK(bb_hooks_init(image, IMAGE_SIZE) == 0);

    /* 1. Everything in its default state. */
    write_ini(user, "");
    CHECK(bb_plugins_install() == 2);  /* good (from folder B) and gameplay */
    CHECK(variant_of("good.dll") == 2);  /* the later folder's file replaced the earlier's */
    CHECK(loaded("gameplay.dll"));
    CHECK(!loaded("fails.dll"));      /* failed its image phase: unloaded */
    CHECK(!loaded("initfail.dll"));   /* failed its init phase: unloaded */
    CHECK(!loaded("notplugin.dll"));  /* no entry points */
    CHECK(!loaded("future.dll"));     /* a newer API than this host's */
    CHECK(!loaded("optin.dll"));      /* opt-in, not turned on */

    const long long *r = results_of("good.dll");
    CHECK(r);
    CHECK(r[0] == BB_PLUGIN_API_VERSION && r[1] == (long long)sizeof(BbHostApi));
    CHECK(r[2] == 0);                                            /* guest_addr refused in init */
    CHECK(r[3] == 1);                                            /* eboot_is_109 */
    CHECK(r[4] == 0);                                            /* on_frame */
    CHECK(r[5] == (long long)(uintptr_t)(image + 0x10));         /* guest_addr in the image */
    CHECK(r[6] == 1 && r[7] == 0 && r[8] == 2);                  /* patch: differ, done, outside */
    CHECK(image[0x10] == 0xcc && image[0x11] == 0xcc && image[0x12] == 0x90);
    CHECK(r[9] == 0 && r[10] == 0xcccc);                         /* read through the image */
    CHECK(r[11] == 0 && r[12] == 1);                             /* read of a runtime address */
    CHECK(r[13] == 0 && r[14] == 77);                            /* write to writable memory */
    CHECK(r[15] == 1);                                           /* write to a read-only page */
    CHECK(r[16] == 1 && r[17] == 0);                             /* missing: event_flag_get */
    CHECK(r[18] == 1 && r[19] == 0);                             /* missing: param_row */
    CHECK(r[20] == 2 && r[21] == -1 && r[22] == 1 && r[23] == 0); /* hook, replace_text, hle, symbol */
    CHECK(r[24] == 1 && r[25] == 1 && r[26] == 1);               /* plugin_dir, host.name, no escape */
    char dir[MAX_PATH];
    snprintf(dir, sizeof dir, "%s\\plugins\\good", user);
    CHECK(GetFileAttributesA(dir) & FILE_ATTRIBUTE_DIRECTORY && GetFileAttributesA(dir) != INVALID_FILE_ATTRIBUTES);

    /* Every pointer in the table is set, so no plugin can call a null one. */
    const BbHostApi *(*api_of)(void) = (const BbHostApi *(*)(void))export_of("good.dll", "test_api");
    const BbHostApi *api = api_of();
    const unsigned char *first = (const unsigned char *)&api->log;
    for (const unsigned char *p = first; p + sizeof(void *) <= (const unsigned char *)api + sizeof *api;
         p += sizeof(void *)) {
        void *fn;
        memcpy(&fn, p, sizeof fn);
        CHECK(fn != NULL);
    }

    /* The tick: the live plugins' callbacks, none of the unloaded one's (it would crash). */
    for (int i = 0; i < 3; ++i) bb_plugins_frame();
    CHECK(frames_of("good.dll") == 3);
    CHECK(frames_of("gameplay.dll") == 3);
    bb_plugins_reset();
    CHECK(!loaded("good.dll") && !loaded("gameplay.dll"));

    /* 2. Online play leaves out the plugin that changes the game. */
    set_env("BB_ONLINE", "1");
    CHECK(bb_plugins_install() == 1);
    CHECK(loaded("good.dll") && !loaded("gameplay.dll"));
    bb_plugins_reset();
    /* ...but seamless co-op, which everyone chose, keeps it. */
    set_env("BB_COOP", "1");
    CHECK(bb_plugins_install() == 2);
    bb_plugins_reset();
    set_env("BB_ONLINE", "");
    set_env("BB_COOP", "");

    /* 3. plugins.ini: an opt-in plugin turned on with its option, a plugin turned off. */
    write_ini(user, "\xef\xbb\xbf# comment\r\n[plugins]\r\ngood = 0\r\noptin=true\r\n\r\n[optin]\r\ncolor = red\r\n");
    CHECK(bb_plugins_install() == 2);  /* optin and gameplay */
    CHECK(!loaded("good.dll") && loaded("optin.dll"));
    const char *(*option)(void) = (const char *(*)(void))export_of("optin.dll", "test_option");
    CHECK(option && !strcmp(option(), "red"));
    bb_plugins_reset();

    /* 4. BB_PLUGINS=0 loads none. */
    write_ini(user, "");
    set_env("BB_PLUGINS", "0");
    CHECK(bb_plugins_install() == 0);
    CHECK(!loaded("good.dll") && !loaded("gameplay.dll"));
    set_env("BB_PLUGINS", "");

    /* 5. Without the 1.09 build the image functions refuse. */
    set_env("BB_EBOOT_109", "0");
    memset(image, 0x90, IMAGE_SIZE);
    CHECK(bb_plugins_install() == 2);
    r = results_of("good.dll");
    CHECK(r[3] == 0 && r[5] == 0 && r[7] == 1);  /* not 1.09, no address, patch refused */
    CHECK(image[0x10] == 0x90);
    bb_plugins_reset();

    printf("plugin host: %d checks passed\n", checks);
    return 0;
}
