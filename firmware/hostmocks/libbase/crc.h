#ifndef HOSTMOCK_CRC_H
#define HOSTMOCK_CRC_H
#include <stdint.h>
static inline uint32_t crc32(const unsigned char *d, unsigned int len) { (void)d; (void)len; return 0; }
#endif
