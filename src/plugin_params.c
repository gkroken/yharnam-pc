/* SPDX-License-Identifier: GPL-3.0-or-later
 * Param tables for plugins (plugin_params.h). The tables are found as src/rebirth.c finds them
 * (after droogie's bbhost, GPL-3.0-or-later): SoloParamRepository holds 63 slots of up to eight
 * caps; a cap's name is a wstring, its param file at *(*(cap + 0x70) + 0x70). A param file: row
 * count u16 at +0xa, the param type (ASCII, 32 bytes) at +0xc, format bytes at +0x2d/+0x2e, and
 * the row table, sorted by id.
 *
 * Field names come from the game's own PARAMDEF files, through scripts/paramdefs.py. A
 * definition is used only when its row size equals the live table's row stride: a mismatch
 * would read the wrong bytes, so field access is refused (and logged once per type). */
#include "plugin_params.h"

#include "engine_hooks.h"

#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

enum { PARAM_REPOSITORY = 0x5940340 };  /* SoloParamRepository* */

typedef struct {
    char name[64];
    char type[12];   /* s8 u8 s16 u16 s32 u32 f32 dummy8 fixstr fixstrW */
    uint32_t offset, bytes, count;
    uint8_t bit, bits;
} Field;

typedef struct {
    char type[64];
    uint32_t row_bytes;
    int first, n;   /* its fields in g_fields */
    int said;       /* a size mismatch was logged */
} Def;

static Def *g_defs;
static int g_def_count;
static Field *g_fields;
static int g_field_count;
static int g_loaded;
static SRWLOCK g_lock = SRWLOCK_INIT;

/* --- the definitions ---------------------------------------------------------------------------- */

static int load_locked(const char *path) {
    g_loaded = 1;
    if (!path) path = getenv("BB_PARAMDEFS");
    FILE *f = path && *path ? fopen(path, "rb") : NULL;
    if (!f) return 0;
    char line[256];
    int def_cap = 0, field_cap = 0;
    while (fgets(line, sizeof line, f)) {
        if (line[0] == 'T' && line[1] == ' ') {
            if (g_def_count == def_cap) {
                def_cap = def_cap ? def_cap * 2 : 64;
                Def *grown = realloc(g_defs, (size_t)def_cap * sizeof *g_defs);
                if (!grown) break;
                g_defs = grown;
            }
            Def d = {0};
            if (sscanf(line + 2, "%63s %u", d.type, &d.row_bytes) != 2) continue;
            d.first = g_field_count;
            g_defs[g_def_count++] = d;
        } else if (line[0] == 'F' && line[1] == ' ' && g_def_count) {
            if (g_field_count == field_cap) {
                field_cap = field_cap ? field_cap * 2 : 1024;
                Field *grown = realloc(g_fields, (size_t)field_cap * sizeof *g_fields);
                if (!grown) break;
                g_fields = grown;
            }
            Field x = {0};
            unsigned bit = 0, bits = 0;
            if (sscanf(line + 2, "%63s %11s %u %u %u %u %u", x.name, x.type, &x.offset, &x.bytes, &x.count, &bit,
                       &bits) != 7)
                continue;
            x.bit = (uint8_t)bit;
            x.bits = (uint8_t)bits;
            g_fields[g_field_count++] = x;
            ++g_defs[g_def_count - 1].n;
        }
    }
    fclose(f);
    return g_def_count;
}

int bb_params_load(const char *path) {
    AcquireSRWLockExclusive(&g_lock);
    const int n = g_loaded ? g_def_count : load_locked(path);
    ReleaseSRWLockExclusive(&g_lock);
    return n;
}

void bb_params_reset(void) {
    AcquireSRWLockExclusive(&g_lock);
    free(g_defs);
    free(g_fields);
    g_defs = NULL;
    g_fields = NULL;
    g_def_count = g_field_count = 0;
    g_loaded = 0;
    ReleaseSRWLockExclusive(&g_lock);
}

static Def *def_for(const char *type) {
    bb_params_load(NULL);
    for (int i = 0; i < g_def_count; ++i)
        if (!strcmp(g_defs[i].type, type)) return &g_defs[i];
    return NULL;
}

/* --- the tables ---------------------------------------------------------------------------------- */

static uint64_t repository(void) {
    unsigned char *image = bb_guest(0);
    uint64_t repo = 0;
    if (!image || bb_image_size() < PARAM_REPOSITORY - 0x400000 + 8) return 0;
    return bb_read((uint64_t)(uintptr_t)(image + PARAM_REPOSITORY - 0x400000), &repo, 8) ? repo : 0;
}

static int cap_named(uint64_t cap, const char *want) {
    uint64_t capacity = 0, text = cap + 0x18;
    if (!bb_read(cap + 0x30, &capacity, 8) || (capacity >= 8 && !bb_read(cap + 0x18, &text, 8))) return 0;
    for (size_t i = 0; i < 64; ++i) {
        uint16_t c = 0;
        if (!bb_read(text + 2 * i, &c, 2) || c != (unsigned char)want[i]) return 0;
        if (!c) return 1;
    }
    return 0;
}

typedef struct {
    uint64_t file;
    uint32_t rows;
    uint8_t format, flags;
} Table;

static int find_table(const char *name, Table *t) {
    const uint64_t repo = repository();
    if (!repo || !name || !*name || strlen(name) >= 64) return 0;
    for (unsigned s = 0; s < 63; ++s) {
        int32_t count = 0;
        if (!bb_read(repo + 0x70 + s * 0x48, &count, 4) || count <= 0) continue;
        for (int k = 0; k < count && k < 8; ++k) {
            uint64_t cap = 0, holder = 0, file = 0;
            if (!bb_read(repo + 0x70 + s * 0x48 + 8 + 8 * k, &cap, 8) || !cap || !cap_named(cap, name)) continue;
            uint16_t rows = 0;
            if (!bb_read(cap + 0x70, &holder, 8) || !holder || !bb_read(holder + 0x70, &file, 8) || !file ||
                !bb_read(file + 0xa, &rows, 2) || !bb_read(file + 0x2d, &t->format, 1) ||
                !bb_read(file + 0x2e, &t->flags, 1))
                return 0;
            t->file = file;
            t->rows = rows;
            return 1;
        }
    }
    return 0;
}

/* Row i's id and data address. */
static int row_at(const Table *t, uint32_t i, uint32_t *id, uint64_t *data) {
    if (i >= t->rows) return 0;
    if (t->format == 2) {
        uint32_t off = 0;
        if (!bb_read(t->file + 0x34 + 0xc * i, id, 4) || !bb_read(t->file + 0x38 + 0xc * i, &off, 4)) return 0;
        *data = t->file + off;
    } else if (t->format <= 3 || !(t->flags & 2)) {
        uint32_t off = 0;
        if (!bb_read(t->file + 0x40 + 0xc * i, id, 4) || !bb_read(t->file + 0x44 + 0xc * i, &off, 4)) return 0;
        *data = t->file + off;
    } else {
        uint64_t off = 0;
        if (!bb_read(t->file + 0x40 + 0x18 * i, id, 4) || !bb_read(t->file + 0x48 + 0x18 * i, &off, 8)) return 0;
        *data = t->file + off;
    }
    return 1;
}

static uint64_t find_row(const Table *t, uint32_t id) {
    uint32_t lo = 0, hi = t->rows;
    while (lo < hi) {  /* rows are sorted by id */
        const uint32_t mid = lo + (hi - lo) / 2;
        uint32_t rid = 0;
        uint64_t data = 0;
        if (!row_at(t, mid, &rid, &data)) return 0;
        if (rid == id) return data;
        if (rid < id) lo = mid + 1;
        else hi = mid;
    }
    return 0;
}

/* The row stride: the distance between the first two rows' data (0 for a table of one row). */
static uint32_t stride(const Table *t) {
    uint32_t id0 = 0, id1 = 0;
    uint64_t d0 = 0, d1 = 0;
    if (t->rows < 2 || !row_at(t, 0, &id0, &d0) || !row_at(t, 1, &id1, &d1) || d1 <= d0 || d1 - d0 > 0x100000) return 0;
    return (uint32_t)(d1 - d0);
}

void *bb_param_row(const char *table, uint32_t id, size_t *bytes) {
    Table t;
    if (bytes) *bytes = 0;
    if (!find_table(table, &t)) return NULL;
    const uint64_t row = find_row(&t, id);
    if (row && bytes) *bytes = stride(&t);
    return (void *)(uintptr_t)row;
}

int bb_param_ids(const char *table, uint32_t *ids, size_t max, size_t *count) {
    Table t;
    if (count) *count = 0;
    if (!find_table(table, &t)) return 1;
    if (count) *count = t.rows;
    for (uint32_t i = 0; ids && i < t.rows && i < max; ++i) {
        uint64_t data = 0;
        if (!row_at(&t, i, &ids[i], &data)) return 1;
    }
    return 0;
}

/* --- fields -------------------------------------------------------------------------------------- */

/* The row and the field (and the array index) for a request, or 0. */
static int locate(const char *table, uint32_t id, const char *spec, uint64_t *row, const Field **field,
                  uint32_t *index) {
    Table t;
    if (!spec || !find_table(table, &t)) return 0;
    *row = find_row(&t, id);
    if (!*row) return 0;
    char type[33] = {0};
    if (!bb_read(t.file + 0xc, type, 32)) return 0;
    char name[64];
    *index = 0;
    snprintf(name, sizeof name, "%s", spec);
    char *bracket = strchr(name, '[');
    if (bracket) {
        char *end = NULL;
        const unsigned long i = strtoul(bracket + 1, &end, 10);
        if (end == bracket + 1 || *end != ']' || end[1]) return 0;
        *index = (uint32_t)i;
        *bracket = 0;
    }
    Def *d = def_for(type);
    if (!d) return 0;
    const uint32_t live = stride(&t);
    if (live && live != d->row_bytes) {
        if (!d->said) {
            d->said = 1;
            printf("Plugins: param type %s: its definition's rows are %u bytes, the game's table's %u; "
                   "its fields are not used\n", type, d->row_bytes, live);
            fflush(stdout);
        }
        return 0;
    }
    for (int i = 0; i < d->n; ++i) {
        const Field *f = &g_fields[d->first + i];
        if (!strcmp(f->name, name)) {
            if (*index >= f->count) return 0;
            *field = f;
            return 1;
        }
    }
    return 0;
}

static int is(const Field *f, const char *type) { return !strcmp(f->type, type); }

static uint32_t element_size(const Field *f) { return f->count ? f->bytes / f->count : f->bytes; }

int bb_param_get(const char *table, uint32_t id, const char *spec, char *out, size_t n) {
    if (out && n) out[0] = 0;
    uint64_t row = 0;
    const Field *f = NULL;
    uint32_t index = 0;
    if (!out || !n || !locate(table, id, spec, &row, &f, &index)) return 1;
    if (is(f, "fixstr") || is(f, "fixstrW")) {
        const int wide = is(f, "fixstrW");
        size_t w = 0;
        for (uint32_t i = 0; i < f->count && w + 1 < n; ++i) {
            uint16_t c = 0;
            if (!bb_read(row + f->offset + (wide ? 2u : 1u) * i, &c, wide ? 2 : 1) || !c) break;
            out[w++] = c < 0x80 ? (char)c : '?';
        }
        out[w] = 0;
        return 0;
    }
    const uint32_t size = f->bits ? f->bytes : element_size(f);
    const uint64_t at = row + f->offset + (f->bits ? 0 : (uint64_t)index * size);
    uint32_t raw = 0;
    if (size > 4 || !bb_read(at, &raw, size)) return 1;
    if (f->bits) {
        const uint32_t mask = f->bits >= 32 ? 0xffffffffu : (1u << f->bits) - 1;
        snprintf(out, n, "%u", (raw >> f->bit) & mask);
    } else if (is(f, "f32")) {
        float v;
        memcpy(&v, &raw, 4);
        snprintf(out, n, "%.9g", v);
    } else if (is(f, "s8")) {
        snprintf(out, n, "%d", (int8_t)raw);
    } else if (is(f, "s16")) {
        snprintf(out, n, "%d", (int16_t)raw);
    } else if (is(f, "s32")) {
        snprintf(out, n, "%d", (int32_t)raw);
    } else {
        snprintf(out, n, "%u", raw);  /* u8 u16 u32 dummy8 */
    }
    return 0;
}

int bb_param_set(const char *table, uint32_t id, const char *spec, const char *value) {
    uint64_t row = 0;
    const Field *f = NULL;
    uint32_t index = 0;
    if (!value || !*value || !locate(table, id, spec, &row, &f, &index)) return 1;
    if (is(f, "fixstr") || is(f, "fixstrW")) return 1;
    const uint32_t size = f->bits ? f->bytes : element_size(f);
    const uint64_t at = row + f->offset + (f->bits ? 0 : (uint64_t)index * size);
    uint32_t raw = 0, check = 0;
    if (size > 4 || !bb_read(at, &check, size)) return 1;
    char *end = NULL;
    errno = 0;
    if (is(f, "f32") && !f->bits) {
        const double d = strtod(value, &end);
        if (*end || errno || !isfinite(d) || fabs(d) > 3.4e38) return 1;
        const float v = (float)d;
        memcpy(&raw, &v, 4);
    } else {
        const long long v = strtoll(value, &end, 0);
        if (*end || errno) return 1;
        long long lo = 0, hi = 0;
        if (f->bits) {
            hi = f->bits >= 32 ? 0xffffffffll : (1ll << f->bits) - 1;
        } else if (is(f, "s8")) { lo = -128; hi = 127; }
        else if (is(f, "s16")) { lo = -32768; hi = 32767; }
        else if (is(f, "s32")) { lo = -2147483648ll; hi = 2147483647ll; }
        else if (size == 1) hi = 0xff;
        else if (size == 2) hi = 0xffff;
        else hi = 0xffffffffll;
        if (v < lo || v > hi) return 1;
        if (f->bits) {
            const uint32_t mask = (f->bits >= 32 ? 0xffffffffu : (1u << f->bits) - 1) << f->bit;
            raw = (check & ~mask) | (((uint32_t)v << f->bit) & mask);  /* the unit's other bits stay */
        } else {
            raw = (uint32_t)v;
        }
    }
    memcpy((void *)(uintptr_t)at, &raw, size);
    return 0;
}
