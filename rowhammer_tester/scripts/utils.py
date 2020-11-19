import os
import csv
import sys
import glob
import json
import time
from operator import or_
from functools import reduce

from migen import log2_int

# ###########################################################################

def discover_generated_files_dir():
    # Search for defs.csv file that should have been generated in build directory.
    # Assume that we are building in repo root.
    script_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
    build_dir = os.path.normpath(os.path.join(script_dir, '..', '..', 'build'))
    candidates = os.path.join(build_dir, '*', 'defs.csv')
    results = glob.glob(candidates)
    if not results:
        raise ImportError(
            'Could not find "defs.csv". Make sure to run target generator (from'
            ' rowhammer_tester/targets/) in the root directory of this repository.')
    elif len(results) > 1:
        if 'TARGET' not in os.environ:
            raise ImportError(
                'More than one "defs.csv" file found. Set environmental variable'
                ' TARGET to the name of the target to use (e.g. `export TARGET=arty`).')
        gen_dir = os.path.join(build_dir, os.environ['TARGET'])
    else:
        gen_dir = os.path.dirname(results[0])

    sys.path.append(gen_dir)
    return gen_dir

GENERATED_DIR = discover_generated_files_dir()
print('Using generated target files in: {}'.format(os.path.relpath(GENERATED_DIR)))

# Import sdram_init.py
sys.path.append(GENERATED_DIR)
try:
    import sdram_init as sdram_init_defs
    from sdram_init import *
except ModuleNotFoundError:
    print('WARNING: sdram_init not loaded')

def get_generated_file(name):
    # For getting csr.csv/analyzer.csv
    filename = os.path.join(GENERATED_DIR, name)
    if not os.path.isfile(filename):
        raise ImportError('Generated file "{}" not found in directory "{}"'.format(name, GENERATED_DIR))
    return filename

def get_generated_defs():
    with open(get_generated_file('defs.csv'), newline='') as f:
        reader = csv.reader(f)
        return {name: value for name, value in reader}

def get_litedram_settings():
    with open(get_generated_file('litedram_settings.json')) as f:
        return json.load(f)

def RemoteClient(*args, **kwargs):
    from litex import RemoteClient as _RemoteClient
    return _RemoteClient(csr_csv=get_generated_file('csr.csv'), *args, **kwargs)

def litex_server():
    from litex.tools.litex_server import RemoteServer
    from litex.tools.remote.comm_udp import CommUDP
    defs = get_generated_defs()
    comm = CommUDP(server=defs['IP_ADDRESS'], port=int(defs['UDP_PORT']))
    server = RemoteServer(comm, '127.0.0.1', 1234)
    server.open()
    server.start(4)

# ###########################################################################

def sdram_software_control(wb):
    wb.regs.sdram_dfii_control.write(dfii_control_cke|dfii_control_odt|dfii_control_reset_n)
    if hasattr(wb.regs, 'ddrphy_en_vtc'):
        wb.regs.ddrphy_en_vtc.write(0)

def sdram_hardware_control(wb):
    wb.regs.sdram_dfii_control.write(dfii_control_sel)
    if hasattr(wb.regs, 'ddrphy_en_vtc'):
        wb.regs.ddrphy_en_vtc.write(1)

def sdram_init(wb):
    sdram_software_control(wb)

    # we cannot check for the string "DFII_CONTROL" as done when generating C code,
    # so this is hardcoded for now
    # update: Hacky but works
    control_cmds = []
    with open(sdram_init_defs.__file__, 'r') as f:
        n = 0
        while True:
            line = f.readline()
            if not line: break
            line = line.strip().replace(' ', '')
            if len(line) and line[0] == '(':
                if line.find('_control_') > 0:
                    control_cmds.append(n)
                n = n + 1

    for i, (comment, a, ba, cmd, delay) in enumerate(init_sequence):
        wb.regs.sdram_dfii_pi0_address.write(a)
        wb.regs.sdram_dfii_pi0_baddress.write(ba)
        if i in control_cmds:
            print('(ctl) ' + comment)
            wb.regs.sdram_dfii_control.write(cmd)
        else:
            print('(cmd) ' + comment)
            wb.regs.sdram_dfii_pi0_command.write(cmd)
            wb.regs.sdram_dfii_pi0_command_issue.write(1)
        time.sleep(0.01 + delay * 1e-5)

    sdram_hardware_control(wb)

# ###########################################################################

def compare(val, ref, fmt, nbytes=4):
    assert fmt in ["bin", "hex"]
    if fmt == "hex":
        print("0x{:0{n}x} {cmp} 0x{:0{n}x}".format(
            val, ref, n=nbytes*2, cmp="==" if val == ref else "!="))
    if fmt == "bin":
        print("{:0{n}b} xor {:0{n}b} = {:0{n}b}".format(
            val, ref, val ^ ref, n=nbytes*8))

def memwrite(wb, data, base=0x40000000, burst=0xff):
    for i in range(0, len(data), burst):
        wb.write(base + 4*i, data[i:i+burst])

def memread(wb, n, base=0x40000000, burst=0xff):
    data = []
    for i in range(0, n, burst):
        data += wb.read(base + 4*i, min(burst, n - i))
    return data

def memfill(wb, n, pattern=0xaaaaaaaa, **kwargs):
    memwrite(wb, [pattern] * n, **kwargs)

def memcheck(wb, n, pattern=0xaaaaaaaa, **kwargs):
    data = memread(wb, n, **kwargs)
    errors = [(i, w) for i, w in enumerate(data) if w != pattern]
    return errors

def memspeed(wb, n, **kwargs):
    def measure(fun, name):
        start = time.time()
        ret = fun(wb, n, **kwargs)
        elapsed = time.time() - start
        print('{:5} speed: {:6.2f} KB/s ({:.1f} sec)'.format(name, (n*4)/elapsed / 1e3, elapsed))
        return ret

    measure(memfill, 'Write')
    data = measure(memread, 'Read')
    errors = [(i, w) for i, w in enumerate(data) if w != kwargs.get('pattern', 0xaaaaaaaa)]
    assert len(errors) == 0, len(errors)

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def word2byte(words, word_size=4):
    for w in words:
        for i in range(word_size):
            yield (w & (0xff << 8*i)) >> 8*i

def memdump(data, base=0x40000000, chunk_len=16):
    def tochar(val):
        return chr(val) if 0x20 <= val <= 0x7e else '.'

    data_bytes = list(word2byte(data))
    for i, chunk in enumerate(chunks(data_bytes, chunk_len)):
        b = " ".join("{:02x}".format(chunk[i] if i < len(chunk) else 0) for i in range(chunk_len))
        c = "".join(tochar(chunk[i] if i < len(chunk) else 0) for i in range(chunk_len))
        print("0x{addr:08x}:  {bytes}  {chars}".format(addr=base + chunk_len*i, bytes=b, chars=c))

################################################################################

class DRAMAddressConverter:
    def __init__(self, *, colbits, rowbits, bankbits, address_align, address_mapping='ROW_BANK_COL'):
        # FIXME: generate these from BaseSoC
        # soc.sdram.controller.settings
        self.colbits = colbits
        self.rowbits = rowbits
        self.bankbits = bankbits
        self.address_align = address_align
        self.address_mapping = address_mapping
        assert self.address_mapping == 'ROW_BANK_COL'

    @classmethod
    def load(cls):
        settings = get_litedram_settings()
        if settings.phy.memtype == "SDR":
            burst_length = settings.phy.nphases
        else:
            from litedram.common import burst_lengths
            burst_length = burst_lengths[settings.phy.memtype]
        address_align = log2_int(burst_length)
        return cls(
            colbits         = settings.geom.colbits,
            rowbits         = settings.geom.rowbits,
            bankbits        = settings.geom.bankbits,
            address_align   = address_align,
            address_mapping = settings.address_mapping,
        )

    def _encode(self, bank, row, col):
        assert bank < 2**self.bankbits
        assert col < 2**self.colbits
        assert row < 2**self.rowbits

        def masked(value, width, offset):
            masked = value & (2**width - 1)
            assert masked == value, "Value larger than value bit-width"
            return masked << offset

        return reduce(or_, [
            masked(row,  self.rowbits,  self.bankbits + self.colbits),
            masked(bank, self.bankbits, self.colbits),
            masked(col,  self.colbits,  0),
        ])

    def encode_bus(self, *, bank, row, col, base=0x40000000, bus_align=2):
        assert bus_align <= self.address_align
        address = self._encode(bank, row, col)
        return base + (address << (self.address_align - bus_align))

    def encode_dma(self, *, bank, row, col):
        address = self._encode(bank, row, col)
        return address >> self.address_align

    def _decode(self, address):
        def extract(value, width, offset):
            mask = 2**width - 1
            return (value & (mask << offset)) >> offset

        row = extract(address, self.rowbits, self.bankbits + self.colbits)
        bank = extract(address, self.bankbits, self.colbits)
        col = extract(address, self.colbits, 0)

        return bank, row, col

    def decode_bus(self, address, base=0x40000000, bus_align=2):
        address -= base
        address >>= self.address_align - bus_align
        return self._decode(address)

    def decode_dma(self, address):
        return self._decode(address << self.address_align)

# ######################### HW (accel) memory utils #############################

#
# wb - remote handle
# offset - memory offset in bytes (modulo 16)
# size - memory size in bytes (modulo 16)
# patterns - pattern to fill memory
#
def hw_memset(wb, offset, size, patterns, dbg=False):
    assert size % 16 == 0
    assert len(patterns) == 1 # FIXME: Support more patterns

    pattern = patterns[0] & 0xffffffff

    if dbg:
        print('hw_memset: offset: 0x{:08x}, size: 0x{:08x}, pattern: 0x{:08x}'.format(offset, size, pattern))

    # Reset module
    wb.regs.writer_start.write(0)
    wb.regs.writer_reset.write(1)
    wb.regs.writer_reset.write(0)

    assert wb.regs.writer_done.read() == 0

    # TODO: Deprecated, remove
    wb.regs.writer_mem_base.write(0x00000000)

    # Unmask whole address space. TODO: Unmask only part of it?
    wb.regs.writer_mem_mask.write(0xffffffff)

    # FIXME: Support more patterns
    wb.write(wb.mems.pattern_w0.base, pattern)
    wb.write(wb.mems.pattern_w1.base, pattern)
    wb.write(wb.mems.pattern_w2.base, pattern)
    wb.write(wb.mems.pattern_w3.base, pattern)
    wb.write(wb.mems.pattern_adr.base, offset // 16)
    # Unmask just one pattern/offset
    wb.regs.writer_data_mask.write(0x00000000)

    # 4 (words) x 4 (bytes)
    wb.regs.writer_count.write(size // 16)

    # Start module
    wb.regs.writer_start.write(1)
    wb.regs.writer_start.write(0)

    # FIXME: Support progress
    while True:
        if wb.regs.writer_done.read():
            break
        else:
            time.sleep(10 / 1e3) # 10 ms


def hw_memtest(wb, offset, size, patterns, dbg=False):
    assert size % 16 == 0
    assert len(patterns) == 1 # FIXME: Support more patterns

    pattern = patterns[0] & 0xffffffff

    if dbg:
        print('hw_memtest: offset: 0x{:08x}, size: 0x{:08x}, pattern: 0x{:08x}'.format(offset, size, pattern))

    wb.regs.reader_start.write(0)
    wb.regs.reader_reset.write(1)
    wb.regs.reader_reset.write(0)

    assert wb.regs.reader_ready.read() == 0

    # Flush error fifo
    while wb.regs.reader_err_rdy.read():
        wb.regs.reader_err_rd.read()

    assert wb.regs.reader_err_rdy.read() == 0

    # Enable error FIFO
    wb.regs.reader_skipfifo.write(0)

    # Unmask whole address space. TODO: Unmask only part of it?
    wb.regs.reader_mem_mask.write(0xffffffff)

    wb.write(wb.mems.pattern_rd_w0.base, pattern)
    wb.write(wb.mems.pattern_rd_w1.base, pattern)
    wb.write(wb.mems.pattern_rd_w2.base, pattern)
    wb.write(wb.mems.pattern_rd_w3.base, pattern)
    wb.write(wb.mems.pattern_rd_adr.base, offset // 16)
    # Unmask just one pattern/offset
    wb.regs.reader_gen_mask.write(0x00000000)

    # 4 (words) x 4 (bytes)
    wb.regs.reader_count.write(size // 16)

    wb.regs.reader_start.write(1)
    wb.regs.reader_start.write(0)

    errors = []

    # Read unmatched offset
    def append_errors(wb, err):
        while wb.regs.reader_err_rdy.read():
            off = wb.regs.reader_err_rd.read()
            err.append(off)

    # FIXME: Support progress
    while True:
        if wb.regs.reader_ready.read():
            break
        else:
            append_errors(wb, errors)
            time.sleep(10 / 1e3) # !0 ms

    # Make sure we read all errors
    append_errors(wb, errors)

    assert wb.regs.reader_ready.read() == 1
    assert wb.regs.reader_err_rdy.read() == 0

    if dbg:
        print('hw_memtest: errors: {:d}'.format(len(errors)))

    return errors


# ###############################################################################

# Open a remote connection in an interactive session (e.g. when sourced as `ipython -i <thisfile>`)
if __name__ == "__main__":
    if bool(getattr(sys, 'ps1', sys.flags.interactive)):
        wb = RemoteClient()
        wb.open()