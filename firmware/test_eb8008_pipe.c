// test_eb8008_pipe.c -- stdin/stdout harness around eb8008_handle for the
// golden differential test (host/tests/test_eb_golden.py, VPLAN SWEB-9).
//
// Protocol: first output line is the decimal base address of a 4 KiB scratch
// bus window. Then, per request: one hex line in, one hex reply line out
// (empty line for no-reply). The python side encodes requests with litex's
// own EtherbonePacket classes and decodes replies the same way -- proving
// the C server speaks litex's dialect byte-for-byte.
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "eb8008.h"

#define BUS_BASE 0xf0000000u
#define BUS_SIZE 0x1000u
static uint32_t bus_mem[BUS_SIZE / 4];

uint32_t eb8008_bus_read(uint32_t addr)
{
    if (addr - BUS_BASE < BUS_SIZE)
        return bus_mem[(addr - BUS_BASE) / 4];
    return 0;
}

void eb8008_bus_write(uint32_t addr, uint32_t val)
{
    if (addr - BUS_BASE < BUS_SIZE)
        bus_mem[(addr - BUS_BASE) / 4] = val;
}

int main(void)
{
    char line[16384];
    uint8_t req[4096], resp[4096 + 64];

    printf("%u\n", BUS_BASE);
    fflush(stdout);

    while (fgets(line, sizeof(line), stdin)) {
        int len = 0;
        for (char *p = line; p[0] && p[1] && p[0] != '\n' && len < (int)sizeof(req); p += 2) {
            unsigned v;
            if (sscanf(p, "%2x", &v) != 1)
                break;
            req[len++] = (uint8_t)v;
        }
        int rlen = eb8008_handle(req, len, resp);
        for (int i = 0; i < rlen; i++)
            printf("%02x", resp[i]);
        printf("\n");
        fflush(stdout);
    }
    return 0;
}
