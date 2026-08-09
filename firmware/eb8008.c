// eb8008.c -- software Etherbone server over the CPU ethmac.
//
// Replaces the LiteEth *hardware* Etherbone core: the hybrid MAC interface
// (one PHY shared by hardware Etherbone + CPU ethmac) is dead on silicon at
// gigabit -- see the hybrid-debug-2026-08-08 tag for the full bring-up
// post-mortem. The SoC now runs the proven plain-ethmac configuration and
// this file speaks just enough of the litex Etherbone dialect (the subset
// litex's CommUDP actually emits, see litex/tools/remote/comm_udp.py and
// etherbone.py) for RemoteClient/litex_server/b8008net to work unchanged:
//
//   probe  : pf=1 packet          -> header-only reply with pr=1
//   write  : record with wcount   -> 32-bit writes to base_addr + 4*i, no ack
//   read   : record with rcount   -> reply record with wcount=rcount,
//            base_write_addr = the request's base_ret_addr (CommUDP's
//            correlation id), data = the values read
//
// All values are 32-bit big-endian on the wire (addr_size = port_size = 4).
// The VexRiscv performs the accesses directly on its own bus, so anything
// RemoteClient could reach through the hardware core (CSRs, memories) is
// reachable here too.

#include <stdint.h>

#include "eb8008.h"

#define EB_MAGIC0 0x4e
#define EB_MAGIC1 0x6f

// Packet header flag bits (byte 2 of the header).
#define EB_PF 0x01
#define EB_PR 0x02

static uint32_t get_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static void put_header(uint8_t *p, int pr)
{
    p[0] = EB_MAGIC0;
    p[1] = EB_MAGIC1;
    p[2] = (uint8_t)((1 << 4) | (pr ? EB_PR : 0)); // version 1 [+ probe reply]
    p[3] = 0x44;                                   // addr_size 4, port_size 4
    p[4] = p[5] = p[6] = p[7] = 0;
}

int eb8008_handle(const uint8_t *req, int req_len, uint8_t *resp)
{
    if (req_len < 8 || req[0] != EB_MAGIC0 || req[1] != EB_MAGIC1)
        return 0;

    // Probe: header-only reply, probe-reply flag set (plus the same 4 bytes
    // of padding CommUDP.probe() itself appends).
    if (req[2] & EB_PF) {
        put_header(resp, 1);
        put_be32(resp + 8, 0);
        return 12;
    }

    const uint8_t *p   = req + 8;
    const uint8_t *end = req + req_len;
    int resp_len = 0;

    while (p + 4 <= end) {
        int wcount = p[2];
        int rcount = p[3];
        p += 4;

        if (wcount) {
            if (p + 4 * (1 + wcount) > end)
                break;
            uint32_t addr = get_be32(p);
            p += 4;
            for (int i = 0; i < wcount; i++) {
                *(volatile uint32_t *)(uintptr_t)(addr + 4 * i) = get_be32(p);
                p += 4;
            }
        }

        if (rcount) {
            if (p + 4 * (1 + rcount) > end)
                break;
            uint32_t base_ret_addr = get_be32(p);
            p += 4;

            if (resp_len == 0) {
                put_header(resp, 0);
                resp_len = 8;
            }
            uint8_t *rec = resp + resp_len;
            rec[0] = 0;
            rec[1] = 0x0f;             // byte_enable
            rec[2] = (uint8_t)rcount;  // reads answered as writes
            rec[3] = 0;
            put_be32(rec + 4, base_ret_addr);
            resp_len += 8;

            for (int i = 0; i < rcount; i++) {
                uint32_t addr = get_be32(p);
                p += 4;
                put_be32(resp + resp_len,
                         *(volatile uint32_t *)(uintptr_t)addr);
                resp_len += 4;
            }
        }
    }

    return resp_len;
}
