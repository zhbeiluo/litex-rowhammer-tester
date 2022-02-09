#!/usr/bin/env python3
#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2021 Antmicro <www.antmicro.com>
# SPDX-License-Identifier: BSD-2-Clause

import os
import argparse
import math
import json

from migen import *

from litex_boards.platforms import datacenter_ddr4_test_board
from litex.build.xilinx.vivado import vivado_build_args, vivado_build_argdict

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser

from litedram.modules import MTA18ASF2G72PZ
from litedram.phy.s7ddrphy import A7DDRPHY
from litedram.init import get_sdram_phy_py_header
from litedram.core.controller import ControllerSettings
from litedram.common import PhySettings, GeomSettings, TimingSettings

from liteeth.phy import LiteEthS7PHYRGMII

from rowhammer_tester.targets import common

# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq, iodelay_clk_freq=200e6):
        self.clock_domains.cd_sys    = ClockDomain()
        self.clock_domains.cd_sys2x  = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x  = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x_dqs = ClockDomain(reset_less=True)
        self.clock_domains.cd_idelay = ClockDomain()
        self.clock_domains.cd_eth = ClockDomain()

        # # #

        self.submodules.pll = pll = S7PLL(speedgrade=-1)
        pll.register_clkin(platform.request("clk100"), 100e6)
        pll.create_clkout(self.cd_sys,    sys_clk_freq)
        pll.create_clkout(self.cd_sys2x,  2 * sys_clk_freq)
        pll.create_clkout(self.cd_sys4x,  4 * sys_clk_freq)
        pll.create_clkout(self.cd_sys4x_dqs, 4*sys_clk_freq, phase=90)
        pll.create_clkout(self.cd_idelay, iodelay_clk_freq)

        # Etherbone --------------------------------------------------------------------------------
        pll.create_clkout(self.cd_eth, 25e6)
        self.comb += platform.request("eth_ref_clk").eq(self.cd_eth.clk)

        self.submodules.idelayctrl = S7IDELAYCTRL(self.cd_idelay)

# BaseSoC ------------------------------------------------------------------------------------------

class SoC(common.RowHammerSoC):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # # Analyzer ---------------------------------------------------------------------------------
        # analyzer_signals = [
        #     self.sdram.dfii.ext_dfi_sel,
        #     *[p.rddata for p in self.ddrphy.dfi.phases],
        #     *[p.rddata_valid for p in self.ddrphy.dfi.phases],
        #     *[p.rddata_en for p in self.ddrphy.dfi.phases],
        # ]
        # from litescope import LiteScopeAnalyzer
        # self.submodules.analyzer = LiteScopeAnalyzer(analyzer_signals,
        #    depth        = 512,
        #    clock_domain = "sys",
        #    csr_csv      = "analyzer.csv")
        # self.add_csr("analyzer")

    def get_platform(self):
        return datacenter_ddr4_test_board.Platform()

    def get_crg(self):
        return _CRG(self.platform, self.sys_clk_freq)

    def get_ddrphy(self):
        return A7DDRPHY(self.platform.request("ddr4"),
            memtype         = "DDR4",
            iodelay_clk_freq = 200e6,
            sys_clk_freq     = self.sys_clk_freq,
            is_rdimm         = True,
        )
        self.add_sdram("sdram",
            phy                     = self.ddrphy,
            module                  = MTA18ASF2G72PZ(clk_freq=sys_clk_freq, rate="1:4"),
            l2_cache_size           = kwargs.get("l2_size", 8192),
            l2_cache_min_data_width = 256,
            size                    = 0x40000000,
        )

    def get_sdram_module(self):
        return MTA18ASF2G72PZ(clk_freq=self.sys_clk_freq, rate="1:4")

    def add_host_bridge(self):
        # Traces between PHY and FPGA introduce ignorable delays of ~0.165ns +/- 0.015ns.
        # PHY chip does not introduce delays on TX (FPGA->PHY), however it includes 1.2ns
        # delay for RX CLK so we only need 0.8ns to match the desired 2ns.
        self.submodules.ethphy = LiteEthS7PHYRGMII(
            clock_pads = self.platform.request("eth_clocks"),
            pads       = self.platform.request("eth"),
            rx_delay   = 0.8e-9,
            hw_reset_cycles = math.ceil(float(10e-3) * self.sys_clk_freq)
        )
        self.add_csr("ethphy")
        self.add_etherbone(
            phy         = self.ethphy,
            ip_address  = self.ip_address,
            mac_address = self.mac_address,
            udp_port    = self.udp_port
        )

    def generate_sdram_phy_py_header(self, output_file):
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        f = open(output_file, "w")
        f.write(get_sdram_phy_py_header(
            self.sdram.controller.settings.phy,
            self.sdram.controller.settings.timing))
        f.close()


# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on LPDDR4 Test Board")

    common.parser_args(parser, sys_clk_freq='50e6')
    vivado_build_args(parser)
    args = parser.parse_args()

    soc_kwargs = common.get_soc_kwargs(args)
    soc = SoC(**soc_kwargs)

    target_name = 'datacenter_ddr4_test_board'
    builder_kwargs = common.get_builder_kwargs(args, target_name=target_name)
    builder = Builder(soc, **builder_kwargs)
    build_kwargs = vivado_build_argdict(args) if not args.sim else {}

    common.run(args, builder, build_kwargs, target_name=target_name)

if __name__ == "__main__":
    main()
