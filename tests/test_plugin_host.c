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

typedef int64_t (BB_GUEST_ABI *IntFn)(int64_t, int64_t);
typedef double (BB_GUEST_ABI *VecFn)(double, double);

/* Scenario 6's functions, at the offsets tests/plugins/hook_plugin.c hooks. */
enum { SUM = 0x1000, FADD = 0x1100, MUL = 0x1200, ARG7 = 0x1300, JUMPS = 0x1400, SUM_COPY = 0x1500,
       CHAIN = 0x1600, OVER = 0x1700, PATCHED = 0x1800, AL = 0x1900, CHAIN2 = 0x1a00, RIPJUMP = 0x1b00,
       TCB = 0x1c00, TCB_CUT = 0x1d00, PATCHED2 = 0x1e00, SKIPPED = 0x1f00, TCB_EARLY = 0x2000 };
/* sum(a, b) = a + 2b: push rbp; mov rbp, rsp; lea rax, [rdi+rsi*2]; pop rbp; ret */
static const unsigned char sum_code[] = {0x55, 0x48, 0x89, 0xe5, 0x48, 0x8d, 0x04, 0x77, 0x5d, 0xc3};
/* fadd(x, y) = x + y: addsd xmm0, xmm1; ret (the whole function is the displaced prologue) */
static const unsigned char fadd_code[] = {0xf2, 0x0f, 0x58, 0xc1, 0xc3};
/* mul(a, b) = a * b: push rbp; mov rbp, rsp; mov rax, rdi; imul rax, rsi; pop rbp; ret */
static const unsigned char mul_code[] = {0x55, 0x48, 0x89, 0xe5, 0x48, 0x89, 0xf8, 0x48, 0x0f, 0xaf, 0xc6, 0x5d, 0xc3};
/* arg7(...) = the 7th integer argument: mov rax, [rsp+8]; ret */
static const unsigned char arg7_code[] = {0x48, 0x8b, 0x44, 0x24, 0x08, 0xc3};
/* al(...) = al at entry, the vector-register count a variadic call carries: movzx eax, al; nop;
 * nop; ret. Hooked, it shows whether the stub gives rax back. */
static const unsigned char al_code[] = {0x0f, 0xb6, 0xc0, 0x90, 0x90, 0xc3};
typedef int64_t (BB_GUEST_ABI *VarFn)(int64_t, ...);
/* tcb() = the guest TCB pointer as the game reads it: push rbp; mov rbp, rsp; mov rax, gs:[0];
 * pop rbp; ret. The 0 displacement is what probe.c's patch_tcb_loads later fills in. */
static const unsigned char tcb_code[] = {0x55, 0x48, 0x89, 0xe5, 0x65, 0x48, 0x8b, 0x04, 0x25, 0, 0, 0, 0, 0x5d, 0xc3};
typedef uint64_t (BB_GUEST_ABI *TcbFn)(void);

static long long call_result[5];
static int call_status = -1;
static DWORD WINAPI call_off_main(LPVOID fn) {
    int (*call)(long long *);
    memcpy(&call, &fn, sizeof call);
    call_status = call(call_result);
    return 0;
}

typedef const long long *(*ResultsFn)(void);

#ifndef PLUGIN_DIR_A
#error PLUGIN_DIR_A, PLUGIN_DIR_B and PLUGIN_DIR_C name the test plugin folders
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
    set_env("BB_EBOOT_SHA256", "00ff00ff");
    set_env("BB_ONLINE", "");
    set_env("BB_PLUGINS", "");

    /* Executable: scenario 6 calls the hooked functions written into it. */
    unsigned char *image = VirtualAlloc(NULL, IMAGE_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    assert(image);
    memset(image, 0x90, IMAGE_SIZE);
    CHECK(bb_hooks_init(image, IMAGE_SIZE) == 0);
    bb_image_code(0, 0x8000);       /* the fake image's "text segment" */
    bb_image_code(0xa000, 0x1000);  /* and a second one, after data */

    /* Written now: scenario 1 already tries a hook over a TCB load (refused, no displacement yet). */
    memcpy(image + TCB_EARLY, tcb_code, sizeof tcb_code);

    /* 1. Everything in its default state. */
    write_ini(user, "");
    CHECK(bb_plugins_install() == 2);  /* good_b (folder B's good.dll) and gameplay */
    /* The later folder's file replaced the earlier's: variant 1 calls itself "good", so had both
     * loaded the count would be 3. */
    CHECK(variant_of("good.dll") == 2);
    CHECK(!loaded("badname.dll"));    /* an invalid name */
    CHECK(!loaded("zdup.dll"));       /* "gameplay" twice: the later file is left out */
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
    CHECK(r[40] == 1 && r[41] == 1 && r[42] == 1 && r[43] == 1 && r[44] == 0); /* and the rest */
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
    CHECK(r[20] == 1 && r[21] == -1 && r[22] == 1 && r[23] == 0); /* hook (no callback), replace_text, hle, symbol */
    CHECK(r[24] == 1 && r[25] == 1 && r[26] == 1 && r[27] == 1); /* plugin_dir, host.name, no escape */
    CHECK(r[28] == 1 && r[29] == 0 && r[31] == 1 && r[32] == 1); /* write: code refused, data written */
    CHECK(r[30] == 0);                                           /* patch: already patched is done */
    CHECK(r[33] == 1);                                           /* eboot_sha256 from the image phase */
    CHECK(r[36] == -1);                                          /* 1.09: the non-1.09 hook was not tried */
    CHECK(r[37] == 2 && image[TCB_EARLY] == 0x55);               /* TCB load, displacement unknown: refused */
    CHECK(r[34] == 1 && r[35] == 0);                             /* write: data into code refused */
    char dir[MAX_PATH];
    snprintf(dir, sizeof dir, "%s\\plugins\\good_b", user);
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
    /* A BOM straight before the section, as editors that save one write it. */
    write_ini(user, "\xef\xbb\xbf[plugins]\r\n# comment\r\ngood_b = 0\r\noptin=true\r\n\r\n[optin]\r\ncolor = red\r\n");
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
    CHECK(r[36] == 1);                           /* ...and no hook */
    CHECK(image[0x1000] == 0x90);
    CHECK(image[0x10] == 0x90);
    bb_plugins_reset();

    /* 6. Hooks and calls into the game, from folder C's plugins. */
    set_env("BB_EBOOT_109", "1");
    set_env("BB_PLUGIN_DIRS", PLUGIN_DIR_C);
    memcpy(image + SUM, sum_code, sizeof sum_code);
    memcpy(image + FADD, fadd_code, sizeof fadd_code);
    memcpy(image + MUL, mul_code, sizeof mul_code);
    memcpy(image + ARG7, arg7_code, sizeof arg7_code);
    memcpy(image + SUM_COPY, sum_code, sizeof sum_code);  /* unhooked: only the length can refuse it */
    memcpy(image + CHAIN, sum_code, sizeof sum_code);
    memcpy(image + CHAIN2, sum_code, sizeof sum_code);
    memcpy(image + OVER, sum_code, sizeof sum_code);
    memcpy(image + PATCHED, sum_code, sizeof sum_code);
    memcpy(image + AL, al_code, sizeof al_code);
    /* bytes 4-7 rewritten (with themselves) as one of the port's own patches or mid-hooks would:
     * a plugin hook over them must be refused even with a NULL prologue */
    CHECK(bb_patch(PATCHED + 4, sum_code + 4, sum_code + 4, 4) == 0);
    memcpy(image + TCB, tcb_code, sizeof tcb_code);
    memcpy(image + TCB_CUT, tcb_code, sizeof tcb_code);
    memcpy(image + PATCHED2, sum_code, sizeof sum_code);
    memcpy(image + SKIPPED, sum_code, sizeof sum_code);
    /* An engine skip site over sum's lea; pop rbp (a mid-hook, recorded through divert). */
    const BbCode lea_pop = {5, bb_code_hash(sum_code + 4, 5)};
    CHECK(bb_hook_skip(SKIPPED + 4, lea_pop) != NULL);
    /* The guest TCB in a real TEB TLS slot, as runtime_thread.c keeps it: gs:[0x1480 + 8 * slot]. */
    const DWORD slot = TlsAlloc();
    CHECK(slot < 64);
    TlsSetValue(slot, (void *)(uintptr_t)0x7e57c0de);
    bb_hooks_set_tcb_displacement(0x1480 + 8 * slot);
    /* jmp rel32 to sum: what a hook another tool placed looks like */
    image[JUMPS] = 0xe9;
    const int32_t to_sum = SUM - (JUMPS + 5);
    memcpy(image + JUMPS + 1, &to_sum, 4);
    /* jmp [rip+0] with its 8-byte target: what Hunter's Atlas lamp travel writes */
    image[RIPJUMP] = 0xff;
    image[RIPJUMP + 1] = 0x25;
    memset(image + RIPJUMP + 2, 0, 4);
    const uint64_t to_sum_abs = (uint64_t)(uintptr_t)(image + SUM);
    memcpy(image + RIPJUMP + 6, &to_sum_abs, 8);
    CHECK(bb_plugins_install() == 2);  /* hooker and hooker2; hookfail unloaded */
    CHECK(!loaded("hookfail.dll"));
    ResultsFn results1 = (ResultsFn)export_of("hooker.dll", "hook_results");
    ResultsFn results2 = (ResultsFn)export_of("hooker2.dll", "hook_results");
    CHECK(results1 && results2);
    const long long *h1 = results1(), *h2 = results2();
    CHECK(h1[20] == 2 && h2[20] == 2);                 /* hook2 in bb_plugin_init: refused */
    CHECK(h1[0] == 0 && h1[1] == 0 && h1[2] == 0);     /* sum, fadd (NULL prologue), mul */
    CHECK(h1[3] == 1 && h1[4] == 1 && h1[5] == 1 && h1[6] == 1); /* wrong bytes, short, outside, no fn */
    CHECK(h1[7] == 1 && image[JUMPS] == 0xe9);         /* a NULL prologue never takes a jump */
    CHECK(h1[16] == 1 && image[RIPJUMP] == 0xff);      /* ...nor a jmp [rip] */
    CHECK(image[SUM_COPY] == 0x55);                    /* the short hook left it alone */
    CHECK(h2[0] == 0 && h2[1] == 2 && h2[2] == 2 && h2[3] == 1); /* chain; other lengths; not original */
    CHECK(h1[8] == 0 && h1[9] == 0 && h1[13] == 0 && h1[15] == 0); /* chain, over, al, chain2 */
    CHECK(h1[14] == 1);                                /* call_guest in bb_plugin_image: refused */
    CHECK(h2[4] == 0 && h2[5] == 0);                   /* v1 joined chain's, hook2 chain2's */
    CHECK(h2[6] == 2 && h2[7] == 2);                   /* inside another hook, over a patch: refused */
    CHECK(image[PATCHED] == 0x55);                     /* the refused hook left the code alone */
    CHECK(h1[17] == 0 && h1[18] == 2 && h1[19] == 0);  /* TCB hooked; the cut refused; patch() */
    CHECK(h2[8] == 2 && h2[9] == 2);                   /* over a plugin's patch(), over a skip site */
    CHECK(image[SUM] == 0xe9 && image[SUM + 5] == 0xcc);  /* jmp to the stub, the rest never run */
    bb_hooks_seal();  /* the stubs become executable; no hook can be placed after this */

    IntFn sum = (IntFn)(void *)(image + SUM), mul = (IntFn)(void *)(image + MUL);
    VecFn fadd = (VecFn)(void *)(image + FADD);
    CHECK(sum(2, 3) == 8);
    CHECK(h1[10] == 1 && h2[10] == 1 && h2[11] == 2 && h2[12] == 3);  /* both ran, in order */
    CHECK(sum(50, 3) == 2050);                       /* hooker made b 1000... */
    CHECK(h2[11] == 50 && h2[12] == 1000);           /* ...and hooker2, later in the chain, saw it */
    CHECK(sum(100, 3) == 7);                         /* hooker skipped with 7 */
    CHECK(h1[10] == 3 && h2[10] == 2);               /* hooker2 does not run after a skip */
    CHECK(sum(1, 1) == 3);                           /* hookfail's callback (a = 999999) never ran */
    CHECK(fadd(1.5, 2.0) == 11.5);                   /* y rewritten in xmm1 */
    CHECK(fadd(2.5, 1.0) == 99.0);                   /* skipped: the value in xmm0 */
    CHECK(fadd(4.0, 0.5) == 4.5);
    CHECK(mul(6, 7) == 42 && h1[11] == 6 && h1[12] == 7);  /* the v1 frame */
    CHECK(mul(13, 2) == 0);                          /* v1 skip: the caller gets 0 */
    IntFn chain = (IntFn)(void *)(image + CHAIN), chain2 = (IntFn)(void *)(image + CHAIN2);
    IntFn over = (IntFn)(void *)(image + OVER);
    CHECK(chain(21, 1) == 0);                        /* hooker set 55 and ran on; the v1 skip gives 0 */
    CHECK(chain(1, 1) == 3);
    CHECK(chain2(22, 1) == 0);                       /* hooker set 66; hooker2's hook2 skip starts at 0 */
    CHECK(chain2(1, 1) == 3);
    CHECK(over(2, 3) == 8);                          /* the refused overlap left hooker's hook working */
    /* The hooked function's TCB load reads the slot, through the stub's rewritten copy. */
    TcbFn tcb = (TcbFn)(void *)(image + TCB);
    CHECK(tcb() == 0x7e57c0de);
    VarFn al = (VarFn)(void *)(image + AL);
    CHECK(al(0, 1.0, 2.0, 3.0) == 3);                /* three vector registers: al 3, through the stub */

    int (*late)(void) = (int (*)(void))export_of("hooker.dll", "hooker_late_hook");
    CHECK(late && late() == 2);                      /* sealed: no new hooks */
    int (*call)(long long *) = (int (*)(long long *))export_of("hooker.dll", "hooker_call");
    CHECK(call && call(call_result) == 0);
    CHECK(call_result[0] == 11 && call_result[1] == 115 && call_result[2] == 42 && call_result[3] == 1);
    CHECK(call_result[4] == 8);                      /* call_guest sets al for a variadic callee */
    void *call_arg;
    memcpy(&call_arg, &call, sizeof call_arg);
    HANDLE t = CreateThread(NULL, 0, call_off_main, call_arg, 0, NULL);  /* off the main thread */
    WaitForSingleObject(t, INFINITE);
    CloseHandle(t);
    CHECK(call_status != 0);
    bb_plugins_reset();

    printf("plugin host: %d checks passed\n", checks);
    return 0;
}
