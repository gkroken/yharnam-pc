/* SPDX-License-Identifier: GPL-3.0-or-later
 * Native plugins (include/bbhost_plugin.h, bbhost's plugin API): DLLs from the plugins folders,
 * loaded once at start while the engine hooks are still open (engine_features.c), then given a
 * once-a-frame tick on the game's main thread. docs/PLUGINS.md is the guide. */
#ifndef BB_PLUGIN_HOST_H
#define BB_PLUGIN_HOST_H

#ifdef __cplusplus
extern "C" {
#endif

/* Finds, filters and starts the plugins: bb_plugin_info is read, then bb_plugin_init and
 * bb_plugin_image are called for each plugin that is on. Call between bb_hooks_init and
 * bb_main_frame_install. Returns the number of plugins left loaded.
 *
 * Folders, a later one overriding a file of the same name in an earlier one:
 * BB_PLUGIN_DIRS (';'-separated) when set, else <data>/plugins and <user>/plugins, where
 * <data> is the folder above bb-probe.exe's and <user> is BB_GPU_USER_DIR (<data>/user).
 * Switches: BB_PLUGINS=0 loads none; <user>/plugins.ini's [plugins] <name>=0/1 turns one off
 * or on (a plugin flagged BB_PLUGIN_OPT_IN is off until turned on); online play
 * (BB_ONLINE=1 without BB_COOP=1) leaves out every plugin flagged BB_PLUGIN_GAMEPLAY. */
int bb_plugins_install(void);

/* The per-frame step: each plugin's on_frame callbacks, in load order. Registered with
 * bb_on_main_frame by bb_plugins_install; tests call it directly. */
void bb_plugins_frame(void);

/* Where show_message and show_text go: the overlay's entry points (gpu/bbgpu.h), set by probe.c
 * before bb_plugins_install; tests set their own. Without them both return 1. */
void bb_plugins_set_text(int (*message)(const char *, float),
                         int (*text)(const char *, float, float, float, float, unsigned));

/* Tests: unloads every plugin and forgets all state, so another install can run. */
void bb_plugins_reset(void);

#ifdef __cplusplus
}
#endif

#endif
