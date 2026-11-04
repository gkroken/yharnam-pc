/* SPDX-License-Identifier: GPL-3.0-or-later
 * Plugin hooks and calls into the game (plugin_hooks.c), used by plugin_host.c for the API's
 * hook, hook2 and call_guest. Internal to the plugin host. */
#ifndef BB_PLUGIN_HOOKS_H
#define BB_PLUGIN_HOOKS_H

#include <stddef.h>
#include <stdint.h>

#include "bbhost_plugin.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Hooks a function's entry at image offset `offset` for plugin `owner` (fn or fn2: one of them).
 * `prologue` (NULL: the bytes there) must be the function's first n bytes, 5 <= n <= 64, whole
 * instructions, none rip-relative or a branch. A second hook at the same function joins its chain
 * (load order; the first that skips decides), its prologue checked against the original bytes.
 * Returns 0, 1 when the bytes differ (or the arguments are bad), 2 when it can't be placed
 * (sealed, out of stub space, or a different length from the first hook's). Before the seal. */
int bb_plugin_hook(uint64_t offset, const void *prologue, size_t n, BbHookFn fn, BbHookFn2 fn2, void *user,
                   int owner);

/* Stops calling everything plugin `owner` hooked (its DLL is about to go): the stubs stay in
 * the code, the chain skips the owner's entries. */
void bb_plugin_hooks_drop(int owner);

/* How the dispatcher tells plugin_host.c whose callback runs on this thread (for its log lines
 * and what it registers): swap(owner) makes owner current and returns who was. */
void bb_plugin_hooks_set_current(int (*swap)(int owner));

/* Calls the game function at `target` (a host pointer into the image) with regs: rdi..r9, the
 * low 64 bits of xmm0-7, four stack arguments; fills regs->ret and ret_xmm0. */
void bb_plugin_call(void *target, BbCallRegs *regs);

/* Tests: forgets every hook (the stubs stay where they are). */
void bb_plugin_hooks_reset(void);

#ifdef __cplusplus
}
#endif

#endif
