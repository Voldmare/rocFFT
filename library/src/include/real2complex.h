// Copyright (c) 2016 - present Advanced Micro Devices, Inc. All rights reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#ifndef REAL_TO_COMPLEX_H
#define REAL_TO_COMPLEX_H

ROCFFT_DEVICE_EXPORT void real2complex(const void* data, void* back);
ROCFFT_DEVICE_EXPORT void complex2hermitian(const void* data, void* back);

ROCFFT_DEVICE_EXPORT void hermitian2complex(const void* data, void* back);
ROCFFT_DEVICE_EXPORT void complex2real(const void* data, void* back);

ROCFFT_DEVICE_EXPORT void r2c_1d_post(const void* data_p, void*);
ROCFFT_DEVICE_EXPORT void r2c_1d_post_transpose(const void* data, void* back);
ROCFFT_DEVICE_EXPORT void c2r_1d_pre(const void* data_p, void*);
ROCFFT_DEVICE_EXPORT void transpose_c2r_1d_pre(const void* data, void* back);

ROCFFT_DEVICE_EXPORT void apply_real_callback(const void* data, void* back);

#endif // REAL_TO_COMPLEX_H
