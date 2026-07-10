// netlist_tb.v - Gate-level boot testbench for the GHDL->Verilog netlist
// ----------------------------------------------------------------------------
// The gate-level counterpart to sim/b8008_net_core_tb.vhdl: proves the
// converted build/b8008_net_core.v netlist (see Makefile `convert` target) still boots
// the b8008_monitor firmware headless (auto-start, no button press) and
// emits its UART banner, exactly like the pre-conversion VHDL sim did.
//
// Boot sequence (25 MHz, ctl_* inputs held low): POR (~21 ms) -> auto-start
// pulse (2 ms later) -> debug reset -> bootstrap RST 0 jam -> T1I detect ->
// bootstrap_done -> CPU fetches from ROM -> firmware delay_short (~380 ms)
// -> send_banner -> OUT 9 UART bytes. Banner arrives ~315 ms in; a 450 ms
// timeout matches the GHDL TB's --stop-time=460ms margin.
//
// Expected banner prefix (from b8008_monitor.asm send_banner, "8008 "):
//   '8'=0x38  '0'=0x30  '0'=0x30  '8'=0x38  ' '=0x20
//
// PASS: the five banner bytes above arrive, in order, before the timeout.
// FAIL: timeout, or any byte mismatches.
//
// Run via `make sim-netlist` (see Makefile) - this file assumes cwd is
// projects/b8008_net (net_rom's ROM_FILE path in this module is relative to
// that directory).
// ----------------------------------------------------------------------------

// No `timescale directive: the GHDL-emitted netlist and ghdl_gates.v carry
// none either, and Verilator's default time unit (1 unit = 1ns here) keeps
// every #-delay in this file consistent with those modules without the
// TIMESCALEMOD warning spam a mismatched directive would produce.

module netlist_tb;

    reg clk = 0;
    reg rst = 1;

    // Console serial
    wire uart_tx;
    reg  uart_rx = 1;

    // Control pulses (held low: auto-start must boot it headless)
    reg ctl_run_stop   = 0;
    reg ctl_step_cycle = 0;
    reg ctl_step_sync  = 0;
    reg ctl_int        = 0;
    reg [2:0] ctl_int_vector = 3'b000;

    // Status
    wire sts_is_running, sts_triggered, sts_tx_busy;

    // External RAM bus (absolute 14-bit 8008 address)
    wire [13:0] ram_addr;
    wire [7:0]  ram_wdata;
    wire [7:0]  ram_rdata;
    wire        ram_rw_n;
    wire        ram_cs_n;

    // External ROM bus (4KB, 12 address bits)
    wire [11:0] rom_addr;
    wire [7:0]  rom_data;

    // Debug (logic-analyzer) outputs
    wire [7:0] dbg_d;
    wire dbg_s0, dbg_s1, dbg_s2, dbg_sync, dbg_phi1, dbg_phi2, dbg_int;

    // ------------------------------------------------------------------
    // Device under test: the converted gate-level netlist
    // ------------------------------------------------------------------
    b8008_net_core dut (
        .clk            (clk),
        .rst            (rst),
        .uart_tx        (uart_tx),
        .uart_rx        (uart_rx),
        .ctl_run_stop   (ctl_run_stop),
        .ctl_step_cycle (ctl_step_cycle),
        .ctl_step_sync  (ctl_step_sync),
        .ctl_int        (ctl_int),
        .ctl_int_vector (ctl_int_vector),
        .sts_is_running (sts_is_running),
        .sts_triggered  (sts_triggered),
        .sts_tx_busy    (sts_tx_busy),
        .ram_addr       (ram_addr),
        .ram_wdata      (ram_wdata),
        .ram_rdata      (ram_rdata),
        .ram_rw_n       (ram_rw_n),
        .ram_cs_n       (ram_cs_n),
        .rom_addr       (rom_addr),
        .rom_data       (rom_data),
        .dbg_d          (dbg_d),
        .dbg_s0         (dbg_s0),
        .dbg_s1         (dbg_s1),
        .dbg_s2         (dbg_s2),
        .dbg_sync       (dbg_sync),
        .dbg_phi1       (dbg_phi1),
        .dbg_phi2       (dbg_phi2),
        .dbg_int        (dbg_int)
    );

    // ------------------------------------------------------------------
    // External memories (sim/models.v) - wired to the core's external buses
    // ------------------------------------------------------------------
    net_ram u_ram (
        .clk   (clk),
        .addr  (ram_addr),
        .wdata (ram_wdata),
        .rdata (ram_rdata),
        .rw_n  (ram_rw_n),
        .cs_n  (ram_cs_n)
    );

    net_rom #(
        .ROM_FILE("../b8008_monitor/src/rom_baked.mem")
    ) u_rom (
        .clk  (clk),
        .addr (rom_addr),
        .rdata(rom_data)
    );

    // ------------------------------------------------------------------
    // Clock and reset (25 MHz core clock, matches sim/b8008_net_core_tb.vhdl)
    // ------------------------------------------------------------------
    always #20 clk = ~clk;   // 40 ns period = 25 MHz

    initial begin
        rst = 1;
        #1000;                // 1 us of synchronous reset, several clocks
        rst = 0;
    end

    // ------------------------------------------------------------------
    // Diagnostics: bootstrap interrupt activity on dbg_int
    // ------------------------------------------------------------------
    initial begin
        forever begin
            @(posedge dbg_int);
            $display("[%0t ns] dbg_int RISE (bootstrap jam started)", $time);
            @(negedge dbg_int);
            $display("[%0t ns] dbg_int FALL (bootstrap complete)", $time);
        end
    end

    // ------------------------------------------------------------------
    // UART RX decoder on uart_tx: 115200 baud, LSB first, no parity,
    // 1 stop bit. Checks each received byte against the expected banner
    // prefix, in order.
    // ------------------------------------------------------------------
    localparam real BIT_TIME = 1_000_000_000.0 / 115200.0;  // ns
    localparam integer NUM_EXPECT = 5;

    reg [7:0] expected [0:NUM_EXPECT-1];
    initial begin
        expected[0] = 8'h38;  // '8'
        expected[1] = 8'h30;  // '0'
        expected[2] = 8'h30;  // '0'
        expected[3] = 8'h38;  // '8'
        expected[4] = 8'h20;  // ' '
    end

    integer byte_count = 0;
    reg     got_prefix = 0;
    reg     prefix_ok  = 1;

    initial begin
        integer idx;
        reg [7:0] rxbyte;
        integer i;

        idx = 0;
        forever begin
            @(negedge uart_tx);          // start bit
            #(BIT_TIME * 1.5);           // middle of bit 0
            for (i = 0; i < 8; i = i + 1) begin
                rxbyte[i] = uart_tx;
                #(BIT_TIME);
            end
            $display("[%0t ns] UART byte %0d: 0x%02x", $time, idx, rxbyte);
            if (idx < NUM_EXPECT) begin
                if (rxbyte !== expected[idx]) begin
                    prefix_ok = 0;
                    $display("  MISMATCH at byte %0d: got 0x%02x expected 0x%02x",
                              idx, rxbyte, expected[idx]);
                end
                if (idx == NUM_EXPECT - 1)
                    got_prefix = 1;
            end
            idx = idx + 1;
            byte_count = idx;
        end
    end

    // ------------------------------------------------------------------
    // Heartbeat, so a long run shows liveness in the log
    // ------------------------------------------------------------------
    initial begin
        forever begin
            #(50_000_000);   // every 50 ms of sim time
            $display("[%0t ns] ... still running, byte_count=%0d", $time, byte_count);
        end
    end

    // ------------------------------------------------------------------
    // Timeout / verdict: PASS if the banner prefix arrives intact before
    // the 450 ms timeout (matches the GHDL TB's --stop-time=460ms margin
    // around a ~315 ms banner arrival). FAIL on timeout or mismatch.
    // ------------------------------------------------------------------
    initial begin
        fork
            begin : wait_prefix
                wait (got_prefix);
            end
            begin : wait_timeout
                #(450_000_000);  // 450 ms in ns
            end
        join_any
        disable fork;

        if (got_prefix && prefix_ok) begin
            $display("=== NET CORE BOOT TEST PASSED ===");
            $finish;
        end else if (!got_prefix) begin
            $display("=== NET CORE BOOT TEST FAILED: timeout waiting for banner prefix (byte_count=%0d) ===", byte_count);
            $fatal(1);
        end else begin
            $display("=== NET CORE BOOT TEST FAILED: banner prefix mismatch ===");
            $fatal(1);
        end
    end

endmodule
