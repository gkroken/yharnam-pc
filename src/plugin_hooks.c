/* SPDX-License-Identifier: GPL-3.0-or-later
 * Plugin hooks (bbhost's hook and hook2) and calls into the game (call_guest), for the plugin
 * host. The semantics follow bbhost's src/host/plugins.cpp (droogie, GPL-3.0-or-later); the stub
 * and the chains are this port's.
 *
 * A hooked function's first n bytes become a jump to a stub in the engine hooks' arena
 * (engine_hooks.c, within rel32 reach). The stub saves the argument registers (rdi rsi rdx rcx r8
 * r9, rax for variadic callers, xmm0-7) in a frame on the stack and calls dispatch(site, frame),
 * which runs the site's chain of plugin callbacks in load order. Then either:
 *   - run: the registers are reloaded from the frame (a hook2 callback may have rewritten them),
 *     the displaced prologue runs from the stub, and execution continues after it; or
 *   - skip: the function never runs; its caller gets the chosen value in rax, and also in xmm0's
 *     low 64 bits (bbhost sets rax only: a function returning a float then returns garbage).
 *
 * At a function's entry rsp is 8 mod 16 and the caller's red zone is already given up (its call
 * wrote there), so the stub may use the stack freely. */
#include "plugin_hooks.h"

#include "engine_hooks.h"

#include <string.h>

enum { MAX_SITES = 128, MAX_ENTRIES = 256, MAX_PROLOGUE = 64 };

/* The stub's frame, at rsp after its sub. */
enum {
    FRAME_GPR = 0x00,  /* rdi rsi rdx rcx r8 r9 rax: 7 x 8 */
    FRAME_RET = 0x38,  /* the value a skip returns */
    FRAME_XMM = 0x40,  /* xmm0-7, 16 bytes each */
    FRAME_SIZE = 0xc8, /* 8 (entry) + 0xc8 = 0xd0: 16-aligned for the call */
};

typedef struct {
    uint64_t offset;
    size_t n;
    unsigned char original[MAX_PROLOGUE];  /* the function's bytes before the first hook */
} Site;

typedef struct {
    int site, owner, live;
    BbHookFn fn;
    BbHookFn2 fn2;
    void *user;
} Entry;

static Site g_sites[MAX_SITES];
static int g_site_count;
static Entry g_entries[MAX_ENTRIES];
static int g_entry_count;
static int (*g_swap)(int owner);

void bb_plugin_hooks_set_current(int (*swap)(int owner)) { g_swap = swap; }

/* --- the dispatcher ---------------------------------------------------------------------------- */

static BB_GUEST_ABI int dispatch(uint64_t site, unsigned char *frame) {
    uint64_t *gpr = (uint64_t *)(frame + FRAME_GPR);
    int skip = 0;
    uint64_t ret = 0;
    for (int i = 0; i < g_entry_count && !skip; ++i) {
        const Entry *e = &g_entries[i];
        if (e->site != (int)site || !e->live) continue;
        const int was = g_swap ? g_swap(e->owner) : -1;
        if (e->fn2) {
            BbHookCtx ctx;
            memcpy(ctx.arg, gpr, sizeof ctx.arg);
            for (int x = 0; x < 8; ++x) memcpy(&ctx.xmm[x], frame + FRAME_XMM + 16 * x, 8);
            ctx.ret = 0;
            skip = e->fn2(&ctx, e->user) != 0;
            /* The next callback in the chain, and the function, see what this one left. */
            memcpy(gpr, ctx.arg, sizeof ctx.arg);
            for (int x = 0; x < 8; ++x) memcpy(frame + FRAME_XMM + 16 * x, &ctx.xmm[x], 8);
            ret = ctx.ret;
        } else {
            const BbHookFrame f = {gpr[0], gpr[1], gpr[2], gpr[3], gpr[4], gpr[5]};
            skip = e->fn(&f, e->user) != 0;
            ret = 0;
        }
        if (g_swap) g_swap(was);
    }
    if (skip) memcpy(frame + FRAME_RET, &ret, 8);
    return skip;
}

/* --- the stub -------------------------------------------------------------------------------------- */

static unsigned char *emit(unsigned char *p, const void *bytes, size_t n) {
    memcpy(p, bytes, n);
    return p + n;
}

/* mov [rsp+disp32], r64 (store) or mov r64, [rsp+disp32] (load); reg is 0-15. */
static unsigned char *gpr_frame(unsigned char *p, int reg, int32_t disp, int store) {
    *p++ = (unsigned char)(0x48 | (reg >= 8 ? 4 : 0));
    *p++ = store ? 0x89 : 0x8b;
    *p++ = (unsigned char)(0x84 | (reg & 7) << 3);
    *p++ = 0x24;
    return emit(p, &disp, 4);
}

/* movdqu [rsp+disp32], xmm (store) or movdqu xmm, [rsp+disp32] (load); xmm is 0-7. */
static unsigned char *xmm_frame(unsigned char *p, int xmm, int32_t disp, int store) {
    *p++ = 0xf3;
    *p++ = 0x0f;
    *p++ = store ? 0x7f : 0x6f;
    *p++ = (unsigned char)(0x84 | xmm << 3);
    *p++ = 0x24;
    return emit(p, &disp, 4);
}

static void put_rel32(unsigned char *field, const unsigned char *next, const unsigned char *to) {
    const int32_t rel = (int32_t)((intptr_t)to - (intptr_t)next);
    memcpy(field, &rel, 4);
}

/* The registers in the frame's order: rdi rsi rdx rcx r8 r9 rax. */
static const int FRAME_REGS[7] = {7, 6, 2, 1, 8, 9, 0};

static unsigned char *build_stub(int site_index, unsigned char *site, size_t n) {
    /* save 7*8 + xmm 8*9 + calls ~40 + restore 7*8 + 8*9 + prologue + jumps: 400 bytes is plenty. */
    unsigned char *s = bb_hook_code(400 + n);
    if (!s) return NULL;
    unsigned char *p = s;
    static const unsigned char sub[] = {0x48, 0x81, 0xec, FRAME_SIZE, 0x00, 0x00, 0x00};   /* sub rsp, 0xc8 */
    static const unsigned char add[] = {0x48, 0x81, 0xc4, FRAME_SIZE, 0x00, 0x00, 0x00};   /* add rsp, 0xc8 */
    p = emit(p, sub, sizeof sub);
    for (int i = 0; i < 7; ++i) p = gpr_frame(p, FRAME_REGS[i], FRAME_GPR + 8 * i, 1);
    for (int x = 0; x < 8; ++x) p = xmm_frame(p, x, FRAME_XMM + 16 * x, 1);
    const uint64_t id = (uint64_t)site_index;
    *p++ = 0x48; *p++ = 0xbf; p = emit(p, &id, 8);                    /* movabs rdi, site */
    *p++ = 0x48; *p++ = 0x89; *p++ = 0xe6;                            /* mov rsi, rsp */
    const uint64_t target = (uint64_t)(uintptr_t)dispatch;
    *p++ = 0x49; *p++ = 0xbb; p = emit(p, &target, 8);                /* movabs r11, dispatch */
    *p++ = 0x41; *p++ = 0xff; *p++ = 0xd3;                            /* call r11 */
    *p++ = 0x85; *p++ = 0xc0;                                         /* test eax, eax */
    *p++ = 0x0f; *p++ = 0x85;                                         /* jnz skip (rel32, below) */
    unsigned char *to_skip = p;
    p += 4;
    for (int i = 0; i < 7; ++i) p = gpr_frame(p, FRAME_REGS[i], FRAME_GPR + 8 * i, 0);
    for (int x = 0; x < 8; ++x) p = xmm_frame(p, x, FRAME_XMM + 16 * x, 0);
    p = emit(p, add, sizeof add);
    unsigned char *copy = p;
    p = emit(p, g_sites[site_index].original, n);                    /* the displaced prologue, */
    bb_hook_fix_tcb(copy, n);                                         /* its TCB loads rewritten */
    *p++ = 0xe9;                                                      /* jmp site + n */
    put_rel32(p, p + 4, site + n);
    p += 4;
    put_rel32(to_skip, to_skip + 4, p);
    p = gpr_frame(p, 0, FRAME_RET, 0);                                /* skip: mov rax, [rsp+ret] */
    *p++ = 0x66; *p++ = 0x48; *p++ = 0x0f; *p++ = 0x6e; *p++ = 0xc0;  /* movq xmm0, rax */
    p = emit(p, add, sizeof add);
    *p++ = 0xc3;                                                      /* ret */
    return s;
}

/* --- hooks ------------------------------------------------------------------------------------------- */

int bb_plugin_hook(uint64_t offset, const void *prologue, size_t n, BbHookFn fn, BbHookFn2 fn2, void *user,
                   int owner) {
    if ((!fn) == (!fn2) || n < 5 || n > MAX_PROLOGUE) return 1;
    unsigned char *image = bb_guest(0);
    const size_t size = bb_image_size();
    if (!image || offset > size || n > size - offset) return 1;
    if (bb_hooks_sealed()) return 2;
    int site = -1;
    for (int i = 0; i < g_site_count; ++i)
        if (g_sites[i].offset == offset) site = i;
    if (site >= 0) {
        /* Hooked already, by a plugin: join the chain, checked against the original bytes. */
        if (n != g_sites[site].n) return 2;
        if (prologue && memcmp(prologue, g_sites[site].original, n)) return 1;
    } else if (bb_image_is_rewritten(offset, n)) {
        /* Another hook or a patch rewrote some of these bytes (the port's own, another plugin's
         * at a nearby offset, a patch()): hooking over it would cut its jump or copy a moved
         * one. Whatever the prologue argument says. */
        return 2;
    } else {
        if (prologue && memcmp(image + offset, prologue, n)) return 1;
        /* Taking the bytes as they are: never a jump or call another hook left there, which would
         * point elsewhere once copied into the stub: rel32 (E9/E8, the engine hooks') or through
         * rip (FF 25 / FF 15: Hunter's Atlas lamp travel writes one at the frame manager's entry
         * before plugins load, runtime_atlas_state.c, and it records no range). */
        const unsigned char *b = image + offset;
        if (!prologue && (b[0] == 0xe9 || b[0] == 0xe8 || (b[0] == 0xff && (b[1] == 0x25 || b[1] == 0x15)))) return 1;
        if (g_site_count == MAX_SITES) return 2;
        site = g_site_count;
        g_sites[site].offset = offset;
        g_sites[site].n = n;
        memcpy(g_sites[site].original, image + offset, n);
    }
    if (g_entry_count == MAX_ENTRIES) return 2;
    if (site == g_site_count) {
        /* The prologue's TCB loads get the displacement the image's own get later (probe.c):
         * refused when that is unknown or a load is cut off. port-aedd07c-F1. */
        unsigned char check[MAX_PROLOGUE];
        memcpy(check, g_sites[site].original, n);
        if (bb_hook_fix_tcb(check, n) < 0) return 2;
        unsigned char *stub = build_stub(site, image + offset, n);
        if (!stub) return 2;
        unsigned char *at = image + offset;
        at[0] = 0xe9;
        put_rel32(at + 1, at + 5, stub);
        memset(at + 5, 0xcc, n - 5);  /* never run: the stub continues after them */
        bb_image_rewritten(offset, n);
        ++g_site_count;
    }
    g_entries[g_entry_count++] = (Entry){site, owner, 1, fn, fn2, user};
    return 0;
}

void bb_plugin_hooks_drop(int owner) {
    for (int i = 0; i < g_entry_count; ++i)
        if (g_entries[i].owner == owner) g_entries[i].live = 0;
}

void bb_plugin_hooks_reset(void) {
    g_entry_count = 0;
    /* The sites stay: their stubs are in the code. A reset hook test starts from new offsets. */
}

/* --- calls into the game -------------------------------------------------------------------------------- */

typedef struct {
    uint64_t rax;
    double xmm0;  /* System V returns {INTEGER, SSE} in rax and xmm0 */
} GuestResult;

/* Variadic, so the compiler sets al to the number of vector registers used (8), as System V
 * wants for a variadic callee; the arguments land where they would without the "...". */
typedef BB_GUEST_ABI GuestResult (*GuestFn)(uint64_t, ...);

static double as_double(uint64_t bits) {
    double d;
    memcpy(&d, &bits, 8);
    return d;
}

void bb_plugin_call(void *target, BbCallRegs *regs) {
    /* Six integer arguments go in rdi..r9 and eight doubles in xmm0-7, whatever their order in
     * the prototype; the four integers after them go on the stack, the 7th argument first. */
    const GuestResult r = ((GuestFn)target)(
        regs->arg[0], regs->arg[1], regs->arg[2], regs->arg[3], regs->arg[4], regs->arg[5], as_double(regs->xmm[0]),
        as_double(regs->xmm[1]), as_double(regs->xmm[2]), as_double(regs->xmm[3]), as_double(regs->xmm[4]),
        as_double(regs->xmm[5]), as_double(regs->xmm[6]), as_double(regs->xmm[7]), regs->stack[0], regs->stack[1],
        regs->stack[2], regs->stack[3]);
    regs->ret = r.rax;
    memcpy(&regs->ret_xmm0, &r.xmm0, 8);
}
