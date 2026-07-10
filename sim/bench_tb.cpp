// bench_tb.cpp - Verilator C++ driver for the B8008Core bench (Task 9).
// ----------------------------------------------------------------------------
// Pre-hardware gate. Drives build/bench_core.v (B8008Core wrapped with a raw
// CSR bus + wishbone bus, both clock domains and resets exposed as ports). No
// network, no TAP: this validates exactly the custom logic sitting between the
// stock LiteEth/Etherbone transport (proven on real hardware in Task 13) and
// the GHDL-converted Intel-8008 monitor netlist.
//
// Three checks (PASS/FAIL each; nonzero exit on any FAIL):
//   1. Wishbone RAM window: write 0..255 to word offsets 0x1000..0x10FF, read
//      back, assert equal. Proves the wishbone shim + BRAM port B. Run with the
//      b8008 core held in reset so its RAM port A cannot race the window.
//   2. Reset-release boot: poll rxlevel via CSR (<=450 ms sim), drain rxtx,
//      assert the "8008 " banner prefix. Proves netlist auto-start + console
//      bridge CSR FIFOs + b8008-domain RAM/ROM ports + the whole boot path.
//   3. Control-CSR CDC + restart: pulse ctl.run_stop -> poll status.is_running
//      flips to 0 (PulseSynchronizer sys->b8008 + status MultiReg b8008->sys).
//      Pulse again: run-from-stopped is a RESTART (debug_clock_control fires a
//      500-clk reset_request), the monitor reboots and re-emits the banner.
//      Pass criterion = banner REAPPEARS within 450 ms, not an instant prompt.
//
// Clocking: sys and b8008 both 25 MHz, driven phase-offset (b8008 posedge one
// eval before the sys posedge, every cycle) so the CDC paths are genuinely
// crossed. rom.init ($readmemh) sits next to bench_core.v in build/, so run
// this binary from build/.
// ----------------------------------------------------------------------------

#include "Vbench_core.h"
#include "verilated.h"

#include <cstdio>
#include <cstdint>
#include <vector>
#include <cstdlib>

// CSR word addresses (== index in B8008Core.get_csrs(sort=True), 32-bit bus).
enum {
    CSR_CTL     = 0,   // pulse fields: run_stop=bit0, step_cycle=1, step_sync=2, int_req=3
    CSR_STATUS  = 1,   // is_running=bit0, triggered=bit1, tx_busy=bit2
    CSR_RXTX    = 2,   // read pops one RX byte
    CSR_RXLEVEL = 3,   // RX FIFO fill level
    CSR_TXFULL  = 4,
    CSR_RXEMPTY = 5,
};

static const uint32_t SYS_HZ    = 25000000;
static const uint32_t CYC_PER_MS = SYS_HZ / 1000;   // 25000

// Expected banner prefix (same source as Task 5 / netlist_tb.v: "8008 ").
static const uint8_t BANNER[] = {0x38, 0x30, 0x30, 0x38, 0x20};
static const int      BANNER_N = 5;

static Vbench_core* dut;
static uint64_t     g_cycles = 0;

// One 40 ns cycle: b8008 posedge, then sys posedge (phase offset), then both
// fall. Exactly one edge of each clock per call; four evals.
static void cycle() {
    dut->b8008_clk = 1; dut->sys_clk = 0; dut->eval();  // b8008 rising
    dut->sys_clk   = 1;                  dut->eval();    // sys rising (offset)
    dut->b8008_clk = 0;                  dut->eval();
    dut->sys_clk   = 0;                  dut->eval();
    g_cycles++;
}

static void run(uint64_t n) { for (uint64_t i = 0; i < n; i++) cycle(); }

// ---- CSR bus (sys domain, 1-cycle handshake) -------------------------------
// A CSR-bus write asserts the target CSR's `re` for one sys cycle (csr_bus:
// c.re.eq(bus.we)); that is how pulse fields fire. A CSR-bus read asserts `we`
// (c.we.eq(bus.re)) and registers dat_r at the same sys posedge -- for rxtx the
// read also pops one FIFO entry.
static void csr_write(uint32_t adr, uint32_t data) {
    dut->csr_adr   = adr;
    dut->csr_dat_w = data;
    dut->csr_we    = 1;
    cycle();
    dut->csr_we    = 0;
}

static uint32_t csr_read(uint32_t adr) {
    dut->csr_adr = adr;
    dut->csr_re  = 1;
    cycle();                       // sys posedge registers dat_r (and pops rxtx)
    dut->csr_re  = 0;
    return dut->csr_dat_r;
}

// ---- Wishbone (sys domain, classic single-beat, ack = cyc&stb&~ack) --------
static void wb_write(uint32_t word_adr, uint8_t data) {
    dut->wb_adr   = word_adr;
    dut->wb_dat_w = data;
    dut->wb_sel   = 0xF;
    dut->wb_we    = 1;
    dut->wb_cyc   = 1;
    dut->wb_stb   = 1;
    do { cycle(); } while (!dut->wb_ack);   // write commits the cycle ack rises
    dut->wb_cyc = dut->wb_stb = dut->wb_we = 0;
    cycle();
}

static uint8_t wb_read(uint32_t word_adr) {
    dut->wb_adr = word_adr;
    dut->wb_sel = 0xF;
    dut->wb_we  = 0;
    dut->wb_cyc = 1;
    dut->wb_stb = 1;
    do { cycle(); } while (!dut->wb_ack);
    uint8_t v = dut->wb_dat_r & 0xFF;       // pb.dat_r valid the cycle ack rises
    dut->wb_cyc = dut->wb_stb = 0;
    cycle();
    return v;
}

// ---- helpers ---------------------------------------------------------------
static void drain_rx(std::vector<uint8_t>& buf) {
    while (csr_read(CSR_RXLEVEL) != 0)
        buf.push_back(csr_read(CSR_RXTX) & 0xFF);
}

static bool banner_prefix_ok(const std::vector<uint8_t>& buf) {
    if ((int)buf.size() < BANNER_N) return false;
    for (int i = 0; i < BANNER_N; i++)
        if (buf[i] != BANNER[i]) return false;
    return true;
}

static void dump_bytes(const std::vector<uint8_t>& buf) {
    printf("        got %zu byte(s):", buf.size());
    for (size_t i = 0; i < buf.size() && i < 16; i++) printf(" 0x%02x", buf[i]);
    printf("\n");
}

// Run the b8008 in ~2 ms chunks up to `budget_ms`, draining RX, until the
// banner prefix arrives. Returns arrival time in ms (or -1 on timeout).
static double wait_for_banner(std::vector<uint8_t>& rx, uint32_t budget_ms,
                              const char* tag) {
    uint64_t start = g_cycles;
    uint64_t deadline = (uint64_t)budget_ms * CYC_PER_MS;
    uint64_t next_hb = 50;   // heartbeat every ~50 ms
    while (g_cycles - start < deadline) {
        run(2000);                          // ~80 us
        drain_rx(rx);
        if (banner_prefix_ok(rx)) {
            return (double)(g_cycles - start) / CYC_PER_MS;
        }
        double elapsed_ms = (double)(g_cycles - start) / CYC_PER_MS;
        if (elapsed_ms >= next_hb) {
            printf("        [%s] ... %.0f ms, rx=%zu byte(s)\n",
                   tag, elapsed_ms, rx.size());
            next_hb += 50;
        }
    }
    return -1.0;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new Vbench_core;

    int failures = 0;

    // Init all inputs low, then a short synchronous reset on BOTH domains.
    dut->sys_clk = dut->b8008_clk = 0;
    dut->sys_rst = 1;  dut->b8008_rst = 1;
    dut->csr_adr = dut->csr_re = dut->csr_we = dut->csr_dat_w = 0;
    dut->wb_adr = dut->wb_dat_w = dut->wb_sel = 0;
    dut->wb_cyc = dut->wb_stb = dut->wb_we = 0;
    run(16);
    // Release sys (CSR + wishbone fabric live); keep b8008 in reset for check 1
    // so its RAM port A cannot race the wishbone window.
    dut->sys_rst = 0;
    run(8);

    // ------------------------------------------------------------------ check 1
    printf("=== CHECK 1: wishbone RAM window (0x1000..0x10FF) ===\n");
    {
        const uint32_t base = 0x1000;
        for (int i = 0; i < 256; i++) wb_write(base + i, (uint8_t)i);
        int mismatches = 0;
        uint8_t first_bad = 0; uint32_t first_bad_adr = 0;
        for (int i = 0; i < 256; i++) {
            uint8_t got = wb_read(base + i);
            if (got != (uint8_t)i) {
                if (!mismatches) { first_bad = got; first_bad_adr = base + i; }
                mismatches++;
            }
        }
        if (mismatches == 0) {
            printf("CHECK 1 PASS: 256 bytes written+read back @ word 0x%04x "
                   "(%.3f ms sim so far)\n", base, (double)g_cycles / CYC_PER_MS);
        } else {
            printf("CHECK 1 FAIL: %d mismatch(es); first @0x%04x got 0x%02x\n",
                   mismatches, first_bad_adr, first_bad);
            failures++;
        }
    }

    // ------------------------------------------------------------------ check 2
    printf("=== CHECK 2: reset-release boot -> banner \"8008 \" ===\n");
    std::vector<uint8_t> rx;
    {
        dut->b8008_rst = 0;              // release the core; it auto-starts
        run(8);
        uint64_t t0 = g_cycles;
        double t_ms = wait_for_banner(rx, 450, "boot");
        (void)t0;
        if (t_ms >= 0.0) {
            printf("CHECK 2 PASS: banner prefix at %.1f ms sim\n", t_ms);
            dump_bytes(rx);
        } else {
            printf("CHECK 2 FAIL: no banner within 450 ms\n");
            dump_bytes(rx);
            failures++;
        }
    }

    // ------------------------------------------------------------------ check 3
    printf("=== CHECK 3: ctl.run_stop CDC -> stop, then restart -> banner ===\n");
    {
        uint32_t st = csr_read(CSR_STATUS);
        printf("        status before stop: is_running=%u\n", st & 1);

        // Pulse run_stop; poll is_running -> 0 (up to 100 ms).
        csr_write(CSR_CTL, 0x1);
        bool stopped = false;
        uint64_t start = g_cycles;
        while (g_cycles - start < (uint64_t)100 * CYC_PER_MS) {
            run(500);
            if ((csr_read(CSR_STATUS) & 1) == 0) { stopped = true; break; }
        }
        if (stopped) {
            printf("        is_running -> 0 after %.3f ms\n",
                   (double)(g_cycles - start) / CYC_PER_MS);
        } else {
            printf("CHECK 3 FAIL: is_running never cleared after run_stop\n");
            failures++;
        }

        if (stopped) {
            // Small settling gap so the PulseSynchronizer is idle, then pulse
            // again: run-from-stopped is a RESTART -> monitor reboots.
            run(2000);
            rx.clear();
            csr_write(CSR_CTL, 0x1);
            run(8);
            double t_ms = wait_for_banner(rx, 450, "restart");
            if (t_ms >= 0.0) {
                printf("CHECK 3 PASS: banner REAPPEARED at %.1f ms after restart\n",
                       t_ms);
                dump_bytes(rx);
            } else {
                printf("CHECK 3 FAIL: banner did not reappear within 450 ms\n");
                dump_bytes(rx);
                failures++;
            }
        }
    }

    printf("=== TOTAL sim time %.3f ms; %d check(s) failed ===\n",
           (double)g_cycles / CYC_PER_MS, failures);
    dut->final();
    delete dut;
    return failures ? 1 : 0;
}
