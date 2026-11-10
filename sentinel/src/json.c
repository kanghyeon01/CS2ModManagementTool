/* Minimal JSON reader (recursive descent, depth-limited) and string escaping. */
#include "json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define JSON_MAX_DEPTH 64

typedef struct {
    const char *s;
    size_t n;
    size_t i;
    char *err;
    size_t errlen;
    int depth;
} parser;

static void set_err(parser *p, const char *msg) {
    if (p->err && p->errlen && p->err[0] == '\0') snprintf(p->err, p->errlen, "%s at offset %zu", msg, p->i);
}

static void skip_ws(parser *p) {
    while (p->i < p->n) {
        char c = p->s[p->i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->i++;
        else break;
    }
}

static jnode *new_node(jtype t) {
    jnode *n = (jnode *)calloc(1, sizeof(jnode));
    if (n) n->type = t;
    return n;
}

void json_free(jnode *n) {
    while (n) {
        jnode *next = n->next;
        json_free(n->child);
        free(n->key);
        free(n->str);
        free(n);
        n = next;
    }
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int read_hex4(parser *p, unsigned *out) {
    if (p->i + 4 > p->n) return -1;
    unsigned v = 0;
    for (int k = 0; k < 4; k++) {
        int h = hexval(p->s[p->i + k]);
        if (h < 0) return -1;
        v = (v << 4) | (unsigned)h;
    }
    p->i += 4;
    *out = v;
    return 0;
}

static void put_utf8(sbuf *sb, unsigned cp) {
    char b[4];
    if (cp < 0x80) {
        b[0] = (char)cp;
        sb_append_n(sb, b, 1);
    } else if (cp < 0x800) {
        b[0] = (char)(0xC0 | (cp >> 6));
        b[1] = (char)(0x80 | (cp & 0x3F));
        sb_append_n(sb, b, 2);
    } else if (cp < 0x10000) {
        b[0] = (char)(0xE0 | (cp >> 12));
        b[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        b[2] = (char)(0x80 | (cp & 0x3F));
        sb_append_n(sb, b, 3);
    } else {
        b[0] = (char)(0xF0 | (cp >> 18));
        b[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        b[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        b[3] = (char)(0x80 | (cp & 0x3F));
        sb_append_n(sb, b, 4);
    }
}

static char *parse_string(parser *p) {
    /* p->s[p->i] == '"' */
    p->i++;
    sbuf sb;
    sb_init(&sb);
    while (p->i < p->n) {
        unsigned char c = (unsigned char)p->s[p->i];
        if (c == '"') {
            p->i++;
            if (sb.oom) {
                sb_free(&sb);
                set_err(p, "out of memory");
                return NULL;
            }
            return sb_take(&sb);
        }
        if (c < 0x20) {
            set_err(p, "control character in string");
            sb_free(&sb);
            return NULL;
        }
        if (c == '\\') {
            p->i++;
            if (p->i >= p->n) break;
            char e = p->s[p->i++];
            switch (e) {
                case '"': sb_append_n(&sb, "\"", 1); break;
                case '\\': sb_append_n(&sb, "\\", 1); break;
                case '/': sb_append_n(&sb, "/", 1); break;
                case 'b': sb_append_n(&sb, "\b", 1); break;
                case 'f': sb_append_n(&sb, "\f", 1); break;
                case 'n': sb_append_n(&sb, "\n", 1); break;
                case 'r': sb_append_n(&sb, "\r", 1); break;
                case 't': sb_append_n(&sb, "\t", 1); break;
                case 'u': {
                    unsigned cp;
                    if (read_hex4(p, &cp) != 0) {
                        set_err(p, "bad \\u escape");
                        sb_free(&sb);
                        return NULL;
                    }
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        unsigned lo;
                        if (p->i + 2 <= p->n && p->s[p->i] == '\\' && p->s[p->i + 1] == 'u') {
                            p->i += 2;
                            if (read_hex4(p, &lo) != 0 || lo < 0xDC00 || lo > 0xDFFF) {
                                set_err(p, "bad surrogate pair");
                                sb_free(&sb);
                                return NULL;
                            }
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            cp = 0xFFFD;
                        }
                    } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                        cp = 0xFFFD;
                    }
                    put_utf8(&sb, cp);
                    break;
                }
                default:
                    set_err(p, "bad escape");
                    sb_free(&sb);
                    return NULL;
            }
            continue;
        }
        sb_append_n(&sb, (const char *)&p->s[p->i], 1);
        p->i++;
    }
    set_err(p, "unterminated string");
    sb_free(&sb);
    return NULL;
}

static jnode *parse_value(parser *p);

static jnode *parse_array(parser *p) {
    p->i++; /* [ */
    jnode *arr = new_node(J_ARR);
    if (!arr) return NULL;
    jnode **tail = &arr->child;
    skip_ws(p);
    if (p->i < p->n && p->s[p->i] == ']') {
        p->i++;
        return arr;
    }
    for (;;) {
        jnode *v = parse_value(p);
        if (!v) {
            json_free(arr);
            return NULL;
        }
        *tail = v;
        tail = &v->next;
        skip_ws(p);
        if (p->i >= p->n) break;
        if (p->s[p->i] == ',') {
            p->i++;
            continue;
        }
        if (p->s[p->i] == ']') {
            p->i++;
            return arr;
        }
        break;
    }
    set_err(p, "expected ',' or ']'");
    json_free(arr);
    return NULL;
}

static jnode *parse_object(parser *p) {
    p->i++; /* { */
    jnode *obj = new_node(J_OBJ);
    if (!obj) return NULL;
    jnode **tail = &obj->child;
    skip_ws(p);
    if (p->i < p->n && p->s[p->i] == '}') {
        p->i++;
        return obj;
    }
    for (;;) {
        skip_ws(p);
        if (p->i >= p->n || p->s[p->i] != '"') {
            set_err(p, "expected a member name");
            json_free(obj);
            return NULL;
        }
        char *key = parse_string(p);
        if (!key) {
            json_free(obj);
            return NULL;
        }
        skip_ws(p);
        if (p->i >= p->n || p->s[p->i] != ':') {
            free(key);
            set_err(p, "expected ':'");
            json_free(obj);
            return NULL;
        }
        p->i++;
        jnode *v = parse_value(p);
        if (!v) {
            free(key);
            json_free(obj);
            return NULL;
        }
        v->key = key;
        *tail = v;
        tail = &v->next;
        skip_ws(p);
        if (p->i >= p->n) break;
        if (p->s[p->i] == ',') {
            p->i++;
            continue;
        }
        if (p->s[p->i] == '}') {
            p->i++;
            return obj;
        }
        break;
    }
    set_err(p, "expected ',' or '}'");
    json_free(obj);
    return NULL;
}

static int match_word(parser *p, const char *w) {
    size_t l = strlen(w);
    if (p->i + l <= p->n && memcmp(p->s + p->i, w, l) == 0) {
        p->i += l;
        return 1;
    }
    return 0;
}

static jnode *parse_number(parser *p) {
    size_t start = p->i;
    if (p->i < p->n && (p->s[p->i] == '-' || p->s[p->i] == '+')) p->i++;
    int digits = 0;
    while (p->i < p->n) {
        char c = p->s[p->i];
        if ((c >= '0' && c <= '9')) {
            digits++;
            p->i++;
        } else if (c == '.' || c == 'e' || c == 'E' || c == '-' || c == '+') {
            p->i++;
        } else {
            break;
        }
    }
    if (!digits || p->i - start > 64) {
        set_err(p, "bad number");
        return NULL;
    }
    jnode *n = new_node(J_NUM);
    if (!n) return NULL;
    n->str = (char *)malloc(p->i - start + 1);
    if (!n->str) {
        free(n);
        return NULL;
    }
    memcpy(n->str, p->s + start, p->i - start);
    n->str[p->i - start] = '\0';
    n->num = strtod(n->str, NULL);
    return n;
}

static jnode *parse_value(parser *p) {
    if (++p->depth > JSON_MAX_DEPTH) {
        set_err(p, "nested too deeply");
        return NULL;
    }
    skip_ws(p);
    jnode *out = NULL;
    if (p->i >= p->n) {
        set_err(p, "unexpected end");
    } else {
        char c = p->s[p->i];
        if (c == '{') out = parse_object(p);
        else if (c == '[') out = parse_array(p);
        else if (c == '"') {
            char *s = parse_string(p);
            if (s) {
                out = new_node(J_STR);
                if (out) out->str = s;
                else free(s);
            }
        } else if (match_word(p, "true")) {
            out = new_node(J_BOOL);
            if (out) out->boolean = 1;
        } else if (match_word(p, "false")) {
            out = new_node(J_BOOL);
        } else if (match_word(p, "null")) {
            out = new_node(J_NULL);
        } else if (c == '-' || (c >= '0' && c <= '9')) {
            out = parse_number(p);
        } else {
            set_err(p, "unexpected character");
        }
    }
    p->depth--;
    return out;
}

jnode *json_parse(const char *text, size_t len, char *err, size_t errlen) {
    parser p = {text, len, 0, err, errlen, 0};
    if (err && errlen) err[0] = '\0';
    jnode *root = parse_value(&p);
    if (!root) return NULL;
    skip_ws(&p);
    if (p.i != p.n) {
        set_err(&p, "trailing characters");
        json_free(root);
        return NULL;
    }
    return root;
}

jnode *json_get(const jnode *obj, const char *key) {
    if (!obj || obj->type != J_OBJ) return NULL;
    for (jnode *c = obj->child; c; c = c->next)
        if (c->key && strcmp(c->key, key) == 0) return c;
    return NULL;
}

jnode *json_path(const jnode *root, const char *path) {
    char part[128];
    const jnode *cur = root;
    const char *s = path;
    while (cur && *s) {
        const char *slash = strchr(s, '/');
        size_t l = slash ? (size_t)(slash - s) : strlen(s);
        if (l == 0 || l >= sizeof part) return NULL;
        memcpy(part, s, l);
        part[l] = '\0';
        cur = json_get(cur, part);
        s += l + (slash ? 1 : 0);
    }
    return (jnode *)cur;
}

const char *json_text(const jnode *n) {
    if (!n) return NULL;
    if (n->type == J_STR || n->type == J_NUM) return n->str;
    if (n->type == J_BOOL) return n->boolean ? "true" : "false";
    return NULL;
}

int json_truthy(const jnode *n) {
    if (!n) return 0;
    if (n->type == J_BOOL) return n->boolean;
    if (n->type == J_NUM) return n->num != 0;
    if (n->type == J_STR) return n->str && n->str[0] && strcmp(n->str, "0") != 0 && strcmp(n->str, "false") != 0;
    return n->type == J_ARR || n->type == J_OBJ;
}

void sb_json_str(sbuf *sb, const char *s) {
    sb_append_n(sb, "\"", 1);
    if (s) {
        for (const unsigned char *c = (const unsigned char *)s; *c; c++) {
            switch (*c) {
                case '"': sb_append_n(sb, "\\\"", 2); break;
                case '\\': sb_append_n(sb, "\\\\", 2); break;
                case '\n': sb_append_n(sb, "\\n", 2); break;
                case '\r': sb_append_n(sb, "\\r", 2); break;
                case '\t': sb_append_n(sb, "\\t", 2); break;
                default:
                    if (*c < 0x20) sb_appendf(sb, "\\u%04x", *c);
                    else sb_append_n(sb, (const char *)c, 1);
            }
        }
    }
    sb_append_n(sb, "\"", 1);
}
