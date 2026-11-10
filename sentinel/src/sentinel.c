/*
 * cs2mmt-sentinel: the always-on core of the CS2 Mod Management Tool, in plain C.
 *
 * It does the one job that must never stop: notice a new Cities: Skylines II build on Steam,
 * wake the PC with Wake-on-LAN, and hand a "game_update" job to the PC agent through the same
 * HTTP API the Python controller offers (the agent talks to both and runs each job once).
 *
 * - no libraries beyond libc; HTTPS goes through the system's curl executable (present on
 *   Synology DSM, Debian, Ubuntu and in the Docker image), run without a shell;
 * - single-threaded, every network and process call has a timeout, state is written atomically;
 * - Discord messages (webhook) only when the Python controller is down (configurable), so the two never
 *   send the same message twice in normal operation.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include "json.h"
#include "util.h"

#define SENTINEL_VERSION "0.1.0"
#define MAX_HEADER 16384
#define MAX_BODY 65536

typedef struct {
    const char *id, *listen_host, *token, *state_file, *curl, *build_url, *appid, *branch;
    long port, poll_s, settle_s, claim_timeout_s, max_wake, lease_s, max_attempts, retry_s, wol_port, wol_repeat;
    const char *wol_mac, *wol_bcast, *approval;
    const char *discord_webhook, *notify_mode, *controller_url, *hc_url, *hc_fail_url;
    long controller_down_s, hc_s, fail_alert_s, dispatch_s, ctrl_check_s;
    int unattended;
} config;

static kvconf g_cfgkv;
static kvconf g_state;
static config C;
static volatile sig_atomic_t g_stop;

/* ---------------------------------------------------------------- state */
static const char *st(const char *k) { return kv_get(&g_state, k, ""); }
static long stl(const char *k) { return kv_long(&g_state, k, 0); }
static void sts(const char *k, const char *v) { kv_set(&g_state, k, v ? v : ""); }
static void stsl(const char *k, long v) {
    char b[32];
    snprintf(b, sizeof b, "%ld", v);
    kv_set(&g_state, k, b);
}

static void st_save(void) {
    sbuf sb;
    sb_init(&sb);
    sb_append(&sb, "# cs2mmt-sentinel state (written by the program)\n");
    for (int i = 0; i < g_state.n; i++) {
        /* values never contain newlines: they are written only by this program */
        sb_appendf(&sb, "%s = %s\n", g_state.keys[i], g_state.vals[i]);
    }
    if (!sb.oom && write_file_atomic(C.state_file, sb_str(&sb), sb.len) != 0) LOGE("cannot write %s: %s", C.state_file, strerror(errno));
    sb_free(&sb);
}

static void scopy(char *dst, size_t n, const char *src) {
    /* bounded copy that truncates silently (no format-truncation warnings) */
    size_t l = src ? strlen(src) : 0;
    if (n == 0) return;
    if (l >= n) l = n - 1;
    if (l) memcpy(dst, src, l);
    dst[l] = '\0';
}

static void sanitize(char *dst, size_t n, const char *src) {
    size_t j = 0;
    for (size_t i = 0; src && src[i] && j + 1 < n; i++) dst[j++] = (src[i] == '\n' || src[i] == '\r') ? ' ' : src[i];
    dst[j] = '\0';
}

/* ---------------------------------------------------------------- curl helpers */
static void curl_quote(sbuf *sb, const char *s) {
    sb_append(sb, "\"");
    for (; s && *s; s++) {
        if (*s == '"' || *s == '\\') sb_appendf(sb, "\\%c", *s);
        else if (*s == '\n') sb_append(sb, "\\n");
        else if (*s == '\r') sb_append(sb, "\\r");
        else if (*s == '\t') sb_append(sb, "\\t");
        else sb_append_n(sb, s, 1);
    }
    sb_append(sb, "\"");
}

/* curl with its options on standard input (-K -): secrets never appear in the process list. */
static int curl_config(const char *config_text, sbuf *out, int timeout_s) {
    char *argv[] = {(char *)C.curl, "-sS", "-K", "-", NULL};
    return run_capture(argv, config_text, timeout_s, out, 8u << 20);
}

/* Discord webhook (one-way): POST {"content": ...}; Discord answers 204 No Content. */
static void discord_send(const char *text) {
    if (!C.discord_webhook || !*C.discord_webhook) {
        LOGI("(no Discord webhook configured) %s", text);
        return;
    }
    char msg[1900];
    snprintf(msg, sizeof msg, "[%s] %s", C.id, text);
    /* do not leave half a UTF-8 character at the end if it was cut */
    size_t n = strlen(msg);
    if (n == sizeof msg - 1) {
        while (n > 0 && ((unsigned char)msg[n - 1] & 0xC0) == 0x80) n--;
        if (n > 0 && ((unsigned char)msg[n - 1] & 0x80)) n--;
        msg[n] = '\0';
    }
    sbuf body, cfg, out;
    sb_init(&body);
    sb_init(&cfg);
    sb_init(&out);
    sb_append(&body, "{\"content\":");
    sb_json_str(&body, msg);
    sb_append(&body, ",\"allowed_mentions\":{\"parse\":[]}}");
    sb_append(&cfg, "url = ");
    curl_quote(&cfg, C.discord_webhook);
    sb_append(&cfg, "\nmax-time = 20\nconnect-timeout = 10\nheader = \"Content-Type: application/json\"\ndata-raw = ");
    curl_quote(&cfg, sb_str(&body));
    sb_append(&cfg, "\noutput = \"/dev/null\"\nwrite-out = \"%{http_code}\"\n");
    int rc = curl_config(sb_str(&cfg), &out, 30);
    if (rc != 0 || strncmp(sb_str(&out), "2", 1) != 0) LOGW("discord send failed (curl %d, HTTP %s)", rc, sb_str(&out));
    sb_free(&body);
    sb_free(&cfg);
    sb_free(&out);
}

static int controller_is_down(void) {
    if (!C.controller_url || !*C.controller_url) return 1;
    long since = stl("controller_down_since");
    return since > 0 && now_s() - since >= C.controller_down_s;
}

static void notify(const char *text, int force) {
    int send = force;
    if (!send) {
        if (strcmp(C.notify_mode, "always") == 0) send = 1;
        else if (strcmp(C.notify_mode, "never") == 0) send = 0;
        else send = controller_is_down();
    }
    LOGI("notify%s: %s", send ? "" : " (quiet)", text);
    if (send) discord_send(text);
}

/* ---------------------------------------------------------------- Steam */
static int fetch_build(char *out, size_t outlen, char *err, size_t errlen) {
    sbuf cfg, body;
    sb_init(&cfg);
    sb_init(&body);
    sb_append(&cfg, "url = ");
    curl_quote(&cfg, C.build_url);
    sb_append(&cfg, "\nfail\nmax-time = 40\nconnect-timeout = 15\nuser-agent = \"cs2mmt-sentinel/" SENTINEL_VERSION "\"\n");
    int rc = curl_config(sb_str(&cfg), &body, 60);
    sb_free(&cfg);
    if (rc != 0) {
        snprintf(err, errlen, "curl exit %d", rc);
        sb_free(&body);
        return -1;
    }
    char perr[160];
    jnode *root = json_parse(sb_str(&body), body.len, perr, sizeof perr);
    sb_free(&body);
    if (!root) {
        snprintf(err, errlen, "bad JSON: %s", perr);
        return -1;
    }
    const char *status = json_text(json_get(root, "status"));
    char path[256];
    snprintf(path, sizeof path, "data/%s/depots/branches/%s/buildid", C.appid, C.branch);
    const char *build = json_text(json_path(root, path));
    int ok = status && strcmp(status, "success") == 0 && build && *build;
    if (ok) {
        scopy(out, outlen, build);
    } else {
        snprintf(err, errlen, "no build id (status %s)", status ? status : "?");
    }
    json_free(root);
    return ok ? 0 : -1;
}

static int build_newer(const char *a, const char *b) { /* a > b, numerically when possible */
    char *ea = NULL, *eb = NULL;
    long long x = strtoll(a, &ea, 10), y = strtoll(b, &eb, 10);
    if (*a && *b && ea && *ea == '\0' && eb && *eb == '\0') return x > y;
    return strcmp(a, b) != 0;
}

static void job_new(const char *old_build, const char *new_build, int manual) {
    char rnd[7], id[96];
    random_hex(rnd, 3);
    snprintf(id, sizeof id, "sentinel-%s-%s", new_build, rnd);
    sts("job_id", id);
    sts("job_state", "queued");
    sts("job_old", old_build);
    sts("job_new", new_build);
    stsl("job_manual", manual);
    stsl("job_created", now_s());
    stsl("job_attempts", 0);
    stsl("job_wake_attempts", 0);
    stsl("job_not_before", 0);
    sts("job_host", "");
    sts("job_stage", "");
    sts("job_result", "");
    sts("job_next", "");
    st_save();
    LOGI("job %s queued (build %s -> %s)", id, old_build, new_build);
}

static void queue_update(const char *old_build, const char *new_build) {
    const char *js = st("job_state");
    if (strcmp(js, "queued") == 0) {
        sts("job_new", new_build); /* keep the oldest old build */
        st_save();
        return;
    }
    if (strcmp(js, "dispatched") == 0 || strcmp(js, "running") == 0) {
        sts("job_next", new_build); /* queued again when the current one ends */
        st_save();
        return;
    }
    job_new(old_build, new_build, 0);
}

static void steam_poll(void) {
    char build[64], err[256];
    stsl("last_check_at", now_s());
    if (fetch_build(build, sizeof build, err, sizeof err) != 0) {
        LOGW("build check failed: %s", err);
        char clean[256];
        sanitize(clean, sizeof clean, err);
        sts("last_error", clean);
        if (stl("fail_since") == 0) stsl("fail_since", now_s());
        if (!stl("fail_notified") && now_s() - stl("fail_since") > C.fail_alert_s) {
            stsl("fail_notified", 1);
            char msg[512];
            snprintf(msg, sizeof msg, "Steam 빌드 정보를 %ld시간째 가져오지 못합니다: %s", C.fail_alert_s / 3600, clean);
            notify(msg, 0);
        }
        st_save();
        return;
    }
    stsl("fail_since", 0);
    stsl("fail_notified", 0);
    sts("last_error", "");
    sts("last_check_build", build);
    const char *known = st("known_build");
    if (!*known) {
        sts("known_build", build);
        LOGI("baseline build %s", build);
        st_save();
        return;
    }
    if (strcmp(known, build) == 0) {
        sts("pending_build", "");
        st_save();
        return;
    }
    if (strcmp(st("pending_build"), build) != 0) {
        sts("pending_build", build);
        stsl("pending_first_seen", now_s());
        LOGI("new build %s seen (was %s); waiting %lds", build, known, C.settle_s);
        st_save();
        return;
    }
    if (now_s() - stl("pending_first_seen") < C.settle_s) return;
    char old[64];
    scopy(old, sizeof old, known);
    queue_update(old, build);
    sts("known_build", build);
    sts("pending_build", "");
    st_save();
    char msg[256];
    snprintf(msg, sizeof msg, "게임 업데이트 감지: 빌드 %s → %s. PC를 깨워 모드 점검을 시작합니다.", old, build);
    notify(msg, 0);
}

/* ---------------------------------------------------------------- dispatch */
static void wake(void) {
    if (!C.wol_mac || !*C.wol_mac) {
        LOGW("no wol_mac configured");
        return;
    }
    int n = send_magic_packet(C.wol_mac, C.wol_bcast, (int)C.wol_port, (int)C.wol_repeat);
    LOGI("WoL to %s via %s:%ld: %d packet(s)", C.wol_mac, C.wol_bcast, C.wol_port, n);
}

static int paused(void) { return stl("paused") != 0; }

static void dispatch_tick(void) {
    const char *js = st("job_state");
    int64_t now = now_s();
    if (strcmp(js, "queued") == 0) {
        if (paused() || now < stl("job_not_before")) return;
        wake();
        sts("job_state", "dispatched");
        stsl("job_dispatched_at", now);
        stsl("job_claim_deadline", now + C.claim_timeout_s);
        stsl("job_wake_attempts", 1);
        st_save();
    } else if (strcmp(js, "dispatched") == 0 && now > stl("job_claim_deadline")) {
        if (stl("job_wake_attempts") < C.max_wake) {
            wake();
            stsl("job_wake_attempts", stl("job_wake_attempts") + 1);
            stsl("job_claim_deadline", now + C.claim_timeout_s);
            st_save();
            return;
        }
        long attempts = stl("job_attempts") + 1;
        stsl("job_attempts", attempts);
        if (attempts < C.max_attempts) {
            sts("job_state", "queued");
            stsl("job_not_before", now + C.retry_s * attempts);
            notify("PC가 WoL에 응답하지 않습니다. 잠시 뒤 다시 깨웁니다.", 0);
        } else {
            sts("job_state", "failed");
            stsl("job_finished_at", now);
            notify("PC를 끝내 깨우지 못해 게임 업데이트 작업을 포기했습니다. 전원과 BIOS의 WoL 설정을 확인해야 합니다.", 1);
        }
        st_save();
    } else if (strcmp(js, "running") == 0 && now > stl("job_lease_until") + 300) {
        long attempts = stl("job_attempts") + 1;
        stsl("job_attempts", attempts);
        if (attempts < C.max_attempts) {
            sts("job_state", "queued");
            stsl("job_not_before", now + C.retry_s);
            notify("PC 작업의 응답이 끊겼습니다 (멈춤?). 다시 시도합니다.", 0);
        } else {
            sts("job_state", "failed");
            stsl("job_finished_at", now);
            notify("PC 작업이 계속 중간에 끊겨 포기했습니다.", 0);
        }
        st_save();
    }
}

static void finish_job(const char *state, const char *summary) {
    char clean[1024];
    sanitize(clean, sizeof clean, summary ? summary : "");
    sts("job_state", strcmp(state, "succeeded") == 0 ? "done" : "failed");
    sts("job_result", state);
    stsl("job_finished_at", now_s());
    char next[64];
    scopy(next, sizeof next, st("job_next"));
    char done_build[64];
    scopy(done_build, sizeof done_build, st("job_new"));
    st_save();
    if (*next && build_newer(next, done_build)) job_new(done_build, next, 0);
}

/* ---------------------------------------------------------------- controller watch, health ping */
static void controller_check(void) {
    if (!C.controller_url || !*C.controller_url) return;
    sbuf cfg, out;
    sb_init(&cfg);
    sb_init(&out);
    char url[600];
    snprintf(url, sizeof url, "%s/api/v1/health", C.controller_url);
    sb_append(&cfg, "url = ");
    curl_quote(&cfg, url);
    sb_append(&cfg, "\nmax-time = 10\nconnect-timeout = 5\noutput = \"/dev/null\"\nwrite-out = \"%{http_code}\"\n");
    int rc = curl_config(sb_str(&cfg), &out, 20);
    int up = rc == 0 && strncmp(sb_str(&out), "200", 3) == 0;
    sb_free(&cfg);
    sb_free(&out);
    long since = stl("controller_down_since");
    if (up) {
        if (stl("controller_down_notified")) notify("파이썬 컨트롤러가 다시 응답합니다.", 1);
        stsl("controller_down_since", 0);
        stsl("controller_down_notified", 0);
    } else {
        if (since == 0) stsl("controller_down_since", now_s());
        else if (!stl("controller_down_notified") && now_s() - since >= C.controller_down_s) {
            stsl("controller_down_notified", 1);
            notify("파이썬 컨트롤러가 응답하지 않습니다. C 감시기(sentinel)가 게임 업데이트 감시를 계속합니다.", 1);
        }
    }
    st_save();
}

static void healthcheck_ping(void) {
    if (!C.hc_url || !*C.hc_url) return;
    sbuf cfg, out;
    sb_init(&cfg);
    sb_init(&out);
    /* Healthy = the build check works. Otherwise ping the optional failure URL, or stay silent so
       the monitor notices the missing ping (works with any heartbeat / dead man's switch service). */
    int failing = stl("fail_since") && now_s() - stl("fail_since") > C.fail_alert_s;
    const char *url = failing ? C.hc_fail_url : C.hc_url;
    if (!url || !*url) {
        sb_free(&cfg);
        sb_free(&out);
        return;
    }
    sb_append(&cfg, "url = ");
    curl_quote(&cfg, url);
    sb_append(&cfg, "\nmax-time = 15\noutput = \"/dev/null\"\n");
    curl_config(sb_str(&cfg), &out, 25);
    sb_free(&cfg);
    sb_free(&out);
}

static void sd_notify_msg(const char *msg) {
    const char *addr = getenv("NOTIFY_SOCKET");
    if (!addr || !*addr) return;
    struct sockaddr_un sa;
    memset(&sa, 0, sizeof sa);
    sa.sun_family = AF_UNIX;
    size_t l = strlen(addr);
    if (l >= sizeof sa.sun_path) return;
    memcpy(sa.sun_path, addr, l);
    if (sa.sun_path[0] == '@') sa.sun_path[0] = '\0';
    int s = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0) return;
    sendto(s, msg, strlen(msg), 0, (struct sockaddr *)&sa, (socklen_t)(offsetof(struct sockaddr_un, sun_path) + l));
    close(s);
}

/* ---------------------------------------------------------------- HTTP API */
typedef struct {
    char method[8];
    char path[1024];
    char query[1024];
    char auth[512];
    long content_length;
    char *body;
    size_t body_len;
} request;

static const char *reason(int status) {
    switch (status) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 404: return "Not Found";
        case 409: return "Conflict";
        case 413: return "Payload Too Large";
        case 501: return "Not Implemented";
        default: return "Error";
    }
}

static void send_response(int fd, int status, const sbuf *body) {
    char head[256];
    int hl = snprintf(head, sizeof head,
                      "HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n",
                      status, reason(status), body->len);
    const char *parts[2] = {head, sb_str(body)};
    size_t lens[2] = {(size_t)hl, body->len};
    for (int i = 0; i < 2; i++) {
        size_t off = 0;
        while (off < lens[i]) {
            ssize_t w = send(fd, parts[i] + off, lens[i] - off, MSG_NOSIGNAL);
            if (w <= 0) {
                if (w < 0 && errno == EINTR) continue;
                return;
            }
            off += (size_t)w;
        }
    }
}

static int read_request(int fd, request *rq) {
    char buf[MAX_HEADER + 1];
    size_t len = 0;
    char *end = NULL;
    while (len < MAX_HEADER) {
        ssize_t r = recv(fd, buf + len, MAX_HEADER - len, 0);
        if (r <= 0) {
            if (r < 0 && errno == EINTR) continue;
            return -1;
        }
        len += (size_t)r;
        buf[len] = '\0';
        if ((end = strstr(buf, "\r\n\r\n")) != NULL) break;
    }
    if (!end) return -1;
    *end = '\0';
    char *body_start = end + 4;
    size_t have = len - (size_t)(body_start - buf);
    /* request line */
    char *line_end = strstr(buf, "\r\n");
    if (line_end) *line_end = '\0';
    char target[1024] = "";
    if (sscanf(buf, "%7s %1023s", rq->method, target) != 2) return -1;
    char *q = strchr(target, '?');
    if (q) {
        *q = '\0';
        scopy(rq->query, sizeof rq->query, q + 1);
    }
    scopy(rq->path, sizeof rq->path, target);
    /* headers */
    char *h = line_end ? line_end + 2 : NULL;
    while (h && *h) {
        char *nl = strstr(h, "\r\n");
        if (nl) *nl = '\0';
        char *colon = strchr(h, ':');
        if (colon) {
            *colon = '\0';
            char *v = colon + 1;
            while (*v == ' ' || *v == '\t') v++;
            if (strcasecmp(h, "Content-Length") == 0) rq->content_length = strtol(v, NULL, 10);
            else if (strcasecmp(h, "Authorization") == 0) scopy(rq->auth, sizeof rq->auth, v);
        }
        h = nl ? nl + 2 : NULL;
    }
    if (rq->content_length < 0 || rq->content_length > MAX_BODY) return -2;
    rq->body = (char *)malloc((size_t)rq->content_length + 1);
    if (!rq->body) return -1;
    size_t got = have > (size_t)rq->content_length ? (size_t)rq->content_length : have;
    memcpy(rq->body, body_start, got);
    while (got < (size_t)rq->content_length) {
        ssize_t r = recv(fd, rq->body + got, (size_t)rq->content_length - got, 0);
        if (r <= 0) {
            if (r < 0 && errno == EINTR) continue;
            return -1;
        }
        got += (size_t)r;
    }
    rq->body[got] = '\0';
    rq->body_len = got;
    return 0;
}

static int authed(const request *rq) {
    if (strncmp(rq->auth, "Bearer ", 7) != 0) return 0;
    return ct_equal(rq->auth + 7, C.token);
}

static void query_param(const char *query, const char *name, char *out, size_t outlen) {
    out[0] = '\0';
    size_t nl = strlen(name);
    const char *p = query;
    while (p && *p) {
        if (strncmp(p, name, nl) == 0 && p[nl] == '=') {
            const char *v = p + nl + 1;
            size_t l = strcspn(v, "&");
            if (l >= outlen) l = outlen - 1;
            memcpy(out, v, l);
            out[l] = '\0';
            return;
        }
        p = strchr(p, '&');
        if (p) p++;
    }
}

static int job_active(void) {
    const char *js = st("job_state");
    return strcmp(js, "queued") == 0 || strcmp(js, "dispatched") == 0 || strcmp(js, "running") == 0;
}

static void job_json(sbuf *sb) {
    char key[96];
    snprintf(key, sizeof key, "game_update:%s", st("job_new"));
    sb_append(sb, "{\"id\":");
    sb_json_str(sb, st("job_id"));
    sb_append(sb, ",\"kind\":\"game_update\",\"state\":");
    sb_json_str(sb, strcmp(st("job_state"), "done") == 0 ? "succeeded" : st("job_state"));
    sb_append(sb, ",\"payload\":{\"old_build\":");
    sb_json_str(sb, st("job_old"));
    sb_append(sb, ",\"new_build\":");
    sb_json_str(sb, st("job_new"));
    sb_append(sb, ",\"work_key\":");
    sb_json_str(sb, key);
    sb_append(sb, ",\"approval\":");
    sb_json_str(sb, C.approval);
    sb_appendf(sb, ",\"manual\":%s,\"detected_by\":", stl("job_manual") ? "true" : "false");
    char by[128];
    snprintf(by, sizeof by, "sentinel %s", C.id);
    sb_json_str(sb, by);
    sb_append(sb, "},\"host\":");
    sb_json_str(sb, st("job_host"));
    sb_appendf(sb, ",\"attempts\":%ld,\"stage\":", stl("job_attempts"));
    sb_json_str(sb, st("job_stage"));
    sb_append(sb, "}");
}

static void status_text(sbuf *sb) {
    sbuf t;
    sb_init(&t);
    sb_appendf(&t, "C 감시기 %s (v%s)\n게임 빌드: %s (확인 %lld초 전)\n", C.id, SENTINEL_VERSION, *st("known_build") ? st("known_build") : "확인 전",
               (long long)(now_s() - stl("last_check_at")));
    if (*st("last_error")) sb_appendf(&t, "최근 오류: %s\n", st("last_error"));
    sb_appendf(&t, "작업: %s %s\n", *st("job_id") ? st("job_id") : "없음", st("job_state"));
    sb_appendf(&t, "파이썬 컨트롤러: %s", C.controller_url && *C.controller_url ? (stl("controller_down_since") ? "응답 없음" : "정상") : "설정 안 함");
    sb_append(sb, "{\"text\":");
    sb_json_str(sb, sb_str(&t));
    sb_append(sb, ",\"job\":");
    job_json(sb);
    sb_append(sb, "}");
    sb_free(&t);
}

static int handle(const request *rq, sbuf *out) {
    int is_get = strcmp(rq->method, "GET") == 0, is_post = strcmp(rq->method, "POST") == 0;
    int ok_auth = authed(rq);
    if (is_get && strcmp(rq->path, "/api/v1/health") == 0) {
        sb_appendf(out, "{\"ok\":true,\"id\":");
        sb_json_str(out, C.id);
        sb_appendf(out, ",\"role\":\"sentinel\",\"version\":\"%s\",\"time\":%lld", SENTINEL_VERSION, (long long)now_s());
        if (ok_auth) {
            sb_append(out, ",\"known_build\":");
            sb_json_str(out, st("known_build"));
            sb_append(out, ",\"job_state\":");
            sb_json_str(out, st("job_state"));
        }
        sb_append(out, "}");
        return 200;
    }
    if (!ok_auth) {
        sb_append(out, "{\"error\":\"unauthorized\"}");
        return 401;
    }
    jnode *body = NULL;
    if (is_post && rq->body_len) {
        char perr[128];
        body = json_parse(rq->body, rq->body_len, perr, sizeof perr);
        if (!body || body->type != J_OBJ) {
            json_free(body);
            sb_append(out, "{\"error\":\"invalid JSON\"}");
            return 400;
        }
    }
    int status = 404;
    const char *p = rq->path;
    const char *job_id = st("job_id");
    size_t jl = strlen(job_id);
    if (is_get && strcmp(p, "/api/v1/status") == 0) {
        status_text(out);
        status = 200;
    } else if (is_get && strcmp(p, "/api/v1/boot-decision") == 0) {
        char bt[32], force[8];
        query_param(rq->query, "boot_time", bt, sizeof bt);
        query_param(rq->query, "force", force, sizeof force);
        const char *js = st("job_state");
        int active = strcmp(js, "dispatched") == 0 || strcmp(js, "running") == 0;
        int woke = atoll(bt) >= stl("job_dispatched_at") - 300;
        if (active && (woke || strcmp(force, "1") == 0)) {
            sb_append(out, "{\"action\":\"windows\",\"job_id\":");
            sb_json_str(out, job_id);
            sb_appendf(out, ",\"kind\":\"game_update\",\"unattended\":%s}", C.unattended ? "true" : "false");
        } else {
            sb_appendf(out, "{\"action\":\"none\",\"unattended\":%s}", C.unattended ? "true" : "false");
        }
        status = 200;
    } else if (is_post && strcmp(p, "/api/v1/jobs/claim") == 0) {
        const char *host = json_text(json_get(body, "host"));
        int opportunistic = json_truthy(json_get(body, "opportunistic"));
        jnode *kinds = json_get(body, "kinds");
        int kind_ok = 1;
        if (kinds && kinds->type == J_ARR) {
            kind_ok = 0;
            for (jnode *k = kinds->child; k; k = k->next)
                if (json_text(k) && strcmp(json_text(k), "game_update") == 0) kind_ok = 1;
        }
        const char *js = st("job_state");
        int resumed = strcmp(js, "running") == 0 && host && strcmp(host, st("job_host")) == 0;
        int claimable = strcmp(js, "dispatched") == 0 || (opportunistic && strcmp(js, "queued") == 0 && now_s() >= stl("job_not_before"));
        if (kind_ok && (resumed || claimable)) {
            if (resumed) stsl("job_attempts", stl("job_attempts") + 1);
            char clean[128];
            sanitize(clean, sizeof clean, host ? host : "pc");
            sts("job_state", "running");
            sts("job_host", clean);
            stsl("job_started_at", now_s());
            stsl("job_lease_until", now_s() + C.lease_s);
            sts("job_stage", "claimed");
            st_save();
            sb_append(out, "{\"job\":");
            job_json(out);
            sb_appendf(out, ",\"resumed\":%s}", resumed ? "true" : "false");
        } else {
            sb_append(out, "{\"job\":null}");
        }
        status = 200;
    } else if (is_post && strcmp(p, "/api/v1/jobs") == 0) {
        const char *kind = json_text(json_get(body, "kind"));
        if (!kind || strcmp(kind, "game_update") != 0) {
            sb_append(out, "{\"error\":\"the sentinel only queues game_update jobs\"}");
            status = 400;
        } else if (job_active()) {
            sb_append(out, "{\"job\":");
            job_json(out);
            sb_append(out, "}");
            status = 200;
        } else {
            char b[64];
            scopy(b, sizeof b, st("known_build"));
            job_new(b, b, 1);
            sb_append(out, "{\"job\":");
            job_json(out);
            sb_append(out, "}");
            status = 200;
        }
    } else if (jl && strncmp(p, "/api/v1/jobs/", 13) == 0 && strncmp(p + 13, job_id, jl) == 0) {
        const char *rest = p + 13 + jl;
        if (is_get && *rest == '\0') {
            sb_append(out, "{\"job\":");
            job_json(out);
            sb_append(out, "}");
            status = 200;
        } else if (is_post && strcmp(rest, "/heartbeat") == 0) {
            if (strcmp(st("job_state"), "running") != 0) {
                sb_append(out, "{\"error\":\"job is not running\"}");
                status = 409;
            } else {
                char clean[200];
                sanitize(clean, sizeof clean, json_text(json_get(body, "stage")));
                sts("job_stage", clean);
                stsl("job_lease_until", now_s() + C.lease_s);
                st_save();
                sb_append(out, "{\"ok\":true,\"cancel\":false}");
                status = 200;
            }
        } else if (is_post && strcmp(rest, "/complete") == 0) {
            const char *state = json_text(json_get(body, "state"));
            const char *summary = json_text(json_get(body, "summary"));
            long attempts = stl("job_attempts") + 1;
            if (state && strcmp(state, "failed") == 0 && json_truthy(json_path(body, "result/retry")) && attempts < C.max_attempts) {
                /* transient trouble on the PC (Steam download, Claude usage limit): later */
                stsl("job_attempts", attempts);
                sts("job_state", "queued");
                stsl("job_not_before", now_s() + 6 * 3600 * attempts);
                st_save();
                notify("게임 업데이트 대응이 일시적인 문제로 실패해 나중에 다시 시도합니다.", 0);
                sb_append(out, "{\"ok\":true,\"requeued\":true}");
                json_free(body);
                return 200;
            }
            finish_job(state ? state : "failed", summary);
            if (summary && *summary) {
                char msg[3000];
                snprintf(msg, sizeof msg, "게임 업데이트 대응 %s\n%s", state && strcmp(state, "succeeded") == 0 ? "완료" : "실패", summary);
                notify(msg, 0);
            }
            sb_append(out, "{\"ok\":true}");
            status = 200;
        }
    } else if (is_post && strcmp(p, "/api/v1/dedupe/complete") == 0) {
        const char *key = json_text(json_get(body, "key"));
        const char *state = json_text(json_get(body, "state"));
        int closed = 0;
        if (key && strncmp(key, "game_update:", 12) == 0) {
            const char *done_build = key + 12;
            const char *js = st("job_state");
            int open = strcmp(js, "queued") == 0 || strcmp(js, "dispatched") == 0;
            int covers = strcmp(done_build, st("job_new")) == 0 || build_newer(done_build, st("job_new"));
            if (open && covers && (!state || strcmp(state, "succeeded") == 0)) {
                finish_job("succeeded", "done via another controller");
                closed = 1;
            }
            if (state && strcmp(state, "succeeded") == 0 && *done_build && (!*st("known_build") || build_newer(done_build, st("known_build")))) {
                char d[64];
                scopy(d, sizeof d, done_build);
                sts("known_build", d);
                st_save();
            }
        }
        sb_appendf(out, "{\"closed\":%d}", closed);
        status = 200;
    } else if (is_post && strcmp(p, "/api/v1/approvals") == 0) {
        sb_append(out, "{\"error\":\"the sentinel does not handle approvals\"}");
        status = 501;
    } else if (is_post && strcmp(p, "/api/v1/notify") == 0) {
        const char *text = json_text(json_get(body, "text"));
        if (text && *text) notify(text, 0);
        sb_append(out, "{\"ok\":true}");
        status = 200;
    } else if (is_post && strcmp(p, "/api/v1/control") == 0) {
        const char *action = json_text(json_get(body, "action"));
        if (action && strcmp(action, "pause") == 0) stsl("paused", 1);
        else if (action && strcmp(action, "resume") == 0) stsl("paused", 0);
        else if (action && strcmp(action, "restart") == 0) {
            /* after a tool update: exit cleanly; Docker / systemd start the (rebuilt) binary again */
            LOGI("restart requested");
            g_stop = 1;
        }
        st_save();
        sb_appendf(out, "{\"paused\":%s,\"restarting\":%s}", paused() ? "true" : "false", g_stop ? "true" : "false");
        status = 200;
    }
    if (status == 404 && out->len == 0) sb_append(out, "{\"error\":\"not found\"}");
    json_free(body);
    return status;
}

static void serve_one(int lfd) {
    struct sockaddr_in peer;
    socklen_t pl = sizeof peer;
    int fd = accept(lfd, (struct sockaddr *)&peer, &pl);
    if (fd < 0) return;
    struct timeval tv = {5, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    request rq;
    memset(&rq, 0, sizeof rq);
    sbuf out;
    sb_init(&out);
    int r = read_request(fd, &rq);
    int status;
    if (r == -2) {
        sb_append(&out, "{\"error\":\"body too large\"}");
        status = 413;
    } else if (r != 0) {
        sb_append(&out, "{\"error\":\"bad request\"}");
        status = 400;
    } else {
        status = handle(&rq, &out);
    }
    send_response(fd, status, &out);
    sb_free(&out);
    free(rq.body);
    close(fd);
}

static int listen_on(const char *host, int port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &sa.sin_addr) != 1 || bind(fd, (struct sockaddr *)&sa, sizeof sa) != 0 || listen(fd, 16) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* ---------------------------------------------------------------- config & main */
static void on_signal(int sig) {
    (void)sig;
    g_stop = 1;
}

static void load_config(void) {
    C.id = kv_get(&g_cfgkv, "id", "sentinel");
    C.listen_host = kv_get(&g_cfgkv, "listen_host", "0.0.0.0");
    C.port = kv_long(&g_cfgkv, "listen_port", 8766);
    C.token = kv_get(&g_cfgkv, "api_token", getenv("CS2MMT_API_TOKEN") ? getenv("CS2MMT_API_TOKEN") : "");
    C.state_file = kv_get(&g_cfgkv, "state_file", "sentinel.state");
    C.curl = kv_get(&g_cfgkv, "curl", "curl");
    C.appid = kv_get(&g_cfgkv, "appid", "949230");
    C.branch = kv_get(&g_cfgkv, "branch", "public");
    C.build_url = kv_get(&g_cfgkv, "build_url", "https://api.steamcmd.net/v1/info/949230");
    C.poll_s = kv_long(&g_cfgkv, "poll_seconds", 900);
    C.settle_s = kv_long(&g_cfgkv, "settle_seconds", 1800);
    C.wol_mac = kv_get(&g_cfgkv, "wol_mac", "");
    C.wol_bcast = kv_get(&g_cfgkv, "wol_broadcast", "255.255.255.255");
    C.wol_port = kv_long(&g_cfgkv, "wol_port", 9);
    C.wol_repeat = kv_long(&g_cfgkv, "wol_repeat", 3);
    C.claim_timeout_s = kv_long(&g_cfgkv, "claim_timeout_seconds", 1500);
    C.max_wake = kv_long(&g_cfgkv, "max_wake_attempts", 3);
    C.lease_s = kv_long(&g_cfgkv, "lease_seconds", 1200);
    C.max_attempts = kv_long(&g_cfgkv, "max_job_attempts", 3);
    C.retry_s = kv_long(&g_cfgkv, "retry_seconds", 1800);
    C.approval = kv_get(&g_cfgkv, "approval", "auto");
    C.discord_webhook = kv_get(&g_cfgkv, "discord_webhook_url", getenv("CS2MMT_DISCORD_WEBHOOK") ? getenv("CS2MMT_DISCORD_WEBHOOK") : "");
    C.notify_mode = kv_get(&g_cfgkv, "notify", "when_controller_down");
    C.controller_url = kv_get(&g_cfgkv, "controller_url", "");
    C.controller_down_s = kv_long(&g_cfgkv, "controller_down_seconds", 900);
    C.hc_url = kv_get(&g_cfgkv, "healthcheck_url", "");
    C.hc_fail_url = kv_get(&g_cfgkv, "healthcheck_fail_url", "");
    C.hc_s = kv_long(&g_cfgkv, "healthcheck_seconds", 300);
    C.fail_alert_s = kv_long(&g_cfgkv, "fail_alert_seconds", 6 * 3600);
    C.unattended = (int)kv_long(&g_cfgkv, "unattended", 1);
    C.dispatch_s = kv_long(&g_cfgkv, "dispatch_seconds", 15);
    C.ctrl_check_s = kv_long(&g_cfgkv, "controller_check_seconds", 120);
}

int main(int argc, char **argv) {
    const char *cfg_path = "/etc/cs2mmt/sentinel.conf";
    int once = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--version") == 0) {
            printf("cs2mmt-sentinel %s\n", SENTINEL_VERSION);
            return 0;
        } else if (strcmp(argv[i], "--once") == 0) {
            once = 1;
        } else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
            cfg_path = argv[++i];
        } else {
            fprintf(stderr, "usage: %s [--config FILE] [--once] [--version]\n", argv[0]);
            return 2;
        }
    }
    char err[256];
    if (kv_load(&g_cfgkv, cfg_path, err, sizeof err) != 0) {
        fprintf(stderr, "config: %s\n", err);
        return 2;
    }
    load_config();
    log_open(kv_get(&g_cfgkv, "log_file", ""));
    if (strlen(C.token) < 16) {
        LOGE("api_token must be at least 16 characters (the same token as the controllers and the agent)");
        return 2;
    }
    char serr[256];
    if (kv_load(&g_state, C.state_file, serr, sizeof serr) != 0) LOGI("starting with an empty state (%s)", serr);
    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    if (once) {
        steam_poll();
        printf("known_build=%s pending=%s job=%s %s error=%s\n", st("known_build"), st("pending_build"), st("job_id"), st("job_state"), st("last_error"));
        return 0;
    }
    int lfd = listen_on(C.listen_host, (int)C.port);
    if (lfd < 0) {
        LOGE("cannot listen on %s:%ld: %s", C.listen_host, C.port, strerror(errno));
        return 1;
    }
    LOGI("cs2mmt-sentinel %s '%s' listening on %s:%ld, state %s", SENTINEL_VERSION, C.id, C.listen_host, C.port, C.state_file);
    sd_notify_msg("READY=1");
    if (!stl("started_once")) {
        stsl("started_once", now_s());
        st_save();
        discord_send("C 감시기(sentinel)가 처음 시작되었습니다. 게임 업데이트를 감시합니다.");
    }
    int64_t next_poll = now_s() + 5, next_dispatch = now_s() + 2, next_ctrl = now_s() + 30, next_hc = now_s() + 60;
    while (!g_stop) {
        struct pollfd pfd = {lfd, POLLIN, 0};
        int pr = poll(&pfd, 1, 1000);
        if (pr > 0 && (pfd.revents & POLLIN)) serve_one(lfd);
        int64_t now = now_s();
        if (now >= next_poll) {
            steam_poll();
            next_poll = now_s() + C.poll_s;
        }
        if (now >= next_dispatch) {
            dispatch_tick();
            next_dispatch = now_s() + C.dispatch_s;
        }
        if (now >= next_ctrl) {
            controller_check();
            next_ctrl = now_s() + C.ctrl_check_s;
        }
        if (now >= next_hc) {
            healthcheck_ping();
            next_hc = now_s() + C.hc_s;
        }
        sd_notify_msg("WATCHDOG=1");
    }
    LOGI("stopping");
    st_save();
    close(lfd);
    kv_free(&g_state);
    kv_free(&g_cfgkv);
    return 0;
}
