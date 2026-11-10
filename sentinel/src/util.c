/* Shared helpers for the sentinel. POSIX only (Linux, Synology DSM, Docker). */
#define _GNU_SOURCE
#include "util.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* ---------------------------------------------------------------- sbuf */
void sb_init(sbuf *sb) {
    sb->buf = NULL;
    sb->len = 0;
    sb->cap = 0;
    sb->oom = 0;
}

void sb_free(sbuf *sb) {
    free(sb->buf);
    sb_init(sb);
}

static int sb_reserve(sbuf *sb, size_t extra) {
    if (sb->oom) return -1;
    if (sb->len + extra + 1 <= sb->cap) return 0;
    size_t cap = sb->cap ? sb->cap : 256;
    while (cap < sb->len + extra + 1) {
        if (cap > ((size_t)1 << 30)) {
            sb->oom = 1;
            return -1;
        }
        cap *= 2;
    }
    char *nb = (char *)realloc(sb->buf, cap);
    if (!nb) {
        sb->oom = 1;
        return -1;
    }
    sb->buf = nb;
    sb->cap = cap;
    return 0;
}

void sb_append_n(sbuf *sb, const char *s, size_t n) {
    if (sb_reserve(sb, n) != 0) return;
    memcpy(sb->buf + sb->len, s, n);
    sb->len += n;
    sb->buf[sb->len] = '\0';
}

void sb_append(sbuf *sb, const char *s) {
    if (s) sb_append_n(sb, s, strlen(s));
}

void sb_appendf(sbuf *sb, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    va_list ap2;
    va_copy(ap2, ap);
    int need = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (need < 0) {
        va_end(ap2);
        return;
    }
    if (sb_reserve(sb, (size_t)need) == 0) {
        vsnprintf(sb->buf + sb->len, (size_t)need + 1, fmt, ap2);
        sb->len += (size_t)need;
    }
    va_end(ap2);
}

char *sb_take(sbuf *sb) {
    char *b = sb->buf;
    if (!b) {
        b = (char *)calloc(1, 1);
    }
    sb_init(sb);
    return b;
}

const char *sb_str(const sbuf *sb) {
    return sb->buf ? sb->buf : "";
}

/* ---------------------------------------------------------------- logging */
static FILE *g_log;

void log_open(const char *path) {
    if (!path || !*path) return;
    FILE *f = fopen(path, "a");
    if (f) {
        setvbuf(f, NULL, _IOLBF, 0);
        g_log = f;
    }
}

void log_msg(const char *level, const char *fmt, ...) {
    char ts[32];
    time_t t = time(NULL);
    struct tm tm;
    gmtime_r(&t, &tm);
    strftime(ts, sizeof ts, "%Y-%m-%dT%H:%M:%SZ", &tm);
    char msg[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    fprintf(stderr, "%s %s %s\n", ts, level, msg);
    if (g_log) fprintf(g_log, "%s %s %s\n", ts, level, msg);
}

int64_t now_s(void) {
    return (int64_t)time(NULL);
}

/* ---------------------------------------------------------------- files */
char *read_file(const char *path, size_t max, size_t *out_len) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return NULL;
    sbuf sb;
    sb_init(&sb);
    char tmp[8192];
    for (;;) {
        ssize_t r = read(fd, tmp, sizeof tmp);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r == 0) break;
        if (sb.len + (size_t)r > max) {
            sb.oom = 1;
            break;
        }
        sb_append_n(&sb, tmp, (size_t)r);
    }
    close(fd);
    if (sb.oom) {
        sb_free(&sb);
        return NULL;
    }
    if (out_len) *out_len = sb.len;
    return sb_take(&sb);
}

int write_file_atomic(const char *path, const char *data, size_t len) {
    char tmp[4096];
    if ((size_t)snprintf(tmp, sizeof tmp, "%s.tmp", path) >= sizeof tmp) return -1;
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(fd, data + off, len - off);
        if (w < 0) {
            if (errno == EINTR) continue;
            close(fd);
            unlink(tmp);
            return -1;
        }
        off += (size_t)w;
    }
    if (fsync(fd) != 0 || close(fd) != 0) {
        unlink(tmp);
        return -1;
    }
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

/* ---------------------------------------------------------------- processes */
int run_capture(char *const argv[], const char *stdin_data, int timeout_s, sbuf *out, size_t max_out) {
    int outp[2], inp[2] = {-1, -1};
    if (pipe(outp) != 0) return -1;
    if (stdin_data && pipe(inp) != 0) {
        close(outp[0]);
        close(outp[1]);
        return -1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        close(outp[0]);
        close(outp[1]);
        if (inp[0] >= 0) {
            close(inp[0]);
            close(inp[1]);
        }
        return -1;
    }
    if (pid == 0) {
        /* child */
        setpgid(0, 0);
        dup2(outp[1], STDOUT_FILENO);
        if (inp[0] >= 0) {
            dup2(inp[0], STDIN_FILENO);
        } else {
            int dn = open("/dev/null", O_RDONLY);
            if (dn >= 0) dup2(dn, STDIN_FILENO);
        }
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) dup2(devnull, STDERR_FILENO);
        close(outp[0]);
        close(outp[1]);
        if (inp[0] >= 0) {
            close(inp[0]);
            close(inp[1]);
        }
        execvp(argv[0], argv);
        _exit(127);
    }
    close(outp[1]);
    if (inp[0] >= 0) {
        close(inp[0]);
        size_t left = strlen(stdin_data);
        const char *p = stdin_data;
        signal(SIGPIPE, SIG_IGN);
        while (left > 0) {
            ssize_t w = write(inp[1], p, left);
            if (w < 0) {
                if (errno == EINTR) continue;
                break;
            }
            p += w;
            left -= (size_t)w;
        }
        close(inp[1]);
    }
    int64_t deadline = now_s() + timeout_s;
    int timed_out = 0;
    char buf[8192];
    for (;;) {
        int64_t left = deadline - now_s();
        if (left <= 0) {
            timed_out = 1;
            break;
        }
        struct pollfd pfd = {outp[0], POLLIN, 0};
        int pr = poll(&pfd, 1, (int)(left > 1 ? 1000 : left * 1000));
        if (pr < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (pr == 0) continue;
        ssize_t r = read(outp[0], buf, sizeof buf);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r == 0) break;
        if (out && out->len + (size_t)r <= max_out) sb_append_n(out, buf, (size_t)r);
    }
    close(outp[0]);
    int status = 0;
    /* The output is closed; wait for the exit, but never past the deadline. */
    while (!timed_out) {
        pid_t w = waitpid(pid, &status, WNOHANG);
        if (w == pid) goto done;
        if (w < 0 && errno != EINTR) goto done;
        if (now_s() >= deadline) {
            timed_out = 1;
            break;
        }
        usleep(50 * 1000);
    }
    kill(-pid, SIGKILL);
    kill(pid, SIGKILL);
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) break;
    }
done:
    if (timed_out) return 124;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -1;
}

/* ---------------------------------------------------------------- misc */
int ct_equal(const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    unsigned char diff = (unsigned char)(la != lb);
    size_t n = la > lb ? la : lb;
    for (size_t i = 0; i < n; i++) {
        unsigned char ca = i < la ? (unsigned char)a[i] : 0;
        unsigned char cb = i < lb ? (unsigned char)b[i] : 0;
        diff |= (unsigned char)(ca ^ cb);
    }
    return diff == 0;
}

void random_hex(char *out, size_t nbytes) {
    unsigned char b[32];
    if (nbytes > sizeof b) nbytes = sizeof b;
    int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    ssize_t got = -1;
    if (fd >= 0) {
        got = read(fd, b, nbytes);
        close(fd);
    }
    if (got != (ssize_t)nbytes) {
        unsigned seed = (unsigned)time(NULL) ^ (unsigned)getpid();
        for (size_t i = 0; i < nbytes; i++) b[i] = (unsigned char)(rand_r(&seed) & 0xFF);
    }
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < nbytes; i++) {
        out[2 * i] = hex[b[i] >> 4];
        out[2 * i + 1] = hex[b[i] & 15];
    }
    out[2 * nbytes] = '\0';
}

int parse_mac(const char *text, unsigned char mac[6]) {
    int n = 0, half = -1;
    for (const char *p = text; *p; p++) {
        int v;
        if (*p >= '0' && *p <= '9') v = *p - '0';
        else if (*p >= 'a' && *p <= 'f') v = *p - 'a' + 10;
        else if (*p >= 'A' && *p <= 'F') v = *p - 'A' + 10;
        else if (*p == ':' || *p == '-' || *p == '.' || *p == ' ') continue;
        else return -1;
        if (half < 0) half = v;
        else {
            if (n >= 6) return -1;
            mac[n++] = (unsigned char)((half << 4) | v);
            half = -1;
        }
    }
    return (n == 6 && half < 0) ? 0 : -1;
}

void build_magic_packet(const unsigned char mac[6], unsigned char out[102]) {
    memset(out, 0xFF, 6);
    for (int i = 0; i < 16; i++) memcpy(out + 6 + i * 6, mac, 6);
}

int send_magic_packet(const char *mac_text, const char *broadcast, int port, int repeat) {
    unsigned char mac[6], pkt[102];
    if (parse_mac(mac_text, mac) != 0) return -1;
    build_magic_packet(mac, pkt);
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return -1;
    int yes = 1;
    setsockopt(s, SOL_SOCKET, SO_BROADCAST, &yes, sizeof yes);
    struct sockaddr_in to;
    memset(&to, 0, sizeof to);
    to.sin_family = AF_INET;
    to.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, broadcast, &to.sin_addr) != 1) {
        close(s);
        return -1;
    }
    int sent = 0;
    for (int i = 0; i < (repeat > 0 ? repeat : 1); i++) {
        if (sendto(s, pkt, sizeof pkt, 0, (struct sockaddr *)&to, sizeof to) == (ssize_t)sizeof pkt) sent++;
    }
    close(s);
    return sent;
}

/* ---------------------------------------------------------------- kv config */
static char *trim(char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    char *e = s + strlen(s);
    while (e > s && isspace((unsigned char)e[-1])) *--e = '\0';
    return s;
}

void kv_set(kvconf *c, const char *key, const char *value) {
    for (int i = 0; i < c->n; i++) {
        if (strcmp(c->keys[i], key) == 0) {
            char *v = strdup(value);
            if (!v) return;
            free(c->vals[i]);
            c->vals[i] = v;
            return;
        }
    }
    if (c->n >= CFG_MAX) return;
    char *k = strdup(key), *v = strdup(value);
    if (!k || !v) {
        free(k);
        free(v);
        return;
    }
    c->keys[c->n] = k;
    c->vals[c->n] = v;
    c->n++;
}

int kv_parse(kvconf *c, const char *text, char *err, size_t errlen) {
    char *copy = strdup(text);
    if (!copy) return -1;
    int lineno = 0;
    char *save = NULL;
    for (char *line = strtok_r(copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        lineno++;
        char *l = trim(line);
        if (!*l || *l == '#' || *l == ';') continue;
        char *eq = strchr(l, '=');
        if (!eq) {
            if (err) snprintf(err, errlen, "line %d: expected key = value", lineno);
            free(copy);
            return -1;
        }
        *eq = '\0';
        char *k = trim(l);
        char *v = trim(eq + 1);
        size_t vl = strlen(v);
        if (vl >= 2 && ((v[0] == '"' && v[vl - 1] == '"') || (v[0] == '\'' && v[vl - 1] == '\''))) {
            v[vl - 1] = '\0';
            v++;
        }
        kv_set(c, k, v);
    }
    free(copy);
    return 0;
}

int kv_load(kvconf *c, const char *path, char *err, size_t errlen) {
    size_t len = 0;
    char *text = read_file(path, 1 << 20, &len);
    if (!text) {
        if (err) snprintf(err, errlen, "cannot read %s: %s", path, strerror(errno));
        return -1;
    }
    int r = kv_parse(c, text, err, errlen);
    free(text);
    return r;
}

const char *kv_get(const kvconf *c, const char *key, const char *def) {
    for (int i = 0; i < c->n; i++)
        if (strcmp(c->keys[i], key) == 0) return c->vals[i];
    return def;
}

long kv_long(const kvconf *c, const char *key, long def) {
    const char *v = kv_get(c, key, NULL);
    if (!v || !*v) return def;
    char *end = NULL;
    long r = strtol(v, &end, 10);
    return (end && *end == '\0') ? r : def;
}

void kv_free(kvconf *c) {
    for (int i = 0; i < c->n; i++) {
        free(c->keys[i]);
        free(c->vals[i]);
    }
    c->n = 0;
}
