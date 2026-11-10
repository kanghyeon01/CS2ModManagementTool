/* Unit tests for the sentinel's JSON reader, helpers and config parser. Run: make test */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "json.h"
#include "util.h"

static int failures = 0;
#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                   \
        }                                                                 \
    } while (0)

static void test_steam_shape(void) {
    const char *doc =
        "{\"data\":{\"949230\":{\"_change_number\":31234567,\"common\":{\"name\":\"Cities: Skylines II\"},"
        "\"depots\":{\"949231\":{\"manifests\":{}},\"branches\":{\"public\":{\"buildid\":\"20512345\",\"timeupdated\":\"1761900000\"},"
        "\"beta\":{\"buildid\":\"20599999\",\"pwdrequired\":\"1\"}}}}},\"status\":\"success\"}";
    char err[128];
    jnode *root = json_parse(doc, strlen(doc), err, sizeof err);
    CHECK(root != NULL);
    CHECK(strcmp(json_text(json_get(root, "status")), "success") == 0);
    CHECK(strcmp(json_text(json_path(root, "data/949230/depots/branches/public/buildid")), "20512345") == 0);
    CHECK(strcmp(json_text(json_path(root, "data/949230/depots/branches/beta/buildid")), "20599999") == 0);
    CHECK(json_path(root, "data/949230/depots/branches/missing/buildid") == NULL);
    CHECK(strcmp(json_text(json_path(root, "data/949230/_change_number")), "31234567") == 0);
    json_free(root);
}

static void test_values_and_escapes(void) {
    const char *doc = "{\"s\":\"a\\\"b\\\\c\\n\\u00e9\\ud83d\\ude00\",\"t\":true,\"f\":false,\"n\":null,\"x\":-12.5e1,\"arr\":[1,\"two\",[]],\"o\":{}}";
    char err[128];
    jnode *root = json_parse(doc, strlen(doc), err, sizeof err);
    CHECK(root != NULL);
    const char *s = json_text(json_get(root, "s"));
    CHECK(s && strcmp(s, "a\"b\\c\n\xc3\xa9\xf0\x9f\x98\x80") == 0);
    CHECK(json_truthy(json_get(root, "t")) == 1);
    CHECK(json_truthy(json_get(root, "f")) == 0);
    CHECK(json_get(root, "n")->type == J_NULL);
    CHECK(json_get(root, "x")->num == -125.0);
    jnode *arr = json_get(root, "arr");
    CHECK(arr && arr->type == J_ARR && arr->child && arr->child->next && strcmp(json_text(arr->child->next), "two") == 0);
    json_free(root);
}

static void test_rejects_bad_input(void) {
    char err[128];
    const char *bad[] = {"", "{", "{\"a\":}", "[1,2", "{\"a\" 1}", "\"unterminated", "{\"a\":1} x", "[\"\\x\"]", "nul", "{\"a\":01e}x"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        jnode *r = json_parse(bad[i], strlen(bad[i]), err, sizeof err);
        CHECK(r == NULL);
        json_free(r);
    }
    /* depth limit */
    char deep[400];
    memset(deep, '[', 200);
    memset(deep + 200, ']', 200);
    jnode *r = json_parse(deep, 400, err, sizeof err);
    CHECK(r == NULL);
    json_free(r);
}

static void test_json_writer(void) {
    sbuf sb;
    sb_init(&sb);
    sb_json_str(&sb, "he said \"hi\"\n\\ \x01");
    CHECK(strcmp(sb_str(&sb), "\"he said \\\"hi\\\"\\n\\\\ \\u0001\"") == 0);
    char err[64];
    jnode *back = json_parse(sb.buf, sb.len, err, sizeof err);
    CHECK(back && strcmp(json_text(back), "he said \"hi\"\n\\ \x01") == 0);
    json_free(back);
    sb_free(&sb);
}

static void test_mac_and_packet(void) {
    unsigned char mac[6], pkt[102];
    CHECK(parse_mac("AA:bb:0C-1d.22 33", mac) == 0);
    CHECK(mac[0] == 0xAA && mac[1] == 0xBB && mac[2] == 0x0C && mac[5] == 0x33);
    CHECK(parse_mac("AA:BB:CC", mac) != 0);
    CHECK(parse_mac("AA:BB:CC:DD:EE:FF:00", mac) != 0);
    CHECK(parse_mac("zz:bb:cc:dd:ee:ff", mac) != 0);
    parse_mac("01:02:03:04:05:06", mac);
    build_magic_packet(mac, pkt);
    for (int i = 0; i < 6; i++) CHECK(pkt[i] == 0xFF);
    for (int r = 0; r < 16; r++) CHECK(memcmp(pkt + 6 + r * 6, mac, 6) == 0);
}

static void test_kv(void) {
    kvconf c;
    memset(&c, 0, sizeof c);
    char err[128];
    CHECK(kv_parse(&c, "# comment\nid = nas\n  poll_seconds=60  \nquoted = \"a b\"\nurl = http://x/y?a=b\n", err, sizeof err) == 0);
    CHECK(strcmp(kv_get(&c, "id", ""), "nas") == 0);
    CHECK(kv_long(&c, "poll_seconds", 0) == 60);
    CHECK(strcmp(kv_get(&c, "quoted", ""), "a b") == 0);
    CHECK(strcmp(kv_get(&c, "url", ""), "http://x/y?a=b") == 0);
    CHECK(kv_long(&c, "missing", 7) == 7);
    kv_set(&c, "id", "server");
    CHECK(strcmp(kv_get(&c, "id", ""), "server") == 0);
    CHECK(kv_parse(&c, "no equals sign here\n", err, sizeof err) != 0);
    kv_free(&c);
}

static void test_ct_equal(void) {
    CHECK(ct_equal("secret-token", "secret-token"));
    CHECK(!ct_equal("secret-token", "secret-tokeN"));
    CHECK(!ct_equal("secret", "secret-token"));
    CHECK(!ct_equal("", "x"));
}

static void test_run_capture(void) {
    sbuf out;
    sb_init(&out);
    char *argv1[] = {"sh", "-c", "cat; echo done", NULL};
    int rc = run_capture(argv1, "from stdin\n", 10, &out, 1 << 20);
    CHECK(rc == 0);
    CHECK(strcmp(sb_str(&out), "from stdin\ndone\n") == 0);
    sb_free(&out);
    char *argv2[] = {"sh", "-c", "sleep 5", NULL};
    rc = run_capture(argv2, NULL, 1, &out, 1024);
    CHECK(rc == 124);
    sb_free(&out);
    char *argv3[] = {"/nonexistent/binary", NULL};
    rc = run_capture(argv3, NULL, 5, &out, 1024);
    CHECK(rc == 127);
    sb_free(&out);
}

int main(void) {
    test_steam_shape();
    test_values_and_escapes();
    test_rejects_bad_input();
    test_json_writer();
    test_mac_and_packet();
    test_kv();
    test_ct_equal();
    test_run_capture();
    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("all sentinel unit tests passed\n");
    return 0;
}
