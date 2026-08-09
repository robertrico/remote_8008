// eb8008.h -- software Etherbone server over the CPU ethmac (see eb8008.c).
#ifndef EB8008_H
#define EB8008_H

#include <stdint.h>

// Handle one Etherbone request datagram. `req`/`req_len` is the UDP payload
// received on the Etherbone port; the reply payload (if any) is written to
// `resp` (caller provides >= req_len + 16 bytes; replies never exceed that).
// Returns the reply length in bytes, or 0 for no reply (bad packet, or a
// write-only request which is fire-and-forget in the Etherbone dialect).
int eb8008_handle(const uint8_t *req, int req_len, uint8_t *resp);

#endif
