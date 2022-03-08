#!/usr/bin/env python3

import time
import random
import argparse

from rowhammer_tester.scripts.utils import (
    memfill, memcheck, memwrite, DRAMAddressConverter, litex_server, RemoteClient,
    get_litedram_settings, get_generated_defs, execute_payload)
from rowhammer_tester.scripts.playbook.lib import (generate_payload_from_row_list)

################################################################################


class RowHammer:

    def __init__(
            self,
            wb,
            *,
            settings,
            nrows,
            column,
            bank,
            rows_start=0,
            no_refresh=False,
            verbose=False,
            plot=False,
            payload_executor=False,
            data_inversion=False):
        for name, val in locals().items():
            setattr(self, name, val)
        self.converter = DRAMAddressConverter.load()
        self._addresses_per_row = {}

    @property
    def rows(self):
        return list(range(self.rows_start, self.nrows))

    def addresses_per_row(self, row):
        # Calculate the addresses lazily and cache them
        if row not in self._addresses_per_row:
            addresses = [
                self.converter.encode_bus(bank=self.bank, col=col, row=row)
                for col in range(2**self.settings.geom.colbits)
            ]
            self._addresses_per_row[row] = addresses
        return self._addresses_per_row[row]

    def attack(self, row_tuple, read_count, progress_header=''):
        # Make sure that the Rowhammer module is in reset state
        self.wb.regs.rowhammer_enabled.write(0)
        self.wb.regs.rowhammer_count.read()  # clears the value

        # Configure the Rowhammer attacker
        assert len(
            row_tuple
        ) == 2, 'Use BIST modules/Payload Executor to hammer different number of rows than 2'
        addresses = [
            self.converter.encode_dma(bank=self.bank, col=self.column, row=r) for r in row_tuple
        ]
        self.wb.regs.rowhammer_address1.write(addresses[0])
        self.wb.regs.rowhammer_address2.write(addresses[1])
        self.wb.regs.rowhammer_enabled.write(1)

        row_strw = len(str(2**self.settings.geom.rowbits - 1))

        def progress(count):
            s = '  {}'.format(progress_header + ' ' if progress_header else '')
            s += 'Rows = {}, Count = {:5.2f}M / {:5.2f}M'.format(
                row_tuple, count / 1e6, read_count / 1e6, n=row_strw)
            print(s, end='  \r')

        while True:
            count = self.wb.regs.rowhammer_count.read()
            progress(count)
            if count >= read_count:
                break

        self.wb.regs.rowhammer_enabled.write(0)
        progress(self.wb.regs.rowhammer_count.read())  # also clears the value
        print()

    def row_access_iterator(self, burst=16):
        for row in self.rows:
            addresses = self.addresses_per_row(row)
            n = (max(addresses) - min(addresses)) // 4
            base_addr = addresses[0]
            yield row, n, base_addr

    def check_errors(self, row_patterns, row_progress=16):
        row_errors = {}
        for row, n, base in self.row_access_iterator():
            errors = memcheck(self.wb, n, pattern=row_patterns[row], base=base, burst=255)
            row_errors[row] = [(addr, data, row_patterns[row]) for addr, data in errors]
            if row % row_progress == 0:
                print('.', end='', flush=True)
        return row_errors

    def errors_count(self, row_errors):
        return sum(1 if len(e) > 0 else 0 for e in row_errors.values())

    @staticmethod
    def bitcount(x):
        return bin(x).count('1')  # seems faster than operations on integers

    @classmethod
    def bitflips(cls, val, ref):
        return cls.bitcount(val ^ ref)

    def errors_bitcount(self, row_errors):
        return sum(
            sum(self.bitflips(value, expected)
                for addr, value, expected in e)
            for e in row_errors.values())

    def display_errors(self, row_errors):
        for row in row_errors:
            if len(row_errors[row]) > 0:
                print(
                    "Bit-flips for row {:{n}}: {}".format(
                        row,
                        sum(
                            self.bitflips(value, expected)
                            for addr, value, expected in row_errors[row]),
                        n=len(str(2**self.settings.geom.rowbits - 1))))
            if self.verbose:
                for i, word, expected in row_errors[row]:
                    base_addr = min(self.addresses_per_row(row))
                    addr = base_addr + 4 * i
                    bank, _row, col = self.converter.decode_bus(addr)
                    print(
                        "Error: 0x{:08x}: 0x{:08x} (row={}, col={})".format(addr, word, _row, col))

        if self.plot:
            from matplotlib import pyplot as plt
            row_err_counts = [len(row_errors.get(row, [])) for row in self.rows]
            plt.bar(self.rows, row_err_counts, width=1)
            plt.grid(True)
            plt.xlabel('Row')
            plt.ylabel('Errors')
            plt.show()

    def run(self, row_pairs, pattern_generator, read_count, row_progress=16, verify_initial=False):
        # TODO: need to invert data when writing/reading, make sure Python integer inversion works correctly
        if self.data_inversion:
            raise NotImplementedError('Currently only HW rowhammer supports data inversion')

        print('\nPreparing ...')
        row_patterns = pattern_generator(self.rows)

        print('\nFilling memory with data ...')
        for row, n, base in self.row_access_iterator():
            memfill(self.wb, n, pattern=row_patterns[row], base=base, burst=255)
            if row % row_progress == 0:
                print('.', end='', flush=True)
            # makes sure to synchronize with the writes (without it for slower conenction
            # we may have a timeout on the first read after writing)
            self.wb.regs.ctrl_scratch.read()

        if verify_initial:
            print('\nVerifying written memory ...')
            errors = self.check_errors(row_patterns, row_progress=row_progress)
            if self.errors_count(errors) == 0:
                print('OK')
            else:
                print()
                self.display_errors(errors)
                return

        if self.no_refresh:
            print('\nDisabling refresh ...')
            self.wb.regs.controller_settings_refresh.write(0)

        print('\nRunning Rowhammer attacks ...')
        for i, row_tuple in enumerate(row_pairs):
            s = 'Iter {:{n}} / {:{n}}'.format(i, len(row_pairs), n=len(str(len(row_pairs))))
            if self.payload_executor:
                self.payload_executor_attack(read_count=read_count, row_tuple=row_tuple)
            else:
                self.attack(row_tuple, read_count=read_count, progress_header=s)

        if self.no_refresh:
            print('\nReenabling refresh ...')
            self.wb.regs.controller_settings_refresh.write(1)

        print('\nVerifying attacked memory ...')
        errors = self.check_errors(row_patterns, row_progress=row_progress)
        if self.errors_count(errors) == 0:
            print('OK')
        else:
            print()
            self.display_errors(errors)
            return

    def payload_executor_attack(self, read_count, row_tuple):
        sys_clk_freq = float(get_generated_defs()['SYS_CLK_FREQ'])
        payload = generate_payload_from_row_list(
            read_count=read_count,
            row_sequence=row_tuple,
            timings=self.settings.timing,
            bankbits=self.settings.geom.bankbits,
            bank=self.bank,
            payload_mem_size=self.wb.mems.payload.size,
            refresh=not self.no_refresh,
            sys_clk_freq=sys_clk_freq,
        )

        execute_payload(self.wb, payload)


################################################################################


def patterns_const(rows, value):
    return {row: value for row in rows}


def patterns_alternating_per_row(rows):
    return {row: 0xffffffff if row % 2 == 0 else 0x00000000 for row in rows}


def patterns_random_per_row(rows, seed=42):
    rng = random.Random(seed)
    return {row: rng.randint(0, 2**32 - 1) for row in rows}


def main(row_hammer_cls):
    parser = argparse.ArgumentParser()
    parser.add_argument('--nrows', type=int, default=0, help='Number of rows to consider')
    parser.add_argument('--bank', type=int, default=0, help='Bank number')
    parser.add_argument('--column', type=int, default=512, help='Column to read from')
    parser.add_argument(
        '--start-row', type=int, default=0, help='Starting row (range = (start, start+nrows))')
    parser.add_argument(
        '--read_count',
        type=float,
        default=10e6,
        help='How many reads to perform for single address pair')
    parser.add_argument('--hammer-only', nargs=2, type=int, help='Run only the Rowhammer attack')
    parser.add_argument(
        '--no-refresh', action='store_true', help='Disable refresh commands during the attacks')
    parser.add_argument(
        '--pattern',
        default='01_per_row',
        choices=['all_0', 'all_1', '01_in_row', '01_per_row', 'rand_per_row'],
        help='Pattern written to DRAM before running attacks')
    parser.add_argument(
        '--row-pairs',
        choices=['sequential', 'const', 'random'],
        default='sequential',
        help='How the rows for subsequent attacks are selected')
    parser.add_argument(
        '--const-rows-pair',
        type=int,
        nargs='+',
        required=False,
        help='When using --row-pairs constant')
    parser.add_argument(
        '--plot', action='store_true',
        help='Plot errors distribution')  # requiers matplotlib and pyqt5 packages
    parser.add_argument(
        '--payload-executor',
        action='store_true',
        help='Do the attack using Payload Executor (1st row only)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Be more verbose')
    parser.add_argument("--srv", action="store_true", help='Start LiteX server')
    parser.add_argument(
        "--experiment-no", type=int, default=0, help='Run preconfigured experiment #no')
    parser.add_argument(
        "--data-inversion", nargs=2, help='Invert pattern data for victim rows (divisor, mask)')
    args = parser.parse_args()

    if args.experiment_no == 1:
        args.nrows = 512
        args.read_count = 15e6
        args.pattern = '01_in_row'
        args.row_pairs = 'const'
        args.const_rows_pair = 88, 99
        args.no_refresh = True

    if args.srv:
        litex_server()

    wb = RemoteClient()
    wb.open()

    row_hammer = row_hammer_cls(
        wb,
        nrows=args.nrows,
        settings=get_litedram_settings(),
        column=args.column,
        bank=args.bank,
        rows_start=args.start_row,
        verbose=args.verbose,
        plot=args.plot,
        no_refresh=args.no_refresh,
        payload_executor=args.payload_executor,
        data_inversion=args.data_inversion,
    )

    if args.hammer_only:
        row_hammer.attack(args.hammer_only, read_count=args.read_count)
    else:
        rng = random.Random(42)

        def rand_row():
            return rng.randint(args.start_row, args.start_row + args.nrows)

        assert not (
            args.row_pairs == 'const' and not args.const_rows_pair), 'Specify --const-rows-pair'
        row_pairs = {
            'sequential': [(0 + args.start_row, i + args.start_row) for i in range(args.nrows)],
            'const': [tuple(args.const_rows_pair) if args.const_rows_pair else ()],
            'random': [(rand_row(), rand_row()) for i in range(args.nrows)],
        }[args.row_pairs]

        pattern = {
            'all_0': lambda rows: patterns_const(rows, 0x00000000),
            'all_1': lambda rows: patterns_const(rows, 0xffffffff),
            '01_in_row': lambda rows: patterns_const(rows, 0xaaaaaaaa),
            '01_per_row': patterns_alternating_per_row,
            'rand_per_row': patterns_random_per_row,
        }[args.pattern]

        row_hammer.run(row_pairs=row_pairs, read_count=args.read_count, pattern_generator=pattern)

    wb.close()


if __name__ == "__main__":
    main(row_hammer_cls=RowHammer)
