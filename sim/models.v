// models.v - Verilog memory models for the b8008_net_core gate-level sim
// ----------------------------------------------------------------------------
// Stand-ins for the external ROM/RAM buses exposed by b8008_net_core (see
// projects/b8008_net/src/b8008_net_core.vhdl). Contract mirrors the VHDL
// simulation models used by sim/b8008_net_core_tb.vhdl:
//   - net_ram (== ram_sync.vhdl behavior): 16384 x 8, synchronous read EVERY
//     posedge (no CS gating on reads - the CPU's data-bus mux only samples
//     RAM data when it addressed RAM), write when !cs_n && !rw_n.
//   - net_rom (== rom_4kx8_bram.vhdl behavior): 4096 x 8, synchronous read
//     every posedge, contents loaded with $readmemh from a baked .mem file
//     (one hex byte per line, no address directives - same format as the
//     project's .mem files).
// ----------------------------------------------------------------------------

module net_ram (
    input  wire        clk,
    input  wire [13:0] addr,
    input  wire [7:0]  wdata,
    output reg  [7:0]  rdata,
    input  wire        rw_n,   // 0 = write, 1 = read
    input  wire        cs_n    // 0 = selected
);
    reg [7:0] mem [0:16383];

    always @(posedge clk) begin
        if (!cs_n && !rw_n)
            mem[addr] <= wdata;
        rdata <= mem[addr];
    end
endmodule

module net_rom #(
    parameter ROM_FILE = ""
) (
    input  wire        clk,
    input  wire [11:0] addr,
    output reg  [7:0]  rdata
);
    reg [7:0] mem [0:4095];

    initial begin
        if (ROM_FILE != "")
            $readmemh(ROM_FILE, mem);
    end

    always @(posedge clk) begin
        rdata <= mem[addr];
    end
endmodule
