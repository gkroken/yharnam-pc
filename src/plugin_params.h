/* SPDX-License-Identifier: GPL-3.0-or-later
 * Param tables for plugins (plugin_params.c): rows by table name and id, fields by the game's own
 * field names. Used by plugin_host.c for param_row, param_get, param_set and param_ids. */
#ifndef BB_PLUGIN_PARAMS_H
#define BB_PLUGIN_PARAMS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The field table scripts/paramdefs.py wrote (BB_PARAMDEFS when path is NULL), loaded once.
 * Returns how many param types it describes (0: none, field names unavailable). */
int bb_params_load(const char *path);

/* A row of a table the game has loaded ("EquipParamWeapon"), by id: its address in the game's
 * memory and its size in bytes (the table's row stride). NULL when the table is not loaded
 * (yet) or has no such row. */
void *bb_param_row(const char *table, uint32_t id, size_t *bytes);

/* A field of a row by its name ("correctStrength"; "name[2]" for an array element) as text,
 * into out (n bytes). 0, or 1 when the row, the field or a matching definition is missing. */
int bb_param_get(const char *table, uint32_t id, const char *field, char *out, size_t n);
/* Writes a field from text: an integer within the field's range, a number for f32; a bitfield
 * keeps its unit's other bits. 0, or 1 when missing, out of range, or a fixed string. */
int bb_param_set(const char *table, uint32_t id, const char *field, const char *value);

/* The table's row ids in order: up to max into ids (may be NULL), the row count into *count.
 * 0, or 1 when the table is not loaded. */
int bb_param_ids(const char *table, uint32_t *ids, size_t max, size_t *count);

/* Tests: forgets the loaded definitions. */
void bb_params_reset(void);

#ifdef __cplusplus
}
#endif

#endif
