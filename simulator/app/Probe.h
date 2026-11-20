#include <stddef.h>
#include <stdint.h>

// Both functions block. Use a fresh app-writable absolute home and an output buffer
// of at least 4096 bytes, large enough for the JSON reply including its final NUL.
// Return 0 for JSON, 1 for an error (diagnostic in out when space permits).
// Reads challenge.bin and returns {uuid, base64}.
int32_t codex_probe(const char *home, char *out, size_t cap);

// Targets review.txt only. NULL approved_b64 sends no write; a non-NULL string
// (including "" for an empty file) authorizes one fs/writeFile before fs/readFile.
// Returns {uuid, base64, writes}, with base64 from the read and writes either 0 or 1.
int32_t codex_edit_probe(const char *home, const char *approved_b64, char *out, size_t cap);
