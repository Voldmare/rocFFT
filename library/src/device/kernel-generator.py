#!/usr/bin/env python3
"""rocFFT kernel generator.

Currently this acts as a shim between CMake and the C++ kernel generator.

It accept two sub-commands:
1. list - lists files that will be generated
2. generate - pass arguments down to the old generator

Note that 'small' kernels don't decompose their lengths.

"""

import argparse
import collections
import copy
import functools
import itertools
import os
import subprocess
import sys

from pathlib import Path
from types import SimpleNamespace as NS
from functools import reduce
from operator import mul
from copy import deepcopy

from generator import (ArgumentList, BaseNode, Call, CommentBlock, ExternC, Function, Include,
                       LineBreak, Map, Pragma, StatementList, Variable, name_args, write)


import stockham

supported_large = [50, 52, 60, 64, 72, 80, 81, 84, 96, 100, 104, 108, 112, 128, 160, 168, 200, 208, 216, 224, 240, 256, 336]
old_gen_supported_large = [50, 64, 81, 100, 128, 200, 256]

#
# CMake helpers
#

def scjoin(xs):
    """Join 'xs' with semi-colons."""
    return ';'.join(str(x) for x in xs)


def scprint(xs):
    """Print 'xs', joined by semi-colons, on a single line.  CMake friendly."""
    print(scjoin(xs), end='', flush=True)


def cjoin(xs):
    """Join 'xs' with commas."""
    return ','.join(str(x) for x in xs)


#
# Helpers
#

def product(*args):
    """Cartesian product of input iteratables, as a list."""
    return list(itertools.product(*args))


def merge(*ds):
    """Merge dictionaries; last one wins."""
    r = collections.OrderedDict()
    for d in ds:
        r.update(d)
    return r


def pmerge(d, fs):
    """Merge d with dicts of {(length, precision, scheme, transpose): f}."""
    r = collections.OrderedDict()
    r.update(d)
    for f in fs:
        r[f.meta.length, f.meta.precision, f.meta.scheme, f.meta.transpose] = f
    return r


def flatten(lst):
    """Flatten a list of lists to a list."""
    return sum(lst, [])


# this function should eventually go away
def pick(all, new_kernels, subtract_from_all=True):
    """From all old kernels, pick out those supported by new kernel, and remove from old list."""
    old = collections.OrderedDict(all)
    new = []

    for nk in new_kernels:
        assert hasattr(nk, 'length')
        for target_length in all:
            if nk.length == target_length:
                new.append(nk) # pick out, put to new
                if subtract_from_all:
                    del old[target_length] # remove from old
                break
    # old-list to old-gen, new-list to new-gen
    return old, new


def merge_length(kernel_list, ks):
    """Merge kernel lists without duplicated meta.length; ignore later ones."""
    merged_list = list(kernel_list)
    lengths = [ item.length for item in kernel_list ]
    for k in ks:
        if k.length not in lengths:
            merged_list.append(k)
    return merged_list

#
# Supported kernel sizes
#

# this function should eventually go away
def supported_small_sizes(precision, pow2=True, pow3=True, pow5=True, commonRadix=True):
    """Return list of 1D small kernels."""

    upper_bound = {
        'sp': 4096,
        'dp': 4096,         # of course this isn't 2048... not sure why (double len 1594323 will fail)
    }

    powers = {
        5: [5**k for k in range(6 if pow5 else 1)],
        3: [3**k for k in range(8 if pow3 else 1)],
        2: [2**k for k in range(13 if pow2 else 1)],
    }

    lengths = [p2 * p3 * p5 for p2, p3, p5 in product(powers[2], powers[3], powers[5])]

    # common radix 7, 11, and 13
    if commonRadix:
        lengths += [7, 14, 21, 28, 42, 49, 56, 84, 112, 168, 224, 336, 343]
        lengths += [11, 22, 44, 88, 121, 176]
        lengths += [13, 17, 26, 52, 104, 169, 208, 272, 528, 1040]

    def filter_bound(length):
        return length <= upper_bound[precision]

    filtered = sorted([x for x in set(lengths) if filter_bound(x)])

    return product(filtered, ['CS_KERNEL_STOCKHAM'])


def supported_large_sizes(precision):
    """Return list of 1D large block kernels."""

    return product(supported_large, ['CS_KERNEL_STOCKHAM_BLOCK_CC',
                                     'CS_KERNEL_STOCKHAM_BLOCK_RC'])


# this function should eventually go away
def supported_2d_sizes(precision):
    """Return list of 2D kernels."""

    # for now, mimic order of old generator so diffing is easier
    powers = {
        5: [5**k for k in range(3, 1, -1)],
        3: [3**k for k in range(5, 1, -1)],
        2: [2**k for k in range(8, 1, -1)],
    }

    lengths = []
    for b1, b2 in [(2, 2), (3, 3), (5, 5), (2, 3), (3, 2), (3, 5), (5, 3), (2, 5), (5, 2)]:
        lengths.extend(product(powers[b1], powers[b2]))

    max_lds_size_bytes = 64 * 1024
    bytes_per_element = {'sp': 8, 'dp': 16}[precision]

    def filter_lds(length):
        return length[0] * length[1] * bytes_per_element * 1.5 <= max_lds_size_bytes

    # explicit list of fused 2D kernels that the old generator doesn't
    # like; usually because the thread counts are wonky.
    avoid = {
        'sp': [(16, 243), (16, 256), (27, 125), (27, 128), (64, 64), (64, 81)],
        'dp': [(16, 243), (16, 256), (25, 125), (27, 125), (32, 125), (25, 128), (27, 128), (32, 128), (64, 64), (64, 81)]
    }[precision]

    def filter_threads(length):
        rlength = (length[1], length[0])
        return length not in avoid and rlength not in avoid

    filtered = [x for x in lengths if filter_lds(x) and filter_threads(x)]

    return product(filtered, ['CS_KERNEL_2D_SINGLE'])


# this function should eventually go away
def get_dependent_1D_sizes(list_2D):
    dep_1D = set()
    for problem in list_2D:
        dep_1D.update( [problem[0][0], problem[0][1]] )

    return product(dep_1D, ['CS_KERNEL_STOCKHAM'])

#
# Prototype generators
#

@name_args(['function'])
class FFTKernel(BaseNode):
    def __str__(self):
        f = 'FFTKernel('
        if self.function.meta.runtime_compile:
            f += 'nullptr'
        else:
            f += str(self.function.address())
        use_3steps_large_twd = getattr(self.function.meta, 'use_3steps_large_twd', None)
        if use_3steps_large_twd is not None:
            f += ', ' + str(use_3steps_large_twd[self.function.meta.precision])
        else:
            f += ', false'
        factors = getattr(self.function.meta, 'factors', None)
        if factors is not None:
            f += ', {' + cjoin(factors) + '}'
        transforms_per_block = getattr(self.function.meta, 'transforms_per_block', None)
        if transforms_per_block is not None:
            f += ', ' + str(transforms_per_block)
        threads_per_block = getattr(self.function.meta, 'threads_per_block', None)
        if threads_per_block is not None:
            f += ', ' + str(threads_per_block)
        half_lds = None
        if hasattr(self.function.meta, 'params'):
            half_lds = getattr(self.function.meta.params, 'half_lds', None)
        if half_lds is not None:
            f += ', ' + str(half_lds).lower()
        f += ')'
        return f


def generate_cpu_function_pool(functions):
    """Generate function to populate the kernel function pool."""

    function_map = Map('function_map')
    precisions = { 'sp': 'rocfft_precision_single',
                   'dp': 'rocfft_precision_double' }

    populate = StatementList()
    for f in functions:
        length, precision, scheme, transpose = f.meta.length, f.meta.precision, f.meta.scheme, f.meta.transpose
        if isinstance(length, (int, str)):
            length = [length, 0]
        key = Call(name='std::make_tuple',
                   arguments=ArgumentList('std::array<size_t, 2>({' + cjoin(length) + '})',
                                          precisions[precision],
                                          scheme,
                                          transpose or 'NONE')).inline()
        populate += function_map.assert_emplace(key, FFTKernel(f))

    return StatementList(
        Include('<iostream>'),
        Include('"../include/function_pool.h"'),
        StatementList(*[f.prototype() for f in functions]),
        Function(name='function_pool::function_pool',
                 value=False,
                 arguments=ArgumentList(),
                 body=populate))


# this function should eventually go away
def generate_small_1d_prototypes(precision, transforms):
    """Generate prototypes for 1D small kernels that will be generated by the old generator."""

    data = Variable('data_p', 'const void *')
    back = Variable('back_p', 'void *')
    functions = []

    def add(name, scheme, transpose=None):
        functions.append(Function(name=name,
                                  arguments=ArgumentList(data, back),
                                  meta=NS(
                                      length=length,
                                      precision=precision,
                                      scheme=scheme,
                                      transpose=transpose,
                                      runtime_compile=False)))

    for length, scheme in transforms.items():
        add(f'rocfft_internal_dfn_{precision}_ci_ci_stoc_{length}', scheme)

    return functions


# this function should eventually go away
def generate_large_1d_prototypes(precision, transforms):
    """Generate prototypes for 1D large block kernels that will be generated from the old generator."""

    data = Variable('data_p', 'const void *')
    back = Variable('back_p', 'void *')
    functions = []

    def add(name, scheme, transpose=None):
        use3Steps = {'sp': 'true', 'dp': 'true'}
        if length == 81:
            use3Steps['dp'] = 'false'
        elif length == 200:
            use3Steps['sp'] = use3Steps['dp'] = 'false'
        functions.append(Function(name=name,
                                  arguments=ArgumentList(data, back),
                                  meta=NS(
                                      length=length,
                                      precision=precision,
                                      scheme=scheme,
                                      use_3steps_large_twd=use3Steps,
                                      transpose=transpose,
                                      runtime_compile=False)))

    for length, scheme in transforms.items():
        if 0:
            add(f'rocfft_internal_dfn_{precision}_ci_ci_sbcc_{length}', 'CS_KERNEL_STOCKHAM_BLOCK_CC')
        elif scheme == 'CS_KERNEL_STOCKHAM_BLOCK_RC':
            # for old-sbcc compatibility: always include the sbcc function (but will be overwritten if new gen has it)
            add(f'rocfft_internal_dfn_{precision}_ci_ci_sbcc_{length}', 'CS_KERNEL_STOCKHAM_BLOCK_CC')
            add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc_{length}', 'CS_KERNEL_STOCKHAM_BLOCK_RC')
            add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_trans_xy_z_tile_aligned_{length}', 'CS_KERNEL_STOCKHAM_TRANSPOSE_XY_Z', 'TILE_ALIGNED')
            add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_trans_z_xy_tile_aligned_{length}', 'CS_KERNEL_STOCKHAM_TRANSPOSE_Z_XY', 'TILE_ALIGNED')
            add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_erc_trans_z_xy_tile_aligned_{length}', 'CS_KERNEL_STOCKHAM_R_TO_CMPLX_TRANSPOSE_Z_XY', 'TILE_ALIGNED')
            if length in [128, 256]:
                add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_trans_xy_z_diagonal_{length}', 'CS_KERNEL_STOCKHAM_TRANSPOSE_XY_Z', 'DIAGONAL')
                add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_trans_z_xy_diagonal_{length}', 'CS_KERNEL_STOCKHAM_TRANSPOSE_Z_XY', 'DIAGONAL')
                add(f'rocfft_internal_dfn_{precision}_op_ci_ci_sbrc3d_fft_erc_trans_z_xy_diagonal_{length}', 'CS_KERNEL_STOCKHAM_R_TO_CMPLX_TRANSPOSE_Z_XY', 'DIAGONAL')

    return functions


# this function should eventually go away
def generate_2d_prototypes(precision, transforms):
    """Generate prototypes for 2D kernels that will be generated by the old generator."""

    data = Variable('data_p', 'const void *')
    back = Variable('back_p', 'void *')
    functions = []

    def add(name, scheme, transpose=None):
        functions.append(Function(name=name,
                                  arguments=ArgumentList(data, back),
                                  meta=NS(
                                      length=length,
                                      precision=precision,
                                      scheme=scheme,
                                      transpose=transpose,
                                      runtime_compile=False)))

    for length, scheme in transforms.items():
        add(f'rocfft_internal_dfn_{precision}_ci_ci_2D_{length[0]}_{length[1]}', 'CS_KERNEL_2D_SINGLE', 'NONE')

    return functions


# this function should eventually go away
def list_old_generated_kernels(patterns=None,
                               precisions=None,
                               num_small_kernel_groups=150):
    """Return a list (for CMake) of files created by the (old) generator."""

    if patterns is None:
        patterns = ['all']
    if precisions is None:
        precisions = ['all']

    #
    # all this 'generated_kernels' should go away when the old generator goes away
    #
    generated_kernels = {
        'kernels_launch_basic': [
            'function_pool.cpp',
        ],
        'kernels_launch_small_sp':
          [f'kernel_launch_single_{i}.cpp' for i in range(num_small_kernel_groups)]
          + [f'kernel_launch_single_{i}.cpp.h' for i in range(num_small_kernel_groups)],
        'kernels_launch_small_dp':
          [f'kernel_launch_double_{i}.cpp' for i in range(num_small_kernel_groups)]
          + [f'kernel_launch_double_{i}.cpp.h' for i in range(num_small_kernel_groups)],
        'kernels_launch_large_sp': [
            'kernel_launch_single_large.cpp',
        ],
        'kernels_launch_large_dp': [
            'kernel_launch_double_large.cpp',
        ],
    }
    generated_kernels['kernels_launch_small_all'] = generated_kernels['kernels_launch_small_sp'] + generated_kernels['kernels_launch_small_dp']
    generated_kernels['kernels_launch_large_all'] = generated_kernels['kernels_launch_large_sp'] + generated_kernels['kernels_launch_large_dp']
    generated_kernels['kernels_launch_all_sp']    = generated_kernels['kernels_launch_small_sp'] + generated_kernels['kernels_launch_large_sp']
    generated_kernels['kernels_launch_all_dp']    = generated_kernels['kernels_launch_small_dp'] + generated_kernels['kernels_launch_large_dp']
    generated_kernels['kernels_launch_all_all']   = generated_kernels['kernels_launch_all_sp']   + generated_kernels['kernels_launch_all_dp']

    gen = generated_kernels['kernels_launch_basic']
    for patt in patterns:
        # old gen no longer handles any 2D_SINGLE kernels
        if patt == '2D':
            continue
        for prec in precisions:
            gen += generated_kernels[f'kernels_launch_{patt}_{prec}']
    return list(set(gen))


def list_generated_kernels(kernels):
    """Return list of kernel filenames."""
    return [kernel_file_name(x) for x in kernels if not x.runtime_compile]


#
# Main!
#

@name_args(['name', 'ip_fwd', 'ip_inv', 'op_fwd', 'op_inv', 'precision'])
class POWX_SMALL_GENERATOR(BaseNode):
    def __str__(self):
        return f'POWX_SMALL_GENERATOR({cjoin(self.args)});'
    def function(self, meta, precision):
        data = Variable('data_p', 'const void *')
        back = Variable('back_p', 'void *')
        meta = NS(precision=precision, **meta.__dict__)
        return Function(name=self.name,
                        arguments=ArgumentList(data, back),
                        meta=meta)


@name_args(['name', 'ip_fwd', 'ip_inv', 'op_fwd', 'op_inv', 'precision'])
class POWX_LARGE_SBCC_GENERATOR(POWX_SMALL_GENERATOR):
    def __str__(self):
        return f'POWX_LARGE_SBCC_GENERATOR({cjoin(self.args)});'

@name_args(['name', 'op_fwd', 'op_inv', 'precision', 'sbrc_type', 'transpose_type'])
class POWX_LARGE_SBRC_GENERATOR(POWX_SMALL_GENERATOR):
    def __str__(self):
        return f'POWX_LARGE_SBRC_GENERATOR({cjoin(self.args)});'


@name_args(['name', 'op_fwd', 'op_inv', 'precision'])
class POWX_LARGE_SBCR_GENERATOR(POWX_SMALL_GENERATOR):
    def __str__(self):
        return f'POWX_LARGE_SBCR_GENERATOR({cjoin(self.args)});'


def kernel_file_name(ns):
    """Given kernel info namespace, return reasonable file name."""

    assert hasattr(ns, 'length')
    length = ns.length

    if isinstance(length, (tuple, list)):
        length = 'x'.join(str(x) for x in length)

    postfix = ''
    if ns.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_CC':
        postfix = '_sbcc'
    elif ns.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_RC':
        postfix = '_sbrc'
    elif ns.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_CR':
        postfix = '_sbcr'

    return f'rocfft_len{length}{postfix}.cpp'


def list_new_kernels():
    """Return list of kernels to generate with the new generator."""

    # remaining lenghts less than 1024: 121 192 224 250 320 336 375
    # 384 405 432 450 480 500 512 576 600 625 640 675 750 768 800 810
    # 864 900 972 1000


    # dictionary of (flavour, threads_per_block) -> list of kernels to generate
    # note the length property is necessary for the latter pick and merge_length
    small_kernels = {
        ('uwide', 256): [
#            NS(length=2, factors=[2]),
#            NS(length=3, factors=[3]),
#            NS(length=5, factors=[5]),
#            NS(length=6, factors=[6]),
#            NS(length=7, factors=[7]),
#            NS(length=8, factors=[8]),
            NS(length=9, factors=[3,3], runtime_compile=True),
#            NS(length=10, factors=[10]),
            NS(length=12, factors=[6,2]),
            NS(length=14, factors=[7,2]),
            NS(length=15, factors=[5,3]),
            NS(length=17, factors=[17]),
#            NS(length=18, factors=[6,3]),
            NS(length=20, factors=[10,2]),
            NS(length=21, factors=[7,3]),
            NS(length=24, factors=[8,3]),
            NS(length=25, factors=[5,5]),
#            NS(length=27, factors=[3,3,3]),
            NS(length=28, factors=[7,4]),
            NS(length=30, factors=[10,3]),
            NS(length=36, factors=[6,6]),
            NS(length=42, factors=[7,6]),
            NS(length=45, factors=[5,3,3]),
#            NS(length=49, factors=[7,7]),
            NS(length=50, factors=[10,5]),
            NS(length=54, factors=[6,3,3]),
            NS(length=56, factors=[8,7]),
#            NS(length=64, factors=[16,4]),
#            NS(length=72, factors=[8,3,3]),
            NS(length=75, factors=[5,5,3]),
            NS(length=80, factors=[16,5]),
#            NS(length=81, factors=[3,3,3,3]),
#            NS(length=100, factors=[10,10]),
            NS(length=108, factors=[6,6,3]),
            NS(length=112, factors=[16,7]),
            NS(length=125, factors=[5,5,5]),
#            NS(length=128, factors=[16,8]),
#            NS(length=135, factors=[5,3,3,3]),
#            NS(length=150, factors=[10,5,3]),
            NS(length=160, factors=[16,10]),
#            NS(length=162, factors=[6,3,3,3]),
            NS(length=168, factors=[8,7,3]),
            NS(length=180, factors=[10,6,3]),
#            NS(length=216, factors=[8,3,3,3]),
            NS(length=225, factors=[5,5,3,3]),
            NS(length=240, factors=[16,5,3]),
#            NS(length=243, factors=[3,3,3,3,3]),
#            NS(length=256, factors=[16,16]),
#            NS(length=270, factors=[10,3,3,3]),
#            NS(length=288, factors=[16,6,3]),
            NS(length=324, factors=[6,6,3,3]),
            NS(length=343, factors=[7,7,7]),
            NS(length=360, factors=[10,6,6]),
            NS(length=400, factors=[16,5,5]),
#            NS(length=486, factors=[6,3,3,3,3]),
#            NS(length=540, factors=[10,6,3,3]),
            NS(length=648, factors=[8,3,3,3,3]),
            NS(length=720, factors=[16,5,3,3]),
#            NS(length=729, factors=[3,3,3,3,3,3]),
            NS(length=960, factors=[16,10,6]),
            NS(length=1040, factors=[13,16,5]),
        ],
        ('uwide', 128): [
            NS(length=96, factors=[6,16]),
            NS(length=272, factors=[16,17]),
        ],
        ('wide', 64): [
#            NS(length=11, factors=[11]),
            NS(length=22, factors=[2,11]),
            NS(length=44, factors=[4,11]),
            NS(length=52, factors=[13,4]),
            NS(length=60, factors=[6,10]),
            NS(length=84, factors=[2,6,7]),
            NS(length=90, factors=[3,3,10]),
            NS(length=120, factors=[2,6,10]),
#            NS(length=200, factors=[2,10,10]),
            NS(length=300, factors=[3,10,10]),
            NS(length=528, factors=[4,4,3,11]),
        ],
        ('uwide', 64): [
            NS(length=32, factors=[16,2]),
            NS(length=40, factors=[10,4]),
            NS(length=48, factors=[3,4,4]),
            NS(length=88, factors=[11,8]),
            NS(length=176, factors=[16,11]),
            NS(length=336, factors=[7,8,6]),
        ],
        # ('tall', X): [
        #     NS(length=4),
        #     NS(length=13),
        #     NS(length=16),
        #     NS(length=26),
        #     NS(length=52),
        #     NS(length=104),
        #     NS(length=169),
        #     NS(length=192),
        #     NS(length=208),
        #     NS(length=320),
        #     NS(length=512),
        #     NS(length=625),
        #     NS(length=864),
        #     NS(length=1000),
        # ]
    }

    expanded = []
    for params, kernels in small_kernels.items():
        flavour, threads_per_block = params
        expanded.extend(NS(**kernel.__dict__,
                           flavour=flavour,
                           threads_per_block=threads_per_block,
                           half_lds=kernel.length==56,
                           scheme='CS_KERNEL_STOCKHAM') for kernel in kernels)

    return expanded

def list_new_2d_kernels():
    """Return list of fused 2D kernels to generate with new generator."""

    # can probably merge this with above when old gen is gone

    fused_kernels = [
        NS(length=[4,4], factors=[[2,2],[2,2]], threads_per_transform=[2,2], threads_per_block=8),
        NS(length=[4,8], factors=[[2,2],[4,2]], threads_per_transform=[2,2], threads_per_block=16),
        NS(length=[4,9], factors=[[2,2],[3,3]], threads_per_transform=[2,3], threads_per_block=18),
        NS(length=[4,16], factors=[[2,2],[4,4]], threads_per_transform=[2,4], threads_per_block=32),
        NS(length=[4,25], factors=[[2,2],[5,5]], threads_per_transform=[2,5], threads_per_block=50),
        NS(length=[4,27], factors=[[2,2],[3,3,3]], threads_per_transform=[2,9], threads_per_block=54),
        NS(length=[4,32], factors=[[2,2],[8,4]], threads_per_transform=[2,4], threads_per_block=64),
        NS(length=[4,64], factors=[[2,2],[4,4,4]], threads_per_transform=[2,16], threads_per_block=128),
        NS(length=[4,81], factors=[[2,2],[3,3,3,3]], threads_per_transform=[2,27], threads_per_block=162),
        NS(length=[4,125], factors=[[2,2],[5,5,5]], threads_per_transform=[2,25], threads_per_block=250),
        NS(length=[4,128], factors=[[2,2],[8,4,4]], threads_per_transform=[2,16], threads_per_block=256),
        NS(length=[4,243], factors=[[2,2],[3,3,3,3,3]], threads_per_transform=[2,81], threads_per_block=486),
        NS(length=[4,256], factors=[[2,2],[4,4,4,4]], threads_per_transform=[2,64], threads_per_block=512),
        NS(length=[8,4], factors=[[4,2],[2,2]], threads_per_transform=[2,2], threads_per_block=16),
        NS(length=[8,8], factors=[[4,2],[4,2]], threads_per_transform=[2,2], threads_per_block=16),
        NS(length=[8,9], factors=[[4,2],[3,3]], threads_per_transform=[2,3], threads_per_block=24),
        NS(length=[8,16], factors=[[4,2],[4,4]], threads_per_transform=[2,4], threads_per_block=32),
        NS(length=[8,25], factors=[[4,2],[5,5]], threads_per_transform=[2,5], threads_per_block=50),
        NS(length=[8,27], factors=[[4,2],[3,3,3]], threads_per_transform=[2,9], threads_per_block=72),
        NS(length=[8,32], factors=[[4,2],[8,4]], threads_per_transform=[2,4], threads_per_block=64),
        NS(length=[8,64], factors=[[4,2],[4,4,4]], threads_per_transform=[2,16], threads_per_block=128),
        NS(length=[8,81], factors=[[4,2],[3,3,3,3]], threads_per_transform=[2,27], threads_per_block=216),
        NS(length=[8,125], factors=[[4,2],[5,5,5]], threads_per_transform=[2,25], threads_per_block=250),
        NS(length=[8,128], factors=[[4,2],[8,4,4]], threads_per_transform=[2,16], threads_per_block=256),
        NS(length=[8,243], factors=[[4,2],[3,3,3,3,3]], threads_per_transform=[2,81], threads_per_block=648),
        NS(length=[8,256], factors=[[4,2],[4,4,4,4]], threads_per_transform=[2,64], threads_per_block=512),
        NS(length=[9,4], factors=[[3,3],[2,2]], threads_per_transform=[3,2], threads_per_block=18),
        NS(length=[9,8], factors=[[3,3],[4,2]], threads_per_transform=[3,2], threads_per_block=24),
        NS(length=[9,9], factors=[[3,3],[3,3]], threads_per_transform=[3,3], threads_per_block=27),
        NS(length=[9,16], factors=[[3,3],[4,4]], threads_per_transform=[3,4], threads_per_block=48),
        NS(length=[9,25], factors=[[3,3],[5,5]], threads_per_transform=[3,5], threads_per_block=75),
        NS(length=[9,27], factors=[[3,3],[3,3,3]], threads_per_transform=[3,9], threads_per_block=81),
        NS(length=[9,32], factors=[[3,3],[8,4]], threads_per_transform=[3,4], threads_per_block=96),
        NS(length=[9,64], factors=[[3,3],[4,4,4]], threads_per_transform=[3,16], threads_per_block=192),
        NS(length=[9,81], factors=[[3,3],[3,3,3,3]], threads_per_transform=[3,27], threads_per_block=243),
        NS(length=[9,125], factors=[[3,3],[5,5,5]], threads_per_transform=[3,25], threads_per_block=375),
        NS(length=[9,128], factors=[[3,3],[8,4,4]], threads_per_transform=[3,16], threads_per_block=384),
        NS(length=[9,243], factors=[[3,3],[3,3,3,3,3]], threads_per_transform=[3,81], threads_per_block=729),
        NS(length=[9,256], factors=[[3,3],[4,4,4,4]], threads_per_transform=[3,64], threads_per_block=768),
        NS(length=[16,4], factors=[[4,4],[2,2]], threads_per_transform=[4,2], threads_per_block=32),
        NS(length=[16,8], factors=[[4,4],[4,2]], threads_per_transform=[4,2], threads_per_block=32),
        NS(length=[16,9], factors=[[4,4],[3,3]], threads_per_transform=[4,3], threads_per_block=48),
        NS(length=[16,16], factors=[[4,4],[4,4]], threads_per_transform=[4,4], threads_per_block=64),
        NS(length=[16,25], factors=[[4,4],[5,5]], threads_per_transform=[4,5], threads_per_block=100),
        NS(length=[16,27], factors=[[4,4],[3,3,3]], threads_per_transform=[4,9], threads_per_block=144),
        NS(length=[16,32], factors=[[4,4],[8,4]], threads_per_transform=[4,4], threads_per_block=128),
        NS(length=[16,64], factors=[[4,4],[4,4,4]], threads_per_transform=[4,16], threads_per_block=256),
        NS(length=[16,81], factors=[[4,4],[3,3,3,3]], threads_per_transform=[4,27], threads_per_block=432),
        NS(length=[16,125], factors=[[4,4],[5,5,5]], threads_per_transform=[4,25], threads_per_block=500),
        NS(length=[16,128], factors=[[4,4],[8,4,4]], threads_per_transform=[4,16], threads_per_block=512),
        NS(length=[25,4], factors=[[5,5],[2,2]], threads_per_transform=[5,2], threads_per_block=50),
        NS(length=[25,8], factors=[[5,5],[4,2]], threads_per_transform=[5,2], threads_per_block=50),
        NS(length=[25,9], factors=[[5,5],[3,3]], threads_per_transform=[5,3], threads_per_block=75),
        NS(length=[25,16], factors=[[5,5],[4,4]], threads_per_transform=[5,4], threads_per_block=100),
        NS(length=[25,25], factors=[[5,5],[5,5]], threads_per_transform=[5,5], threads_per_block=125),
        NS(length=[25,27], factors=[[5,5],[3,3,3]], threads_per_transform=[5,9], threads_per_block=225),
        NS(length=[25,32], factors=[[5,5],[8,4]], threads_per_transform=[5,4], threads_per_block=160),
        NS(length=[25,64], factors=[[5,5],[4,4,4]], threads_per_transform=[5,16], threads_per_block=400),
        NS(length=[25,81], factors=[[5,5],[3,3,3,3]], threads_per_transform=[5,27], threads_per_block=675),
        NS(length=[25,125], factors=[[5,5],[5,5,5]], threads_per_transform=[5,25], threads_per_block=625),
        NS(length=[25,128], factors=[[5,5],[8,4,4]], threads_per_transform=[5,16], threads_per_block=640),
        NS(length=[27,4], factors=[[3,3,3],[2,2]], threads_per_transform=[9,2], threads_per_block=54),
        NS(length=[27,8], factors=[[3,3,3],[4,2]], threads_per_transform=[9,2], threads_per_block=72),
        NS(length=[27,9], factors=[[3,3,3],[3,3]], threads_per_transform=[9,3], threads_per_block=81),
        NS(length=[27,16], factors=[[3,3,3],[4,4]], threads_per_transform=[9,4], threads_per_block=144),
        NS(length=[27,25], factors=[[3,3,3],[5,5]], threads_per_transform=[9,5], threads_per_block=225),
        NS(length=[27,27], factors=[[3,3,3],[3,3,3]], threads_per_transform=[9,9], threads_per_block=243),
        NS(length=[27,32], factors=[[3,3,3],[8,4]], threads_per_transform=[9,4], threads_per_block=288),
        NS(length=[27,64], factors=[[3,3,3],[4,4,4]], threads_per_transform=[9,16], threads_per_block=576),
        NS(length=[27,81], factors=[[3,3,3],[3,3,3,3]], threads_per_transform=[9,27], threads_per_block=729),
        NS(length=[32,4], factors=[[8,4],[2,2]], threads_per_transform=[4,2], threads_per_block=64),
        NS(length=[32,8], factors=[[8,4],[4,2]], threads_per_transform=[4,2], threads_per_block=64),
        NS(length=[32,9], factors=[[8,4],[3,3]], threads_per_transform=[4,3], threads_per_block=96),
        NS(length=[32,16], factors=[[8,4],[4,4]], threads_per_transform=[4,4], threads_per_block=128),
        NS(length=[32,25], factors=[[8,4],[5,5]], threads_per_transform=[4,5], threads_per_block=160),
        NS(length=[32,27], factors=[[8,4],[3,3,3]], threads_per_transform=[4,9], threads_per_block=288),
        NS(length=[32,32], factors=[[8,4],[8,4]], threads_per_transform=[4,4], threads_per_block=128),
        NS(length=[32,64], factors=[[8,4],[4,4,4]], threads_per_transform=[4,16], threads_per_block=512),
        NS(length=[32,81], factors=[[8,4],[3,3,3,3]], threads_per_transform=[4,27], threads_per_block=864),
        NS(length=[32,125], factors=[[8,4],[5,5,5]], threads_per_transform=[4,25], threads_per_block=800),
        NS(length=[32,128], factors=[[8,4],[8,4,4]], threads_per_transform=[4,16], threads_per_block=512),
        NS(length=[64,4], factors=[[4,4,4],[2,2]], threads_per_transform=[16,2], threads_per_block=128),
        NS(length=[64,8], factors=[[4,4,4],[4,2]], threads_per_transform=[16,2], threads_per_block=128),
        NS(length=[64,9], factors=[[4,4,4],[3,3]], threads_per_transform=[16,3], threads_per_block=192),
        NS(length=[64,16], factors=[[4,4,4],[4,4]], threads_per_transform=[16,4], threads_per_block=256),
        NS(length=[64,25], factors=[[4,4,4],[5,5]], threads_per_transform=[16,5], threads_per_block=400),
        NS(length=[64,27], factors=[[4,4,4],[3,3,3]], threads_per_transform=[16,9], threads_per_block=576),
        NS(length=[64,32], factors=[[4,4,4],[8,4]], threads_per_transform=[16,4], threads_per_block=512),
        NS(length=[81,4], factors=[[3,3,3,3],[2,2]], threads_per_transform=[27,2], threads_per_block=162),
        NS(length=[81,8], factors=[[3,3,3,3],[4,2]], threads_per_transform=[27,2], threads_per_block=216),
        NS(length=[81,9], factors=[[3,3,3,3],[3,3]], threads_per_transform=[27,3], threads_per_block=243),
        NS(length=[81,16], factors=[[3,3,3,3],[4,4]], threads_per_transform=[27,4], threads_per_block=432),
        NS(length=[81,25], factors=[[3,3,3,3],[5,5]], threads_per_transform=[27,5], threads_per_block=675),
        NS(length=[81,27], factors=[[3,3,3,3],[3,3,3]], threads_per_transform=[27,9], threads_per_block=729),
        NS(length=[81,32], factors=[[3,3,3,3],[8,4]], threads_per_transform=[27,4], threads_per_block=864),
        NS(length=[125,4], factors=[[5,5,5],[2,2]], threads_per_transform=[25,2], threads_per_block=250),
        NS(length=[125,8], factors=[[5,5,5],[4,2]], threads_per_transform=[25,2], threads_per_block=250),
        NS(length=[125,9], factors=[[5,5,5],[3,3]], threads_per_transform=[25,3], threads_per_block=375),
        NS(length=[125,16], factors=[[5,5,5],[4,4]], threads_per_transform=[25,4], threads_per_block=500),
        NS(length=[125,25], factors=[[5,5,5],[5,5]], threads_per_transform=[25,5], threads_per_block=625),
        NS(length=[125,32], factors=[[5,5,5],[8,4]], threads_per_transform=[25,4], threads_per_block=800),
        NS(length=[128,4], factors=[[8,4,4],[2,2]], threads_per_transform=[16,2], threads_per_block=256),
        NS(length=[128,8], factors=[[8,4,4],[4,2]], threads_per_transform=[16,2], threads_per_block=256),
        NS(length=[128,9], factors=[[8,4,4],[3,3]], threads_per_transform=[16,3], threads_per_block=384),
        NS(length=[128,16], factors=[[8,4,4],[4,4]], threads_per_transform=[16,4], threads_per_block=512),
        NS(length=[128,25], factors=[[8,4,4],[5,5]], threads_per_transform=[16,5], threads_per_block=640),
        NS(length=[128,32], factors=[[8,4,4],[8,4]], threads_per_transform=[16,4], threads_per_block=512),
        NS(length=[243,4], factors=[[3,3,3,3,3],[2,2]], threads_per_transform=[81,2], threads_per_block=486),
        NS(length=[243,8], factors=[[3,3,3,3,3],[4,2]], threads_per_transform=[81,2], threads_per_block=648),
        NS(length=[243,9], factors=[[3,3,3,3,3],[3,3]], threads_per_transform=[81,3], threads_per_block=729),
        NS(length=[256,4], factors=[[4,4,4,4],[2,2]], threads_per_transform=[64,2], threads_per_block=512),
        NS(length=[256,8], factors=[[4,4,4,4],[4,2]], threads_per_transform=[64,2], threads_per_block=512),
        NS(length=[256,9], factors=[[4,4,4,4],[3,3]], threads_per_transform=[64,3], threads_per_block=768),
    ]

    expanded = []
    expanded.extend(NS(**kernel.__dict__,
                       scheme='CS_KERNEL_2D_SINGLE') for kernel in fused_kernels)

    return expanded


def list_new_large_kernels():
    """Return list of large kernels to generate with the new generator."""

    sbcc_kernels = [
        NS(length=50,  factors=[10, 5],      use_3steps_large_twd={
           'sp': 'true',  'dp': 'true'}, threads_per_block=256),
        NS(length=52,  factors=[13, 4],      use_3steps_large_twd={
           'sp': 'true',  'dp': 'true'}),
        NS(length=60,  factors=[6, 10],      use_3steps_large_twd={
           'sp': 'false',  'dp': 'false'}),
        NS(length=64,  factors=[8, 8],       use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=72,  factors=[8, 3, 3],    use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=80,  factors=[10, 8],    use_3steps_large_twd={
           'sp': 'false',  'dp': 'false'}),
        NS(length=81,  factors=[3, 3, 3, 3], use_3steps_large_twd={
           'sp': 'true',  'dp': 'true'}),
        NS(length=84,  factors=[7, 2, 6],    use_3steps_large_twd={
           'sp': 'true',  'dp': 'true'}),
        NS(length=96,  factors=[6, 16],      use_3steps_large_twd={
           'sp': 'false',  'dp': 'false'}),
        NS(length=100, factors=[5, 5, 4],    use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}, threads_per_block=100),
        NS(length=104, factors=[13, 8],      use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=108, factors=[6, 6, 3],    use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=112, factors=[4, 7, 4],    use_3steps_large_twd={
           'sp': 'false',  'dp': 'false'}),
        NS(length=128, factors=[8, 4, 4],    use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=160, factors=[4, 10, 4],   use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}, flavour='wide'),
        NS(length=168, factors=[7, 6, 4],    use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}, threads_per_block=128),
        NS(length=192, factors=[6, 4, 4, 2], use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=200, factors=[8, 5, 5],    use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=208, factors=[13, 16],     use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=216, factors=[8, 3, 3, 3], use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=224, factors=[8, 7, 4],    use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=240, factors=[8, 5, 6],    use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'}),
        NS(length=256, factors=[4, 4, 4, 4], use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'}),
        NS(length=336, factors=[6, 7, 8],    use_3steps_large_twd={
           'sp': 'false', 'dp': 'false'})
    ]

    # for SBCC kernel, increase desired threads_per_block so that columns per
    # thread block is also increased. currently targeting for 16 columns
    block_width = 16
    for k in sbcc_kernels:
        k.scheme = 'CS_KERNEL_STOCKHAM_BLOCK_CC'
        if not hasattr(k, 'threads_per_block'):
            k.threads_per_block = block_width * \
                reduce(mul, k.factors, 1) // min(k.factors)
        if not hasattr(k, 'length'):
            k.length = functools.reduce(lambda a, b: a * b, k.factors)

    # SBRC
    sbrc_kernels = [
        NS(length=64,  factors=[4, 4, 4], scheme='CS_KERNEL_STOCKHAM_BLOCK_RC', threads_per_block=128),
        # NS(length=128, factors=[8, 4, 4], scheme='CS_KERNEL_STOCKHAM_BLOCK_RC', threads_per_block=128),
    ]

    # NB:
    # Technically, we could have SBCR kernels the same amount as SBCC.
    #
    # sbcr_kernels = copy.deepcopy(sbcc_kernels)
    # for k in sbcr_kernels:
    #     k.scheme = 'CS_KERNEL_STOCKHAM_BLOCK_CR'
    #
    # Just enable length 64 for now.

    sbcr_kernels = [
        NS(length=64,  factors=[8, 8],       use_3steps_large_twd={
           'sp': 'true',  'dp': 'false'})
    ]

    block_width = 16
    for k in sbcr_kernels:
        k.scheme = 'CS_KERNEL_STOCKHAM_BLOCK_CR'
        if not hasattr(k, 'threads_per_block'):
            k.threads_per_block = block_width * \
                reduce(mul, k.factors, 1) // min(k.factors)
        if not hasattr(k, 'length'):
            k.length = functools.reduce(lambda a, b: a * b, k.factors)

    return sbcc_kernels + sbcr_kernels + sbrc_kernels


def default_runtime_compile(kernels):
    '''Returns a copy of input kernel list with a default value for runtime_compile.'''

    return [k if hasattr(k, 'runtime_compile') else NS(**k.__dict__, runtime_compile=False) for k in kernels]

def generate_kernel(kernel, precisions):
    """Generate a single kernel file for 'kernel'.

    The kernel file contains all kernel variations corresponding to
    the kernel meta data in 'kernel'.

    A list of CPU functions is returned.
    """

    fname = Path(__file__).resolve()

    typename_dict = {
        'sp': 'float2',
        'dp': 'double2',
    }

    src = StatementList(
        CommentBlock(
            'Stockham kernels generated by:',
            '',
            '    ' + ' '.join(sys.argv),
            '',
            'Generator is: ' + str(fname),
            ''
            'Kernel is: ' + str(kernel)),
        LineBreak(),
        Include('<hip/hip_runtime.h>'),
        Include('"kernel_launch.h"'),
        Include('"kernels/common.h"'),
        Include('"kernels/butterfly_constant.h"'),
        Include('"rocfft_butterfly_template.h"'),
        Include('"real2complex.h"'),
        LineBreak())

    kdevice, kglobal = stockham.stockham(**kernel.__dict__)
    # forward runtime compile flag into kglobal.meta so we can know
    # whether to put a prototype into the function pool
    kglobal.meta = NS(**kglobal.meta.__dict__, runtime_compile=kernel.runtime_compile)
    length = kglobal.meta.length
    forward, inverse = kglobal.name, kglobal.name.replace('forward', 'inverse')
    if not kernel.runtime_compile:
        src += stockham.make_variants(kdevice, kglobal)

    cpu_functions = []
    for p in precisions:
        if kglobal.meta.scheme == 'CS_KERNEL_STOCKHAM':
            prototype = POWX_SMALL_GENERATOR(f'rocfft_internal_dfn_{p}_ci_ci_stoc_{length}',
                                             'ip_' + forward, 'ip_' + inverse,
                                             'op_' + forward, 'op_' + inverse, typename_dict[p])
            src += prototype
            cpu_functions.append(prototype.function(kglobal.meta, p))

        elif kglobal.meta.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_CC':
            prototype = POWX_LARGE_SBCC_GENERATOR(f'rocfft_internal_dfn_{p}_ci_ci_sbcc_{length}',
                                                  'ip_' + forward, 'ip_' + inverse,
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p])
            src += prototype
            cpu_functions.append(prototype.function(kglobal.meta, p))

        elif kglobal.meta.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_CR':
            prototype = POWX_LARGE_SBCR_GENERATOR(f'rocfft_internal_dfn_{p}_ci_ci_sbcr_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p])

            src += prototype
            cpu_functions.append(prototype.function(kglobal.meta, p))

        elif kglobal.meta.scheme == 'CS_KERNEL_2D_SINGLE':
            prototype = POWX_SMALL_GENERATOR(f'rocfft_internal_dfn_{p}_ci_ci_2D_{length[0]}_{length[1]}',
                                             'ip_' + forward, 'ip_' + inverse,
                                             'op_' + forward, 'op_' + inverse, typename_dict[p])
            src += prototype
            cpu_functions.append(prototype.function(kglobal.meta, p))

        elif kglobal.meta.scheme == 'CS_KERNEL_STOCKHAM_BLOCK_RC':
            # SBRC_2D
            sbrc_type, transpose_type, meta = 'SBRC_2D', 'TILE_ALIGNED', deepcopy(kglobal.meta)
            prototype = POWX_LARGE_SBRC_GENERATOR(f'rocfft_internal_dfn_{p}_op_ci_ci_sbrc_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p],
                                                  sbrc_type, transpose_type)
            src += prototype
            cpu_functions.append(prototype.function(meta, p))

            # SBRC_3D_FFT_TRANS_XY_Z
            sbrc_type, transpose_type, meta = 'SBRC_3D_FFT_TRANS_XY_Z', 'TILE_ALIGNED', deepcopy(kglobal.meta)
            prototype = POWX_LARGE_SBRC_GENERATOR(f'rocfft_internal_dfn_{p}_op_ci_ci_sbrc3d_fft_trans_xy_z_tile_aligned_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p],
                                                  sbrc_type, transpose_type)
            src += prototype
            meta.scheme, meta.transpose = 'CS_KERNEL_STOCKHAM_TRANSPOSE_XY_Z', 'TILE_ALIGNED'
            cpu_functions.append(prototype.function(meta, p))

            sbrc_type, transpose_type, meta = 'SBRC_3D_FFT_TRANS_XY_Z', 'DIAGONAL', deepcopy(kglobal.meta)
            prototype = POWX_LARGE_SBRC_GENERATOR(f'rocfft_internal_dfn_{p}_op_ci_ci_sbrc3d_fft_trans_xy_z_diagonal_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p],
                                                  sbrc_type, transpose_type)
            src += prototype
            meta.scheme, meta.transpose = 'CS_KERNEL_STOCKHAM_TRANSPOSE_XY_Z', 'DIAGONAL'
            cpu_functions.append(prototype.function(meta, p))

            # SBRC_3D_FFT_TRANS_Z_XY
            sbrc_type, transpose_type, meta = 'SBRC_3D_FFT_TRANS_Z_XY', 'TILE_ALIGNED', deepcopy(kglobal.meta)
            prototype = POWX_LARGE_SBRC_GENERATOR(f'rocfft_internal_dfn_{p}_op_ci_ci_sbrc3d_fft_trans_z_xy_tile_aligned_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p],
                                                  sbrc_type, transpose_type)
            src += prototype
            meta.scheme, meta.transpose = 'CS_KERNEL_STOCKHAM_TRANSPOSE_Z_XY', 'TILE_ALIGNED'
            cpu_functions.append(prototype.function(meta, p))


            # SBRC_3D_FFT_TRANS_Z_XY
            sbrc_type, transpose_type, meta = 'SBRC_3D_FFT_ERC_TRANS_Z_XY', 'TILE_ALIGNED', deepcopy(kglobal.meta)
            prototype = POWX_LARGE_SBRC_GENERATOR(f'rocfft_internal_dfn_{p}_op_ci_ci_sbrc3d_fft_erc_trans_z_xy_tile_aligned_{length}',
                                                  'op_' + forward, 'op_' + inverse, typename_dict[p],
                                                  sbrc_type, transpose_type)
            src += prototype
            meta.scheme, meta.transpose = 'CS_KERNEL_STOCKHAM_R_TO_CMPLX_TRANSPOSE_Z_XY', 'TILE_ALIGNED'
            cpu_functions.append(prototype.function(meta, p))

        else:
            raise NotImplementedError(f'Unable to generate host functions for scheme {kglobal.meta.scheme}.')

    if not kernel.runtime_compile:
        write(kernel_file_name(kernel), src, format=False)
    return cpu_functions


def generate_new_kernels(kernels, precisions):
    """Generate and write kernels from the kernel list.

    Entries in the kernel list are simple namespaces.  These are
    passed as keyword arguments to the Stockham generator.

    A list of CPU functions is returned.
    """
    return flatten([generate_kernel(k, precisions) for k in kernels])


def cli():
    """Command line interface..."""
    parser = argparse.ArgumentParser(prog='kernel-generator')
    subparsers = parser.add_subparsers(dest='command')
    parser.add_argument('--groups', type=int, help='Numer of small kernel groups.', default=150)
    parser.add_argument('--pattern', type=str, help='Kernel pattern to generate.', default='all')
    parser.add_argument('--precision', type=str, help='Precision to generate.', default='all')
    parser.add_argument('--manual-small', type=str, help='Small kernel sizes to generate.')
    parser.add_argument('--manual-large', type=str, help='Large kernel sizes to generate.')
    parser.add_argument('--runtime-compile', type=str, help='Allow runtime-compiled kernels.')

    list_parser = subparsers.add_parser('list', help='List kernel files that will be generated.')

    generate_parser = subparsers.add_parser('generate', help='Generate kernels.')
    generate_parser.add_argument('generator', type=str, help='Kernel generator executable.')

    args = parser.parse_args()

    #
    # which kernels to build? set the flags for generate before modifying patterns
    #
    patterns = args.pattern.split(',')
    large = 'all' in patterns or 'large' in patterns
    small = 'all' in patterns or 'small' in patterns
    dim2  = 'all' in patterns or '2D' in patterns
    pow2  = small or 'pow2' in patterns
    pow3  = small or 'pow3' in patterns
    pow5  = small or 'pow5' in patterns
    pow7  = small or 'pow7' in patterns

    if patterns == ['none']:
        patterns = []
    if args.manual_small:
        patterns += ['small']
    if args.manual_large:
        patterns += ['large']
    # TODO- if dim2, pattern += small as well

    replacements = {
        'pow2': 'small',
        'pow3': 'small',
        'pow5': 'small',
        'pow7': 'small',
    }

    patterns = [replacements.get(key, key) for key in patterns if key != 'none']
    if 'all' in patterns:
        patterns += ['small']
        patterns += ['large']
        patterns += ['2D']
    patterns = set(patterns)

    #
    # which precicions to build?
    #
    precisions = args.precision.split(',')

    replacements = {
        'single': 'sp',
        'double': 'dp',
    }

    precisions = [replacements.get(key, key) for key in precisions if key != 'none']
    if 'all' in precisions:
        precisions = ['sp', 'dp']
    precisions = set(precisions)


    #
    # list all the exact sizes of kernels to build
    #
    manual_small = None
    if args.manual_small:
        manual_small = product(map(int, args.manual_small.split(',')),
                               ['CS_KERNEL_STOCKHAM'])

    manual_large = None
    if args.manual_large:
        manual_large = product(map(int, args.manual_large.split(',')),
                               ['CS_KERNEL_STOCKHAM_BLOCK_CC', 'CS_KERNEL_STOCKHAM_BLOCK_RC'])

    # all kernels to be generated from arguments
    expand_sizes = {
        'small': { 'sp': [], 'dp': [] },
        'large': { 'sp': [], 'dp': [] },
    }

    if small or pow2 or pow3 or pow5 or pow7:
        for p in precisions:
            expand_sizes['small'][p] = merge(expand_sizes['small'][p], supported_small_sizes(p, pow2, pow3, pow5, pow7))
    if manual_small:
        for p in precisions:
            expand_sizes['small'][p] = merge(expand_sizes['small'][p], manual_small)
    if large:
        for p in precisions:
            expand_sizes['large'][p] = merge(expand_sizes['large'][p], supported_large_sizes(p))
    if manual_large:
        for p in precisions:
            expand_sizes['large'][p] = merge(expand_sizes['large'][p], manual_large)

    #
    # which kernels by new-gen and which by old-gen? categorize input kernels
    #
    supported_new_small_kernels = list_new_kernels()
    supported_new_large_kernels = list_new_large_kernels()
    new_small_kernels = new_large_kernels = []

    # Don't subtract_from_all for large, since so far sbrc and transpose still rely on old-gen.
    for p in precisions:
        expand_sizes['small'][p], new_smalls = pick(expand_sizes['small'][p], supported_new_small_kernels)
        expand_sizes['large'][p], new_larges = pick(expand_sizes['large'][p], supported_new_large_kernels, subtract_from_all=False)
        # remove the unsupported large sizes in old_gen from arguments
        for length in list(expand_sizes['large'][p]):
            if length not in old_gen_supported_large:
                del expand_sizes['large'][p][length]
        # remove the unsupported small sizes in old_gen from arguments
        old_gen_supported_small = [item[0] for item in supported_small_sizes(p)]
        for length in list(expand_sizes['small'][p]):
            if length not in old_gen_supported_small:
                del expand_sizes['small'][p][length]
        new_small_kernels = merge_length(new_small_kernels, new_smalls)
        new_large_kernels = merge_length(new_large_kernels, new_larges)

    new_kernels = new_small_kernels + new_large_kernels
    if dim2:
        new_kernels += list_new_2d_kernels()
    # set runtime_compile on new kernels that haven't already set a
    # value
    new_kernels = default_runtime_compile(new_kernels)

    if args.runtime_compile != 'ON':
        for k in new_kernels:
            k.runtime_compile = False

    # update the patterns after removing new kernels from old generator to avoid including some missing cpp
    if 'small' in patterns and len(expand_sizes['small']['sp']) == 0 and len(expand_sizes['small']['dp']) == 0:
        patterns.remove('small')
    if 'large' in patterns and len(expand_sizes['large']['sp']) == 0 and len(expand_sizes['large']['dp']) == 0:
        patterns.remove('large')

    #
    # return the necessary include files to cmake
    #
    if args.command == 'list':

        scprint(set(list_old_generated_kernels(patterns=patterns,
                                           precisions=precisions,
                                           num_small_kernel_groups=args.groups)
                    + list_generated_kernels(new_kernels)))
        return

    if args.command == 'generate':

        # collection of Functions to generate prototypes for
        psmall, plarge, p2d = {}, {}, {}

        # already excludes small and large-1D from new-generators
        for p in precisions:
            psmall = pmerge(psmall, generate_small_1d_prototypes(p, expand_sizes['small'][p]))
            plarge = pmerge(plarge, generate_large_1d_prototypes(p, expand_sizes['large'][p]))

        if dim2:
            for p in precisions:
                transform_2D = merge([], supported_2d_sizes(p))
                p2d = pmerge(p2d, generate_2d_prototypes(p, transform_2D))

        # hijack a few new kernels...
        pnew = pmerge({}, generate_new_kernels(new_kernels, precisions))

        cpu_functions = list(merge(psmall, plarge, p2d, pnew).values())
        write('function_pool.cpp', generate_cpu_function_pool(cpu_functions), format=True)

        old_small_lengths = {f.meta.length for f in psmall.values()}
        old_large_lengths = {f.meta.length for f in plarge.values()} # sbcc=new-gen, sbrc/transpose=old-gen
        new_large_lengths = {k.length for k in new_large_kernels} # sbcc by new-gen

        if old_small_lengths:
            subprocess.run([args.generator, '-g', str(args.groups), '-p', args.precision, '-t', 'none', '--manual-small', cjoin(sorted(old_small_lengths))], check=True)
        if old_large_lengths:
            if new_large_lengths:
                subprocess.run([args.generator, '-g', str(args.groups), '-p', args.precision, '-t', 'none', '--manual-large', cjoin(sorted(old_large_lengths)), '--no-sbcc', cjoin(sorted(new_large_lengths))], check=True)
            else:
                subprocess.run([args.generator, '-g', str(args.groups), '-p', args.precision, '-t', 'none', '--manual-large', cjoin(sorted(old_large_lengths))], check=True)


if __name__ == '__main__':
    cli()

