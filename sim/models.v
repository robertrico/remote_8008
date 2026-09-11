// models.v - Verilog memory models for the b8008_net_core gate-level sim
// ----------------------------------------------------------------------------
// Stand-in for the external ROM bus exposed by b8008_net_core (see
// src/b8008_net_core.vhdl in this repo). RAM lives inside b8008_top and
// needs no model. Contract mirrors the VHDL simulation model used by
// sim/b8008_net_core_tb.vhdl:
//   - net_rom (== rom_4kx8_bram.vhdl behavior): 4096 x 8, synchronous read
//     every posedge, contents loaded with $readmemh from a baked .mem file
//     (one hex byte per line, no address directives - same format as the
//     project's .mem files).
// ----------------------------------------------------------------------------

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
