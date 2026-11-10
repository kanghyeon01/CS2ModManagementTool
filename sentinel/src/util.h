/* Shared helpers for the sentinel: growable strings, logging, files, processes, config. */
#ifndef CS2MMT_UTIL_H
#define CS2MMT_UTIL_H

#include <stddef.h>
#include <stdint.h>
#include <time.h>

/* ---- growable string ---- */
typedef struct {
    char *buf;
    size_t len;
    size_t cap;
    int oom; /* set when an allocation failed; the content is then incomplete */
} sbuf;

void sb_init(sbuf *sb);
void sb_free(sbuf *sb);
void sb_append_n(sbuf *sb, const char *s, size_t n);
void sb_append(sbuf *sb, const char *s);
void sb_appendf(sbuf *sb, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
char *sb_take(sbuf *sb); /* returns the buffer (caller frees) and resets sb */
const char *sb_str(const sbuf *sb);

/* ---- logging ---- */
void log_open(const char *path);
void log_msg(const char *level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
#define LOGI(...) log_msg("INFO", __VA_ARGS__)
#define LOGW(...) log_msg("WARN", __VA_ARGS__)
#define LOGE(...) log_msg("ERROR", __VA_ARGS__)

int64_t now_s(void);

/* ---- files ---- */
char *read_file(const char *path, size_t max, size_t *out_len); /* NUL-terminated, caller frees */
int write_file_atomic(const char *path, const char *data, size_t len); /* 0 on success */

/* ---- processes ---- */
/* Runs argv[0] (searched in PATH) without a shell; stdout is captured into ``out`` (up to
 * ``max_out`` bytes), ``stdin_data`` (may be NULL) is written to its standard input. Returns
 * the exit code, 124 on timeout, -1 if it could not be started. */
int run_capture(char *const argv[], const char *stdin_data, int timeout_s, sbuf *out, size_t max_out);

/* ---- misc ---- */
int ct_equal(const char *a, const char *b); /* constant-time string comparison */
void random_hex(char *out, size_t nbytes);   /* out must hold 2*nbytes+1 */
int parse_mac(const char *text, unsigned char mac[6]);
void build_magic_packet(const unsigned char mac[6], unsigned char out[102]);
int send_magic_packet(const char *mac_text, const char *broadcast, int port, int repeat); /* datagrams sent */

/* ---- key = value config ---- */
#define CFG_MAX 128
typedef struct {
    int n;
    char *keys[CFG_MAX];
    char *vals[CFG_MAX];
} kvconf;

int kv_load(kvconf *c, const char *path, char *err, size_t errlen); /* 0 on success */
int kv_parse(kvconf *c, const char *text, char *err, size_t errlen);
const char *kv_get(const kvconf *c, const char *key, const char *def);
long kv_long(const kvconf *c, const char *key, long def);
void kv_set(kvconf *c, const char *key, const char *value);
void kv_free(kvconf *c);

#endif
