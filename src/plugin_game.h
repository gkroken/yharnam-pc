/* SPDX-License-Identifier: GPL-3.0-or-later
 * The game's state for plugins (plugin_game.c): event flags, the local player's numbers, where
 * the player is. Used by plugin_host.c for the API's event_flag_*, on_event_flag, player_stat_*,
 * player_position and player_block. Addresses are the 1.09 build's (Binary Ninja). */
#ifndef BB_PLUGIN_GAME_H
#define BB_PLUGIN_GAME_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* A flag's value, 0 or 1, read from the game's flag store with fault-safe reads (any thread).
 * Returns 0, or 1 when the store or the flag's block does not exist (yet). */
int bb_game_event_flag_get(uint32_t id, int *value);
/* Writes a flag's bit (call from the game's main thread) and reports a change to the watchers.
 * Returns 0, or 1 when the block does not exist. */
int bb_game_event_flag_set(uint32_t id, int value);

/* Starts watching the game's own flag writes: a hook on its single-flag setter. Only while the
 * engine hooks are open (bb_plugin_image). Returns 0 (or 0 again when already watching), else
 * the hook's result. */
int bb_game_watch_flags(void);
int bb_game_watching_flags(void);
typedef struct {
    uint32_t id;
    int value;
} BbFlagChange;
/* The changes since the last call, oldest first (at most max); *lost counts changes dropped
 * because more came between two calls than the queue holds. */
size_t bb_game_take_flag_changes(BbFlagChange *out, size_t max, uint64_t *lost);

/* The local player's numbers by bbhost's names ("level", "echoes", "vitality", "hp", ...,
 * "deaths", "play_time_ms"). 0, or 1 for an unknown name or no loaded character. */
int bb_game_player_stat_get(const char *name, int64_t *value);
int bb_game_player_stat_set(const char *name, int64_t value);

/* The main player's position (y up) and facing, and the map block it is in (area, block, two
 * sub-ids: m24_01_00_00 = 0x18010000). 0, or 1 when the world has no player. */
int bb_game_player_position(float xyz[3], float *yaw);
int bb_game_player_block(uint32_t *block);

/* Tests: forgets the watcher and its queue. */
void bb_game_reset(void);

#ifdef __cplusplus
}
#endif

#endif
