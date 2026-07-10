--------------------------------------------------------------------------------
-- b8008_net_core_tb.vhdl - Headless boot testbench for b8008_net_core
--------------------------------------------------------------------------------
-- Proves the pure-logic monitor core boots the firmware with NO physical
-- button press (auto-start) and emits its UART banner. The core has no PLL,
-- no pads, no debouncers and no on-chip memories: the ROM (rom_4kx8_bram, the
-- baked b8008_monitor firmware) and the data RAM (ram_sync) are instantiated
-- HERE, outside the core, wired to the core's external buses per the
-- b8008_top ram_ext_* / rom contract.
--
-- Boot sequence (all at 25 MHz, ctl_* inputs held low):
--   POR (~21 ms) -> auto_start_pulse (2 ms later) -> debug reset ->
--   bootstrap RST 0 jam -> T1I detect -> bootstrap_done -> CPU fetches from
--   ROM -> firmware delay_short (~380 ms) -> send_banner -> OUT 9 UART bytes.
--
-- PASS criteria (checked at end of stimulus):
--   1. dbg_int rose (bootstrap jam started) and fell again (T1I detected)
--   2. UART decoder received the first bytes of the "8008 Monitor" banner
--      taken from b8008_monitor.asm send_banner: '8','0','0','8',' '.
--
-- Run:  make sim-core   (expect minutes of wall-clock: 460 ms of sim time)
--
-- The UART-decode procedure is copied from the monitor project's
-- monitor_boot_tb.vhdl (which only checks the first byte); here we assert on
-- the real banner string, not a permissive set.
--------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

entity b8008_net_core_tb is
end entity b8008_net_core_tb;

architecture behavior of b8008_net_core_tb is

    constant CLK_PERIOD : time := 40 ns;      -- 25 MHz core clock
    constant BIT_TIME   : time := 8.681 us;   -- 115200 baud

    -- Expected banner prefix: "8008 " from send_banner in b8008_monitor.asm
    --   '8'=0x38  '0'=0x30  '0'=0x30  '8'=0x38  ' '=0x20
    type byte_vec_t is array (natural range <>) of std_logic_vector(7 downto 0);
    constant BANNER_PREFIX : byte_vec_t := (x"38", x"30", x"30", x"38", x"20");

    signal clk : std_logic := '0';
    signal rst : std_logic := '1';

    -- Console serial
    signal uart_tx : std_logic;
    signal uart_rx : std_logic := '1';

    -- Control pulses (held low: auto-start must boot it headless)
    signal ctl_run_stop   : std_logic := '0';
    signal ctl_step_cycle : std_logic := '0';
    signal ctl_step_sync  : std_logic := '0';
    signal ctl_int        : std_logic := '0';
    signal ctl_int_vector : std_logic_vector(2 downto 0) := "000";

    -- Status
    signal sts_is_running : std_logic;
    signal sts_triggered  : std_logic;
    signal sts_tx_busy    : std_logic;

    -- External RAM bus (absolute 14-bit 8008 address) - wired to ram_sync
    signal ram_addr  : std_logic_vector(13 downto 0);
    signal ram_wdata : std_logic_vector(7 downto 0);
    signal ram_rdata : std_logic_vector(7 downto 0);
    signal ram_rw_n  : std_logic;
    signal ram_cs_n  : std_logic;

    -- External ROM bus (4KB, 12 address bits) - wired to rom_4kx8_bram
    signal rom_addr : std_logic_vector(11 downto 0);
    signal rom_data : std_logic_vector(7 downto 0);

    -- Debug (logic-analyzer) outputs
    signal dbg_d    : std_logic_vector(7 downto 0);
    signal dbg_s0, dbg_s1, dbg_s2 : std_logic;
    signal dbg_sync, dbg_phi1, dbg_phi2 : std_logic;
    signal dbg_int  : std_logic;

    -- Result tracking
    signal saw_int_rise : boolean := false;
    signal saw_int_fall : boolean := false;
    signal byte_count   : natural := 0;
    signal prefix_ok    : boolean := true;
    signal got_prefix   : boolean := false;
    signal sim_done     : boolean := false;

begin

    ----------------------------------------------------------------------------
    -- Device under test: the pure-logic monitor core
    ----------------------------------------------------------------------------
    dut : entity work.b8008_net_core
        port map (
            clk            => clk,
            rst            => rst,
            uart_tx        => uart_tx,
            uart_rx        => uart_rx,
            ctl_run_stop   => ctl_run_stop,
            ctl_step_cycle => ctl_step_cycle,
            ctl_step_sync  => ctl_step_sync,
            ctl_int        => ctl_int,
            ctl_int_vector => ctl_int_vector,
            sts_is_running => sts_is_running,
            sts_triggered  => sts_triggered,
            sts_tx_busy    => sts_tx_busy,
            ram_addr       => ram_addr,
            ram_wdata      => ram_wdata,
            ram_rdata      => ram_rdata,
            ram_rw_n       => ram_rw_n,
            ram_cs_n       => ram_cs_n,
            rom_addr       => rom_addr,
            rom_data       => rom_data,
            dbg_d          => dbg_d,
            dbg_s0         => dbg_s0,
            dbg_s1         => dbg_s1,
            dbg_s2         => dbg_s2,
            dbg_sync       => dbg_sync,
            dbg_phi1       => dbg_phi1,
            dbg_phi2       => dbg_phi2,
            dbg_int        => dbg_int
        );

    ----------------------------------------------------------------------------
    -- External data RAM: ram_sync is the behavioral model, contract-conformant
    -- by construction (1-cycle synchronous read, no CS gating on reads).
    -- ADDR_BITS => 14: RAM is addressed by the ABSOLUTE 14-bit 8008 address
    -- (b8008_top default map generics: RAM_ADDR_BITS=14, no base subtraction),
    -- matching what the internal ram_sync instance is at defaults.
    ----------------------------------------------------------------------------
    u_ram : entity work.ram_sync
        generic map (
            ADDR_BITS => 14,
            INIT_FILE => ""
        )
        port map (
            CLK      => clk,
            ADDR     => ram_addr,
            DATA_IN  => ram_wdata,
            DATA_OUT => ram_rdata,
            RW_N     => ram_rw_n,
            CS_N     => ram_cs_n
        );

    ----------------------------------------------------------------------------
    -- External program ROM: rom_4kx8_bram with the b8008_monitor firmware
    -- baked in (constant array in its source). CS_N tied low, exactly as the
    -- monitor top's gen_internal_rom instantiates it.
    ----------------------------------------------------------------------------
    u_rom : entity work.rom_4kx8_bram
        port map (
            CLK      => clk,
            ADDR     => rom_addr,
            DATA_OUT => rom_data,
            CS_N     => '0'
        );

    ----------------------------------------------------------------------------
    -- Clock and reset
    ----------------------------------------------------------------------------
    clk <= not clk after CLK_PERIOD / 2 when not sim_done else '0';

    reset_gen : process
    begin
        rst <= '1';
        wait for 1 us;   -- a handful of clocks of synchronous reset
        rst <= '0';
        wait;
    end process;

    ----------------------------------------------------------------------------
    -- Track bootstrap interrupt activity on dbg_int
    ----------------------------------------------------------------------------
    int_watch : process(dbg_int)
    begin
        if rising_edge(dbg_int) then
            saw_int_rise <= true;
            report "dbg_int RISE at " & time'image(now);
        elsif falling_edge(dbg_int) then
            saw_int_fall <= true;
            report "dbg_int FALL at " & time'image(now) & " (bootstrap complete)";
        end if;
    end process;

    ----------------------------------------------------------------------------
    -- UART RX decoder on uart_tx (copied from monitor_boot_tb). Latches each
    -- received byte, checks it against the expected banner prefix in order.
    ----------------------------------------------------------------------------
    uart_decode : process
        variable byte : std_logic_vector(7 downto 0);
        variable idx  : natural := 0;
    begin
        wait until falling_edge(uart_tx);   -- start bit
        wait for BIT_TIME * 1.5;            -- middle of bit 0
        for i in 0 to 7 loop
            byte(i) := uart_tx;
            wait for BIT_TIME;
        end loop;
        report "UART byte " & integer'image(idx) & ": 0x" & to_hstring(byte) &
               " at " & time'image(now);
        if idx < BANNER_PREFIX'length then
            if byte /= BANNER_PREFIX(idx) then
                prefix_ok <= false;
                report "UART byte " & integer'image(idx) & " = 0x" &
                       to_hstring(byte) & " expected 0x" &
                       to_hstring(BANNER_PREFIX(idx)) severity warning;
            end if;
            if idx = BANNER_PREFIX'length - 1 then
                got_prefix <= true;
            end if;
        end if;
        idx := idx + 1;
        byte_count <= idx;
    end process;

    ----------------------------------------------------------------------------
    -- Stimulus and verdict
    ----------------------------------------------------------------------------
    stimulus : process
    begin
        -- POR (~21 ms) + auto-start (2 ms) + firmware delay_short (~380 ms)
        -- before the banner. Wait for the whole prefix with a generous timeout.
        wait until got_prefix for 450 ms;
        wait for 5 ms;                      -- let the last byte handler finish

        assert saw_int_rise
            report "FAIL: bootstrap interrupt never asserted" severity error;
        assert saw_int_fall
            report "FAIL: bootstrap never completed (T1I not detected) - CPU stuck in jam loop"
            severity error;
        assert byte_count > 0
            report "FAIL: no UART output - headless auto-start did not boot the CPU"
            severity error;
        assert got_prefix
            report "FAIL: banner prefix never fully received (timeout)" severity error;
        assert prefix_ok
            report "FAIL: UART banner bytes did not match '8008 ' from send_banner"
            severity error;

        if saw_int_rise and saw_int_fall and got_prefix and prefix_ok then
            report "=== NET CORE BOOT TEST PASSED ===" severity note;
        else
            report "=== NET CORE BOOT TEST FAILED ===" severity error;
        end if;

        sim_done <= true;
        wait;
    end process;

end architecture behavior;
