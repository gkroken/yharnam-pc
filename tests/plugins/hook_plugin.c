/* SPDX-License-Identifier: GPL-3.0-or-later
 * The hook plugins tests/test_plugin_host.c loads (scenario 6), built once per VARIANT:
 *   1 hooker    hook2 on sum (rewrite an argument, or skip with a value), hook2 on fadd (floats,
 *               and a skip returning a double), hook on mul (the v1 frame; skip), refusals, and
 *               call_guest and a late hook2 on request
 *   2 hooker2   joins sum's chain: sees what hooker left; a different length is refused
 *   3 hookfail  hooks sum, then fails bb_plugin_image: its callback must never run
 * Also (port-1840f95): chain and chain2 (hooker sets a value and runs on; next in each chain,
 * hooker2's v1 skip on chain and its hook2 skip on chain2, each straight after it), over (hooker2's NULL-prologue hook inside hooker's jump: refused), patched (a range
 * the test rewrote with bb_patch: refused), al (rax survives the stub; call_guest sets al).
 * The functions are the test's, written into its fake image at the offsets below. */
#include "bbhost_plugin.h"

#include <string.h>

#ifndef VARIANT
#error VARIANT must be set
#endif

#define BN(offset) (0x400000ull + (offset))
enum { SUM = 0x1000, FADD = 0x1100, MUL = 0x1200, ARG7 = 0x1300, JUMPS = 0x1400, SUM_COPY = 0x1500,
       CHAIN = 0x1600, OVER = 0x1700, PATCHED = 0x1800, AL = 0x1900, CHAIN2 = 0x1a00, RIPJUMP = 0x1b00 };
static const unsigned char sum_prologue[8] = {0x55, 0x48, 0x89, 0xe5, 0x48, 0x8d, 0x04, 0x77};
static const unsigned char mul_prologue[7] __attribute__((unused)) = {0x55, 0x48, 0x89, 0xe5, 0x48, 0x89, 0xf8};

static const BbHostApi *g_api;
static long long g_results[64];
BB_PLUGIN_EXPORT const long long *hook_results(void) { return g_results; }
BB_PLUGIN_EXPORT const BbHostApi *hook_api(void) { return g_api; }

static int noop2(BbHookCtx *ctx, void *user) {
    (void)ctx, (void)user;
    return 0;
}

#if VARIANT == 1
static double f64(uint64_t bits) {
    double d;
    memcpy(&d, &bits, 8);
    return d;
}

#define NAME "hooker"
/* sum(a, b) = a + 2b: a == 100 skips with 7; a == 50 makes b 1000. */
static int on_sum(BbHookCtx *ctx, void *user) {
    (void)user;
    ++g_results[10];
    if (ctx->arg[0] == 100) {
        ctx->ret = 7;
        return 1;
    }
    if (ctx->arg[0] == 50) ctx->arg[1] = 1000;
    return 0;
}
/* fadd(x, y) = x + y: x == 1.5 makes y 10; x == 2.5 skips with 99.0. */
static int on_fadd(BbHookCtx *ctx, void *user) {
    (void)user;
    const double x = f64(ctx->xmm[0]);
    if (x == 1.5) ctx->xmm[1] = bb_f64(10.0);
    if (x == 2.5) {
        ctx->ret = bb_f64(99.0);
        return 1;
    }
    return 0;
}
/* mul(a, b) = a * b, through the v1 frame: records it; a == 13 skips (the caller gets 0). */
static int on_mul(const BbHookFrame *frame, void *user) {
    (void)user;
    g_results[11] = (long long)frame->rdi;
    g_results[12] = (long long)frame->rsi;
    return frame->rdi == 13;
}
/* chain(a, b) = a + 2b: a == 21 or 22 sets a return value and runs on (hooker2 decides). */
static int on_chain(BbHookCtx *ctx, void *user) {
    (void)user;
    if (ctx->arg[0] == 21) ctx->ret = 55;
    if (ctx->arg[0] == 22) ctx->ret = 66;
    return 0;
}
BB_PLUGIN_EXPORT int hooker_late_hook(void) {
    return g_api->hook2(BN(ARG7), NULL, 5, on_sum, 0);
}
/* call_guest, as a plugin would make it from on_frame; the results into out. */
BB_PLUGIN_EXPORT int hooker_call(long long out[5]) {
    BbCallRegs r;
    memset(&r, 0, sizeof r);
    r.arg[0] = 3;
    r.arg[1] = 4;
    const int a = g_api->call_guest(BN(SUM), &r);
    out[0] = (long long)r.ret;  /* through sum's hooks: 3 + 8 = 11 */
    memset(&r, 0, sizeof r);
    r.xmm[0] = bb_f64(1.5);
    r.xmm[1] = bb_f64(2.0);
    const int b = g_api->call_guest(BN(FADD), &r);
    out[1] = (long long)(f64(r.ret_xmm0) * 10);  /* y becomes 10 in the hook: 115 */
    memset(&r, 0, sizeof r);
    r.stack[0] = 42;
    const int c = g_api->call_guest(BN(ARG7), &r);
    out[2] = (long long)r.ret;  /* the 7th integer argument: 42 */
    memset(&r, 0, sizeof r);
    out[3] = g_api->call_guest(BN(0x9000), &r);  /* data, not code: 1 */
    memset(&r, 0, sizeof r);
    const int d = g_api->call_guest(BN(AL), &r);
    out[4] = (long long)r.ret;  /* al as call_guest set it: 8 */
    return a | b | c | d;
}
#elif VARIANT == 2
#define NAME "hooker2"
/* On chain, after hooker: a == 21 skips through the v1 frame (the caller must get 0, not 55). */
static int on_chain_v1(const BbHookFrame *frame, void *user) {
    (void)user;
    return frame->rdi == 21;
}
/* ...and a == 22 skips without setting a value (the caller must get 0, not hooker's 66). */
static int on_chain2(BbHookCtx *ctx, void *user) {
    (void)user;
    return ctx->arg[0] == 22;
}
static int on_sum2(BbHookCtx *ctx, void *user) {
    (void)user;
    ++g_results[10];
    g_results[11] = (long long)ctx->arg[0];
    g_results[12] = (long long)ctx->arg[1];
    return 0;
}
#else
#define NAME "hookfail"
static int on_fail(BbHookCtx *ctx, void *user) {
    (void)user;
    ctx->arg[0] = 999999;
    return 0;
}
#endif

BB_PLUGIN_EXPORT const BbPluginInfo bb_plugin_info = {BB_PLUGIN_API_VERSION, 0, NAME, NAME, "1", "tests", "hooks"};

BB_PLUGIN_EXPORT int bb_plugin_init(const BbHostApi *api) {
    g_api = api;
    /* Hooks are for bb_plugin_image. */
    g_results[20] = api->hook2(BN(SUM), sum_prologue, 8, noop2, 0);  /* 2: not the image phase */
    return 0;
}

BB_PLUGIN_EXPORT int bb_plugin_image(const BbHostApi *api) {
#if VARIANT == 1
    static const unsigned char wrong[5] = {1, 2, 3, 4, 5};
    g_results[0] = api->hook2(BN(SUM), sum_prologue, 8, on_sum, 0);
    g_results[1] = api->hook2(BN(FADD), NULL, 5, on_fadd, 0);
    g_results[2] = api->hook(BN(MUL), mul_prologue, 7, on_mul, 0);
    g_results[3] = api->hook2(BN(ARG7), wrong, 5, on_sum, 0);            /* 1: bytes differ */
    g_results[4] = api->hook2(BN(SUM_COPY), sum_prologue, 4, on_sum, 0); /* 1: under 5 bytes (they match) */
    g_results[7] = api->hook2(BN(JUMPS), NULL, 5, on_sum, 0);            /* 1: the bytes there are a jmp */
    g_results[16] = api->hook2(BN(RIPJUMP), NULL, 14, on_sum, 0);        /* 1: a jmp [rip] (Atlas's kind) */
    g_results[5] = api->hook2(BN(0x200000), sum_prologue, 8, on_sum, 0); /* 1: outside the image */
    g_results[6] = api->hook(BN(MUL), mul_prologue, 7, 0, 0);            /* 1: no callback */
    g_results[8] = api->hook2(BN(CHAIN), sum_prologue, 8, on_chain, 0);  /* 0 */
    g_results[15] = api->hook2(BN(CHAIN2), sum_prologue, 8, on_chain, 0); /* 0 */
    g_results[9] = api->hook2(BN(OVER), sum_prologue, 8, noop2, 0);      /* 0 */
    g_results[13] = api->hook2(BN(AL), NULL, 5, noop2, 0);               /* 0: movzx eax, al; nop; nop */
    BbCallRegs r;
    memset(&r, 0, sizeof r);
    g_results[14] = api->call_guest(BN(ARG7), &r);                       /* 1: the game has not started */
    return 0;
#elif VARIANT == 2
    g_results[0] = api->hook2(BN(SUM), sum_prologue, 8, on_sum2, 0);     /* 0: joins the chain */
    g_results[1] = api->hook2(BN(SUM), sum_prologue, 7, on_sum2, 0);     /* 2: another length */
    g_results[2] = api->hook2(BN(SUM), mul_prologue, 7, on_sum2, 0);     /* 2: another length */
    static const unsigned char not_sum[8] = {0x55, 0x48, 0x89, 0xe5, 0x90, 0x90, 0x90, 0x90};
    g_results[3] = api->hook2(BN(SUM), not_sum, 8, on_sum2, 0);          /* 1: not the original */
    g_results[4] = api->hook(BN(CHAIN), sum_prologue, 8, on_chain_v1, 0); /* 0: joins the chain */
    g_results[5] = api->hook2(BN(CHAIN2), sum_prologue, 8, on_chain2, 0); /* 0: joins chain2's chain */
    g_results[6] = api->hook2(BN(OVER + 3), NULL, 5, on_sum2, 0);         /* 2: inside hooker's jump */
    g_results[7] = api->hook2(BN(PATCHED), NULL, 8, on_sum2, 0);          /* 2: over rewritten bytes */
    return 0;
#else
    g_results[0] = api->hook2(BN(SUM), sum_prologue, 8, on_fail, 0);
    return 1;
#endif
}
