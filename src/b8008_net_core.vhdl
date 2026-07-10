--------------------------------------------------------------------------------
-- b8008_net_core.vhdl - Pure-logic monitor core for the LiteX SoC
--------------------------------------------------------------------------------
-- A hardware-proven b8008 monitor (projects/b8008_monitor/b8008_monitor_top)
-- ported to a pure-logic wrapper with NO board-specific edges:
--   - no PLL (clk arrives already at 25 MHz from the LiteX CRG)
--   - no I/O pads (console serial and the LA debug are internal wires)
--   - no debouncers / DIP switches (front-panel features become ports)
--   - no on-chip memory arrays (ROM and RAM live outside on external buses)
--
-- Everything that made the monitor boot to its "8008 Monitor" banner on
-- silicon is kept verbatim: the POR counter, the 2 ms auto-start press, the
-- bootstrap RST 0 jam FSM, and the debug clock controller. The DIP-switch
-- features (reset, hardware break, READY hold, interrupt trigger/vector) are
-- replaced by the ctl_* ports; the LED muxes and rolling-fetch capture are
-- deleted; the CPU debug pins come straight out on dbg_*.
--
-- CAUTION: debug_clock_control's bootstrap_done port is tied to constant '0'
-- here. A rising edge on it arms the post-bootstrap hardware break, which
-- FREEZES the CPU clock (debug_clock_control.vhdl:135-137). The monitor gated
-- it with (bootstrap_done and not sw(1)); wiring the real bootstrap_done here
-- would enable the break unconditionally and kill the headless boot the moment
-- bootstrap completes. Constant '0' = break disabled = CPU keeps running.
--
-- Copyright (c) 2025 Robert Rico
--------------------------------------------------------------------------------

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity b8008_net_core is
    port (
        clk  : in std_logic;   -- 25 MHz (LiteX "b8008" clock domain)
        rst  : in std_logic;   -- active-high, synchronous, from LiteX CRG
        -- console serial (internal wires to LiteX console bridge)
        uart_tx : out std_logic;
        uart_rx : in  std_logic;
        -- control pulses: single-cycle, clk domain, already synchronized
        ctl_run_stop   : in std_logic;
        ctl_step_cycle : in std_logic;
        ctl_step_sync  : in std_logic;
        ctl_int        : in std_logic;   -- one interrupt request
        ctl_int_vector : in std_logic_vector(2 downto 0);  -- stable level
        -- status (clk domain; LiteX side synchronizes)
        sts_is_running : out std_logic;
        sts_triggered  : out std_logic;
        sts_tx_busy    : out std_logic;
        -- external RAM bus (contract: see b8008_top ram_ext_* comment).
        -- Full 14-bit ABSOLUTE 8008 address, passed through unsliced: the
        -- b8008_top default map generics (RAM_BASE=0x1000, RAM_LAST=0x3FFF,
        -- RAM_ADDR_BITS=14) address RAM by latched_address[13:0] with no
        -- base subtraction. Slicing to 12:0 would alias 0x2000-0x3FFF onto
        -- 0x0000-0x1FFF and corrupt the live 0x1000-0x1FFF region.
        ram_addr  : out std_logic_vector(13 downto 0);
        ram_wdata : out std_logic_vector(7 downto 0);
        ram_rdata : in  std_logic_vector(7 downto 0);
        ram_rw_n  : out std_logic;
        ram_cs_n  : out std_logic;
        -- external ROM bus (contract: 1-cycle synchronous read, no gating)
        rom_addr : out std_logic_vector(11 downto 0);
        rom_data : in  std_logic_vector(7 downto 0);
        -- logic-analyzer debug (straight to pads at SoC level)
        dbg_d    : out std_logic_vector(7 downto 0);
        dbg_s0   : out std_logic; dbg_s1 : out std_logic; dbg_s2 : out std_logic;
        dbg_sync : out std_logic; dbg_phi1 : out std_logic; dbg_phi2 : out std_logic;
        dbg_int  : out std_logic
    );
end entity b8008_net_core;

architecture rtl of b8008_net_core is

    --------------------------------------------------------------------------------
    -- Component: b8008_top (CPU with external ROM and external RAM)
    --------------------------------------------------------------------------------
    component b8008_top is
        generic (
            CLK_FREQ_HZ   : integer := 100_000_000;
            RAM_INIT_FILE : string  := "";
            ROM_BASE      : integer := 16#0000#;
            ROM_LAST      : integer := 16#0FFF#;
            RAM_BASE      : integer := 16#1000#;
            RAM_LAST      : integer := 16#3FFF#;
            RAM_ADDR_BITS : integer := 14;
            EXTERNAL_RAM  : boolean := false
        );
        port (
            clk_in      : in std_logic;
            reset       : in std_logic;
            run_enable  : in std_logic;
            interrupt   : in std_logic;
            int_vector  : in std_logic_vector(2 downto 0);
            ready_in    : in std_logic := '1';
            phi1_out    : out std_logic;
            phi2_out    : out std_logic;
            sync_out    : out std_logic;
            s0_out      : out std_logic;
            s1_out      : out std_logic;
            s2_out      : out std_logic;
            address_out : out std_logic_vector(13 downto 0);
            data_out    : out std_logic_vector(7 downto 0);
            ram_byte_0  : out std_logic_vector(7 downto 0);
            debug_reg_a         : out std_logic_vector(7 downto 0);
            debug_reg_b         : out std_logic_vector(7 downto 0);
            debug_reg_c         : out std_logic_vector(7 downto 0);
            debug_reg_d         : out std_logic_vector(7 downto 0);
            debug_reg_e         : out std_logic_vector(7 downto 0);
            debug_reg_h         : out std_logic_vector(7 downto 0);
            debug_reg_l         : out std_logic_vector(7 downto 0);
            debug_cycle         : out std_logic_vector(1 downto 0);
            debug_pc            : out std_logic_vector(13 downto 0);
            debug_ir            : out std_logic_vector(7 downto 0);
            debug_needs_address : out std_logic;
            debug_int_pending   : out std_logic;
            debug_flag_carry    : out std_logic;
            debug_flag_zero     : out std_logic;
            debug_flag_sign     : out std_logic;
            debug_flag_parity   : out std_logic;
            debug_io_port_8     : out std_logic_vector(7 downto 0);
            debug_io_port_9     : out std_logic_vector(7 downto 0);
            debug_io_port_10    : out std_logic_vector(7 downto 0);
            debug_state_half    : out std_logic;
            io_port_in          : in  std_logic_vector(7 downto 0);
            io_port_in_select   : in  std_logic_vector(2 downto 0);
            io_port_in_enable   : in  std_logic;
            io_port_out         : out std_logic_vector(7 downto 0);
            io_port_num_out     : out std_logic_vector(4 downto 0);
            io_port_write       : out std_logic;
            io_port_read        : out std_logic;
            rom_a               : out std_logic_vector(13 downto 0);
            rom_d               : in  std_logic_vector(7 downto 0);
            rom_ce_n            : out std_logic;
            rom_oe_n            : out std_logic;
            ram_ext_addr  : out std_logic_vector(13 downto 0);
            ram_ext_wdata : out std_logic_vector(7 downto 0);
            ram_ext_rdata : in  std_logic_vector(7 downto 0) := x"00";
            ram_ext_rw_n  : out std_logic;
            ram_ext_cs_n  : out std_logic
        );
    end component;

    --------------------------------------------------------------------------------
    -- Component: B8008_USART (UART with 8008 handshaking)
    --------------------------------------------------------------------------------
    component b8008_usart is
        generic (
            CLK_FREQ_HZ  : integer := 100_000_000;
            BAUD_RATE    : integer := 115200;
            RX_PORT_NUM  : std_logic_vector(2 downto 0) := "001";
            TX_PORT_NUM  : std_logic_vector(4 downto 0) := "01001"
        );
        port (
            clk             : in  std_logic;
            rst             : in  std_logic;
            io_port_read    : in  std_logic;
            io_port_write   : in  std_logic;
            io_port_num     : in  std_logic_vector(4 downto 0);
            io_port_out     : in  std_logic_vector(7 downto 0);
            rx_port_data    : out std_logic_vector(7 downto 0);
            tx_busy         : out std_logic;
            uart_tx         : out std_logic;
            uart_rx         : in  std_logic
        );
    end component;

    --------------------------------------------------------------------------------
    -- Component: debug_clock_control (Three-button debug controller)
    --------------------------------------------------------------------------------
    component debug_clock_control is
        port (
            clk_in          : in  std_logic;
            reset           : in  std_logic;
            btn_run_stop    : in  std_logic;
            btn_step_cycle  : in  std_logic;
            btn_step_sync   : in  std_logic;
            phi1_in         : in  std_logic;
            phi2_in         : in  std_logic;
            sync_in         : in  std_logic;
            bootstrap_done  : in  std_logic;
            run_enable      : out std_logic;
            is_running      : out std_logic;
            next_is_phi1    : out std_logic;
            next_is_phi2    : out std_logic;
            triggered       : out std_logic;
            reset_request   : out std_logic
        );
    end component;

    --------------------------------------------------------------------------------
    -- Internal Signals
    --------------------------------------------------------------------------------
    -- POR (Power-On Reset) and reset control
    signal por_active   : std_logic := '1';
    signal reset_sw     : std_logic;
    signal reset_int    : std_logic;

    -- Interrupt request latch (replaces the DIP-switch int_button)
    signal t1i_ack_sig  : std_logic;
    signal int_req_latch : std_logic := '0';
    signal cpu_int_vec  : std_logic_vector(2 downto 0);

    -- CPU signals
    signal phi1         : std_logic;
    signal phi2         : std_logic;
    signal sync_sig     : std_logic;
    signal s0_sig       : std_logic;
    signal s1_sig       : std_logic;
    signal s2_sig       : std_logic;
    signal data_sig     : std_logic_vector(7 downto 0);

    -- Bootstrap interrupt control
    signal bootstrap_int     : std_logic := '0';
    signal bootstrap_done    : std_logic := '0';
    signal bootstrap_counter : unsigned(7 downto 0) := (others => '0');
    signal bs_phi2_prev      : std_logic := '0';

    -- Auto-start: synthetic run/stop press ~2 ms after POR/reset release
    signal auto_start_cnt   : unsigned(16 downto 0) := (others => '0');
    signal auto_start_done  : std_logic := '0';
    signal auto_start_pulse : std_logic := '0';

    -- ROM bus (b8008_top drives 14-bit addresses; sliced to 12 bits for the
    -- 4KB firmware). The RAM bus needs no intermediate: ram_ext_addr goes out
    -- unsliced (absolute 14-bit addressing, see entity comment).
    signal rom_a_int      : std_logic_vector(13 downto 0);
    signal rom_d_cpu      : std_logic_vector(7 downto 0);

    -- I/O port signals
    signal io_port_out  : std_logic_vector(7 downto 0);
    signal io_port_num  : std_logic_vector(4 downto 0);
    signal io_port_write : std_logic;
    signal io_port_read  : std_logic;
    signal io_port_in_data : std_logic_vector(7 downto 0);

    -- UART status
    signal uart_tx_busy   : std_logic;

    -- Clock counter for POR timing
    signal clk_counter : unsigned(25 downto 0) := (others => '0');

    -- Debug clock control signals
    signal dbg_run_enable   : std_logic;
    signal dbg_is_running   : std_logic;
    signal dbg_next_is_phi1 : std_logic;
    signal dbg_next_is_phi2 : std_logic;
    signal dbg_triggered    : std_logic;
    signal dbg_reset_request : std_logic;

begin

    --------------------------------------------------------------------------------
    -- Reset control
    --------------------------------------------------------------------------------
    -- The board reset switch is gone; the LiteX CRG owns reset via rst. Keep the
    -- reset_sw name as a constant so the auto-start / debug-reset plumbing below
    -- reads exactly like the proven monitor top.
    reset_sw <= '0';

    --------------------------------------------------------------------------------
    -- Power-On Reset (POR)
    --------------------------------------------------------------------------------
    -- POR holds reset until rst deasserts AND the counter has run out. rst
    -- replaces the monitor's pll_locked = '0' hold (clk is already locked at
    -- 25 MHz coming in from the CRG).
    process(clk)
    begin
        if rising_edge(clk) then
            if rst = '1' then
                clk_counter <= (others => '0');
                por_active  <= '1';
            else
                clk_counter <= clk_counter + 1;
                if clk_counter(19) = '1' then
                    por_active <= '0';
                end if;
            end if;
        end if;
    end process;

    -- Debug controller only sees POR and the (now constant) switch, not its
    -- own reset request. This prevents a reset feedback loop.
    reset_int <= por_active or reset_sw or dbg_reset_request;

    --------------------------------------------------------------------------------
    -- Auto-start
    --------------------------------------------------------------------------------
    -- Fires one synthetic run/stop press 2 ms after POR (or reset_sw) releases,
    -- so the CPU boots without a physical button press - this is what makes the
    -- headless boot work. Re-arms on reset_sw.
    --------------------------------------------------------------------------------
    process(clk)
    begin
        if rising_edge(clk) then
            auto_start_pulse <= '0';
            if por_active = '1' or reset_sw = '1' then
                auto_start_cnt  <= (others => '0');
                auto_start_done <= '0';
            elsif auto_start_done = '0' then
                auto_start_cnt <= auto_start_cnt + 1;
                if auto_start_cnt = 50000 then    -- 2 ms at 25 MHz
                    auto_start_pulse <= '1';
                    auto_start_done  <= '1';
                end if;
            end if;
        end if;
    end process;

    --------------------------------------------------------------------------------
    -- Debug Clock Control
    --------------------------------------------------------------------------------
    -- Gates the master clock to the CPU. The three physical buttons become ctl_*
    -- ports; run/stop is OR'd with the auto-start pulse to boot headless.
    -- bootstrap_done is FORCED to '0' - see the CAUTION at the top of the file.
    --------------------------------------------------------------------------------
    u_debug_clk : debug_clock_control
        port map (
            clk_in          => clk,
            reset           => por_active or reset_sw,  -- not dbg_reset_request (feedback loop)
            btn_run_stop    => ctl_run_stop or auto_start_pulse,
            btn_step_cycle  => ctl_step_cycle,
            btn_step_sync   => ctl_step_sync,
            phi1_in         => phi1,
            phi2_in         => phi2,
            sync_in         => sync_sig,
            bootstrap_done  => '0',                     -- hardware break disabled (headless)
            run_enable      => dbg_run_enable,
            is_running      => dbg_is_running,
            next_is_phi1    => dbg_next_is_phi1,
            next_is_phi2    => dbg_next_is_phi2,
            triggered       => dbg_triggered,
            reset_request   => dbg_reset_request
        );

    --------------------------------------------------------------------------------
    -- Bootstrap Interrupt Control
    --------------------------------------------------------------------------------
    -- Runs entirely in the clk domain, advancing on a phi2 rising-edge enable
    -- pulse. phi2, s0/s1/s2, sync, and reset_int are all clk-domain signals, so
    -- there is no clock-domain crossing anywhere in this FSM.
    --------------------------------------------------------------------------------
    process(clk)
    begin
        if rising_edge(clk) then
            bs_phi2_prev <= phi2;

            if reset_int = '1' then
                bootstrap_int     <= '0';
                bootstrap_done    <= '0';
                bootstrap_counter <= (others => '0');
            elsif phi2 = '1' and bs_phi2_prev = '0' then
                if bootstrap_done = '0' then
                    bootstrap_int <= '1';
                    bootstrap_counter <= bootstrap_counter + 1;
                    -- Wait for the counter to allow the CPU to reach T1I, then
                    -- check for the T1I state.
                    if bootstrap_counter >= 16 then
                        if s2_sig = '1' and s1_sig = '1' and s0_sig = '0' and sync_sig = '1' then
                            bootstrap_int  <= '0';
                            bootstrap_done <= '1';
                        end if;
                    end if;
                end if;
            end if;
        end if;
    end process;

    --------------------------------------------------------------------------------
    -- Interrupt request: ctl_int = one interrupt, ctl_int_vector = its vector
    --------------------------------------------------------------------------------
    -- Armed only after bootstrap so a request can never race the RST 0 jam.
    -- The latch clears on the CPU's T1I acknowledge (status 110, the same decode
    -- the bootstrap FSM uses).
    t1i_ack_sig <= '1' when (s2_sig = '1' and s1_sig = '1' and s0_sig = '0') else '0';

    int_latch : process(clk)
    begin
        if rising_edge(clk) then
            if reset_int = '1' or bootstrap_done = '0' then
                int_req_latch <= '0';
            elsif ctl_int = '1' then
                int_req_latch <= '1';
            elsif t1i_ack_sig = '1' then
                int_req_latch <= '0';
            end if;
        end if;
    end process;

    -- Latch the jam vector at REQUEST time. A combinational mux on bootstrap_done
    -- raced the bootstrap's own T1I; the latch only moves while a request is
    -- pending, so it is stable through every T1I.
    vec_latch : process(clk)
    begin
        if rising_edge(clk) then
            if reset_int = '1' or bootstrap_done = '0' then
                cpu_int_vec <= "000";              -- bootstrap jams RST 0
            elsif int_req_latch = '1' then
                cpu_int_vec <= ctl_int_vector;     -- freeze the request vector
            end if;
        end if;
    end process;

    --------------------------------------------------------------------------------
    -- b8008 CPU System Instance
    --------------------------------------------------------------------------------
    -- EXTERNAL_RAM => true: RAM is owned by the SoC and driven over ram_ext_*.
    -- The memory-map generics are left at b8008_top's defaults, exactly as the
    -- monitor top instantiated it (ROM 0x0000-0x0FFF, RAM 0x1000-0x3FFF).
    --------------------------------------------------------------------------------
    u_system : b8008_top
        generic map (
            CLK_FREQ_HZ  => 25_000_000,
            EXTERNAL_RAM => true
        )
        port map (
            clk_in      => clk,
            reset       => reset_int,
            run_enable  => dbg_run_enable,        -- Debug hold: '0' freezes phi state machine
            interrupt   => bootstrap_int or int_req_latch,
            int_vector  => cpu_int_vec,           -- RST 0 for bootstrap, ctl pick after
            ready_in    => '1',                   -- READY hold removed; always ready
            phi1_out    => phi1,
            phi2_out    => phi2,
            sync_out    => sync_sig,
            s0_out      => s0_sig,
            s1_out      => s1_sig,
            s2_out      => s2_sig,
            address_out => open,
            data_out    => data_sig,
            ram_byte_0  => open,
            debug_reg_a         => open,
            debug_reg_b         => open,
            debug_reg_c         => open,
            debug_reg_d         => open,
            debug_reg_e         => open,
            debug_reg_h         => open,
            debug_reg_l         => open,
            debug_cycle         => open,
            debug_pc            => open,
            debug_ir            => open,
            debug_needs_address => open,
            debug_int_pending   => open,
            debug_flag_carry    => open,
            debug_flag_zero     => open,
            debug_flag_sign     => open,
            debug_flag_parity   => open,
            debug_io_port_8     => open,
            debug_io_port_9     => open,
            debug_io_port_10    => open,
            debug_state_half    => open,
            -- External I/O port interface
            io_port_in          => io_port_in_data,
            io_port_in_select   => "001",         -- Port 1 uses external input
            io_port_in_enable   => '1',           -- ENABLED - use UART RX data
            io_port_out         => io_port_out,
            io_port_num_out     => io_port_num,
            io_port_write       => io_port_write,
            io_port_read        => io_port_read,
            -- External ROM interface (external path only)
            rom_a               => rom_a_int,
            rom_d               => rom_d_cpu,
            rom_ce_n            => open,
            rom_oe_n            => open,
            -- External RAM interface (owned by the SoC)
            ram_ext_addr  => ram_addr,
            ram_ext_wdata => ram_wdata,
            ram_ext_rdata => ram_rdata,
            ram_ext_rw_n  => ram_rw_n,
            ram_ext_cs_n  => ram_cs_n
        );

    -- ROM: external path only. The SoC owns the ROM; feed its data straight to
    -- the CPU and expose the low 12 address bits (4KB firmware).
    rom_d_cpu <= rom_data;
    rom_addr  <= rom_a_int(11 downto 0);

    --------------------------------------------------------------------------------
    -- B8008_USART Instance (115200 baud, with 8008 handshaking)
    --------------------------------------------------------------------------------
    u_uart : b8008_usart
        generic map (
            CLK_FREQ_HZ => 25_000_000,
            BAUD_RATE   => 115200,
            RX_PORT_NUM => "001",    -- INP 1 for UART RX
            TX_PORT_NUM => "01001"   -- OUT 9 for UART TX
        )
        port map (
            clk          => clk,
            rst          => reset_int,
            io_port_read => io_port_read,
            io_port_write => io_port_write,
            io_port_num  => io_port_num,
            io_port_out  => io_port_out,
            rx_port_data => io_port_in_data,  -- Directly wired to CPU input
            tx_busy      => uart_tx_busy,
            uart_tx      => uart_tx,
            uart_rx      => uart_rx
        );

    --------------------------------------------------------------------------------
    -- Status (clk domain; LiteX side synchronizes)
    --------------------------------------------------------------------------------
    sts_is_running <= dbg_is_running;
    sts_triggered  <= dbg_triggered;
    sts_tx_busy    <= uart_tx_busy;

    --------------------------------------------------------------------------------
    -- CPU Debug Outputs (straight to pads at SoC level, for a logic analyzer)
    --------------------------------------------------------------------------------
    dbg_d    <= data_sig;
    dbg_s0   <= s0_sig;
    dbg_s1   <= s1_sig;
    dbg_s2   <= s2_sig;
    dbg_sync <= sync_sig;
    dbg_phi1 <= phi1;
    dbg_phi2 <= phi2;
    dbg_int  <= bootstrap_int;

end architecture rtl;
