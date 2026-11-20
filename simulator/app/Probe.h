#include <stddef.h>
#include <stdint.h>

// On success, return zero and write a NUL-terminated UUID to out.
int32_t codex_probe(const char *home, char *out, size_t cap);
