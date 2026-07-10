// dhcp8008.c
//
// DHCPv4 packet builder/parser for the b8008_net firmware's DHCP client.
// Pure byte-buffer code: no networking, no CSR access, no state -- so it
// links and runs unmodified under a host `cc` (see test_dhcp_host.c) as
// well as the riscv32 freestanding build. main.c owns the socket plumbing
// (libliteeth's microudp) and the retry/renewal policy around this.
//
// Two things worth knowing before touching main.c's use of this code:
//
// 1. chaddr (the hardware-address field this module writes into the BOOTP
//    header and option 61) is the *Etherbone* MAC (0x10e2d5000001), not the
//    Ethernet frame's actual source MAC (the CPU's ethmac interface,
//    0x10e2d5000002) -- main.c hands udp_start() the ethmac address for
//    framing while passing the Etherbone address in here as chaddr. This is
//    legal DHCP (chaddr only has to be a hardware address the client can be
//    reached at, and the DHCPDISCOVER's broadcast flag -- which this module
//    always sets -- makes the server's reply come back as an L2/L3
//    broadcast that the CPU sees regardless of which MAC chaddr names). It
//    is exactly the setup Etherbone needs: the lease ends up addressed to
//    Etherbone's identity even though the CPU's ethmac did the talking.
//    Some DHCP-snooping / port-security switches bind a lease to the
//    frame's source MAC and will drop a reply whose chaddr disagrees with
//    it. Home routers and unmanaged switches don't care. A managed-switch
//    demo venue might -- if DHCP silently stops working there, this
//    mismatch is the first thing to check.
//
// 2. "Renewal" in this firmware is a full periodic re-acquisition: every
//    lease_secs/2, main.c throws away whatever it has and runs a fresh
//    DISCOVER -> OFFER -> REQUEST -> ACK cycle (see dhcp_run() in main.c).
//    It is NOT an RFC 2131 4.4.5 unicast RENEWING-state exchange (which
//    would unicast a REQUEST straight to the lease's server, skipping
//    DISCOVER). For a single fixed appliance like this, the two approaches
//    reach the same steady state -- same server, same offered address, same
//    lease -- so the simpler one was chosen. Call it what it is: this is
//    re-acquisition, not RENEW.

#include "dhcp8008.h"

#include <string.h>

// ---- byte-level helpers (avoid depending on struct packing/endianness) ---

static void put8(uint8_t *p, uint8_t v)
{
    p[0] = v;
}

static void put16be(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void put32be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t get32be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

// ---- builder ---------------------------------------------------------------

// Fills the fixed BOOTP header (op..file) + magic cookie, zeroing everything
// else (secs, ciaddr, yiaddr, siaddr, giaddr, sname, file, chaddr padding).
// Returns the offset of the first option byte (DHCP_OPTIONS_OFFSET).
static int build_header(uint8_t *buf, const uint8_t chaddr[6], uint32_t xid)
{
    memset(buf, 0, DHCP_OPTIONS_OFFSET);

    put8(buf + 0, DHCP_OP_BOOTREQUEST);
    put8(buf + 1, DHCP_HTYPE_ETHERNET);
    put8(buf + 2, DHCP_HLEN_ETHERNET);
    put8(buf + 3, 0); // hops
    put32be(buf + 4, xid);
    put16be(buf + 8, 0);      // secs
    put16be(buf + 10, 0x8000); // flags: broadcast (see caveat 1 above)
    // ciaddr(12)/yiaddr(16)/siaddr(20)/giaddr(24) stay 0.0.0.0 -- an
    // unleased client has no address to claim yet.
    memcpy(buf + 28, chaddr, 6); // chaddr[16]; bytes 6..15 stay zero-padded
    // sname[64] @44, file[128] @108 stay zero (unused).
    put32be(buf + 236, DHCP_MAGIC_COOKIE);

    return DHCP_OPTIONS_OFFSET;
}

static int put_option(uint8_t *buf, int off, uint8_t code,
                       const uint8_t *data, uint8_t len)
{
    buf[off++] = code;
    buf[off++] = len;
    memcpy(buf + off, data, len);
    return off + len;
}

// Options common to DISCOVER and REQUEST: message type, parameter request
// list (subnet mask/router/DNS), hostname, client identifier.
static int build_common_options(uint8_t *buf, int off,
                                 const uint8_t chaddr[6], uint8_t msg_type)
{
    uint8_t v = msg_type;
    off = put_option(buf, off, DHCP_OPT_MSG_TYPE, &v, 1);

    static const uint8_t param_req_list[3] = {1, 3, 6};
    off = put_option(buf, off, DHCP_OPT_PARAM_REQ_LIST, param_req_list, 3);

    off = put_option(buf, off, DHCP_OPT_HOSTNAME,
                      (const uint8_t *)DHCP_HOSTNAME,
                      (uint8_t)(sizeof(DHCP_HOSTNAME) - 1));

    uint8_t client_id[7];
    client_id[0] = DHCP_HTYPE_ETHERNET;
    memcpy(client_id + 1, chaddr, 6);
    off = put_option(buf, off, DHCP_OPT_CLIENT_ID, client_id, 7);

    return off;
}

static int finish_options(uint8_t *buf, int off)
{
    buf[off++] = DHCP_OPT_END;
    while (off < DHCP_MIN_PACKET_LEN)
        buf[off++] = DHCP_OPT_PAD;
    return off;
}

int dhcp_build_discover(uint8_t *buf, const uint8_t chaddr[6], uint32_t xid)
{
    int off = build_header(buf, chaddr, xid);
    off = build_common_options(buf, off, chaddr, DHCP_MSG_DISCOVER);
    return finish_options(buf, off);
}

int dhcp_build_request(uint8_t *buf, const uint8_t chaddr[6], uint32_t xid,
                        uint32_t requested_ip, uint32_t server_id)
{
    int off = build_header(buf, chaddr, xid);
    off = build_common_options(buf, off, chaddr, DHCP_MSG_REQUEST);

    uint8_t ipbuf[4];
    put32be(ipbuf, requested_ip);
    off = put_option(buf, off, DHCP_OPT_REQUESTED_IP, ipbuf, 4);

    put32be(ipbuf, server_id);
    off = put_option(buf, off, DHCP_OPT_SERVER_ID, ipbuf, 4);

    return finish_options(buf, off);
}

// ---- parser -----------------------------------------------------------------

static int parse_reply(const uint8_t *buf, int len, uint32_t xid,
                        uint8_t want_msg_type, uint32_t *ip, uint32_t *server,
                        uint32_t *lease_secs)
{
    if (len < DHCP_OPTIONS_OFFSET)
        return 0;
    if (buf[0] != DHCP_OP_BOOTREPLY)
        return 0;
    if (get32be(buf + 4) != xid)
        return 0;
    if (get32be(buf + 236) != DHCP_MAGIC_COOKIE)
        return 0;

    uint32_t yiaddr = get32be(buf + 16);
    uint32_t got_server = 0;
    uint32_t got_lease = 0;
    int msg_type = -1;

    int off = DHCP_OPTIONS_OFFSET;
    while (off < len) {
        uint8_t code = buf[off++];
        if (code == DHCP_OPT_END)
            break;
        if (code == DHCP_OPT_PAD)
            continue;
        if (off >= len)
            break; // truncated option TLV
        uint8_t olen = buf[off++];
        if (off + olen > len)
            break; // truncated option value

        switch (code) {
        case DHCP_OPT_MSG_TYPE:
            if (olen >= 1)
                msg_type = buf[off];
            break;
        case DHCP_OPT_SERVER_ID:
            if (olen >= 4)
                got_server = get32be(buf + off);
            break;
        case DHCP_OPT_LEASE_TIME:
            if (olen >= 4)
                got_lease = get32be(buf + off);
            break;
        default:
            break;
        }

        off += olen;
    }

    if (msg_type != want_msg_type)
        return 0;

    if (ip)
        *ip = yiaddr;
    if (server)
        *server = got_server;
    if (lease_secs)
        *lease_secs = got_lease;

    return 1;
}

int dhcp_parse_offer(const uint8_t *buf, int len, uint32_t xid,
                      uint32_t *ip, uint32_t *server, uint32_t *lease_secs)
{
    return parse_reply(buf, len, xid, DHCP_MSG_OFFER, ip, server, lease_secs);
}

int dhcp_parse_ack(const uint8_t *buf, int len, uint32_t xid,
                    uint32_t *ip, uint32_t *lease_secs)
{
    return parse_reply(buf, len, xid, DHCP_MSG_ACK, ip, NULL, lease_secs);
}
