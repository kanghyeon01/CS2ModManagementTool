/* Minimal JSON reader and writer helpers for the sentinel (no dependencies). */
#ifndef CS2MMT_JSON_H
#define CS2MMT_JSON_H

#include <stddef.h>

#include "util.h"

typedef enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ } jtype;

typedef struct jnode {
    jtype type;
    char *key;           /* member name when inside an object, else NULL */
    char *str;           /* J_STR: decoded UTF-8; J_NUM: the number as written */
    double num;          /* J_NUM */
    int boolean;         /* J_BOOL */
    struct jnode *child; /* first element / member */
    struct jnode *next;  /* next sibling */
} jnode;

/* Parses ``len`` bytes. Returns NULL on error and writes a message to ``err``. */
jnode *json_parse(const char *text, size_t len, char *err, size_t errlen);
void json_free(jnode *n);

/* Member of an object by name (first match), or NULL. */
jnode *json_get(const jnode *obj, const char *key);
/* Walks "a/b/c" through objects; NULL when any step is missing. */
jnode *json_path(const jnode *root, const char *path);
/* String value of a J_STR, the text of a J_NUM, "true"/"false" for J_BOOL, else NULL. */
const char *json_text(const jnode *n);
int json_truthy(const jnode *n);

/* Appends ``s`` as a quoted JSON string. */
void sb_json_str(sbuf *sb, const char *s);

#endif
