#!/usr/bin/env python3
"""Host regression checks; no player, ALSA, decoder headers or SD card needed.

Compiles the production gain header and actual write_s16_frames function
extracted from audio.c, replacing only the ALSA writer with a capture stub.
"""
from pathlib import Path
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "music-app/app/audio.c").read_text()
start = source.index("static void *write_s16_frames(")
end = source.index("\n/* The nice -8", start)
wrapper = source[start:end]

HARNESS = r'''
#include <assert.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "pcm_gain.h"

typedef unsigned long snd_pcm_uframes_t;
#define FMT_S16_LE 2
#define FMT_S24_LE 6
#define FMT_S32_LE 10
static int g_out_fmt, channels, calls, fail_at;
static size_t written;
static int32_t capture[24000];
static void *write_pcm_frames(void *pcm, const char *p,
                              snd_pcm_uframes_t frames, size_t frame_bytes) {
    calls++;
    if (fail_at && calls == fail_at) return NULL;
    int wide = g_out_fmt != FMT_S16_LE;
    assert(frame_bytes == (size_t)channels * (wide ? 4 : 2));
    size_t n = frames * (size_t)channels;
    assert(written + n <= sizeof(capture)/sizeof(capture[0]));
    for (size_t i = 0; i < n; i++) {
        if (wide) memcpy(&capture[written++], p + i * 4, 4);
        else {
            int16_t v;
            memcpy(&v, p + i * 2, 2);
            capture[written++] = v;
        }
    }
    return pcm;
}
'''

CHECKS = r'''
static void numeric_checks(void) {
    uint32_t rng = 0x12345678;
    int32_t prev = -1;
    for (int v = 0; v <= 100; v++) {
        int32_t gain = pcm_gain_for_volume(v);
        assert(gain > prev);
        prev = gain;
        for (unsigned bits = 16; bits <= 32; bits += 8) {
            double promotion = (double)(UINT32_C(1) << (bits - 16));
            for (int s = -32768; s <= 32767; s++) {
                int32_t out = pcm_gain_s16((int16_t)s, gain, bits, &rng);
                double expected = s * promotion * v * v / 10000.0;
                double error_bound = (bits == 16 && v > 0 && v < 100 ? 1.5 : 0.5)
                                   + 32768.0 * promotion / (2.0 * PCM_GAIN_ONE);
                assert(fabs(out - expected) <= error_bound + 1e-6);
                if (v == 0) assert(out == 0);
                if (v == 100) assert(out == (int32_t)(s * promotion));
                if (bits == 16) assert(out >= -32768 && out <= 32767);
                if (bits == 24) assert(out >= -8388608 && out <= 8388607);
            }
        }
    }
    assert(pcm_gain_for_volume(-1) == 0);
    assert(pcm_gain_for_volume(101) == PCM_GAIN_ONE);
    /* At 10%, the intended squared gain is .01, not the old 2/256. */
    assert(fabs(pcm_gain_for_volume(10) / (double)PCM_GAIN_ONE - .01) < 3e-8);

    /* TPDF removes signal-correlated truncation and its negative DC bias. */
    for (int sample = -123; sample <= 123; sample += 123) {
        double sum = 0, sumsq = 0;
        const int n = 200000;
        double target = sample * pcm_gain_for_volume(10) / (double)PCM_GAIN_ONE;
        for (int i = 0; i < n; i++) {
            double e = pcm_gain_s16((int16_t)sample, pcm_gain_for_volume(10), 16, &rng) - target;
            sum += e; sumsq += e*e;
        }
        assert(fabs(sum / n) < .01);
        assert(sumsq / n > .23 && sumsq / n < .27);
    }
    puts("PASS: all S16 values, volumes 0..100, S16/S24/S32 ranges and precision");
    puts("PASS: unity, mute, gain curve, TPDF mean and noise power");
}

static void write_checks(void) {
    static short input[20000], original[20000];
    for (int i = 0; i < 20000; i++) input[i] = (short)((i * 47) % 65536 - 32768);
    memcpy(original, input, sizeof(input));
    const int formats[] = {FMT_S16_LE, FMT_S24_LE, FMT_S32_LE};
    const int volumes[] = {0, 10, 99, 100};
    int token;
    for (int f = 0; f < 3; f++) {
        g_out_fmt = formats[f];
        unsigned bits = f == 0 ? 16 : f == 1 ? 24 : 32;
        for (channels = 1; channels <= 4; channels *= 2) {
            for (int j = 0; j < 4; j++) {
                written = 0; calls = 0; fail_at = 0;
                int v = volumes[j];
                assert(write_s16_frames(&token, input, 5000, channels, v) == &token);
                assert(written == (size_t)5000 * channels);
                for (size_t i = 0; i < written; i++) {
                    double target = input[i] * (double)(UINT32_C(1) << (bits - 16))
                                  * pcm_gain_for_volume(v) / PCM_GAIN_ONE;
                    assert(fabs(capture[i] - target) <= (bits == 16 ? 1.500001 : .500001));
                    if (!v) assert(capture[i] == 0);
                }
                assert(memcmp(input, original, sizeof(input)) == 0);
                if (bits > 16 || v != 100)
                    assert(calls == (5000 + 8192/channels - 1)/(8192/channels));
            }
        }
    }
    channels = 2; g_out_fmt = FMT_S24_LE;
    written = 0; calls = 0; fail_at = 1;
    assert(write_s16_frames(&token, input, 5000, channels, 10) == NULL);
    assert(calls == 1 && written == 0);
    puts("PASS: production writer packing, interleaving, chunking and failure propagation");
}
int main(void) { numeric_checks(); write_checks(); return 0; }
'''

with tempfile.TemporaryDirectory(prefix="libra-gain-test-") as temp:
    folder = Path(temp)
    cfile = folder / "test.c"
    exe = folder / "test"
    cfile.write_text(HARNESS + wrapper + CHECKS)
    command = shlex.split(os.environ.get("CC", "cc"))
    flags = shlex.split(os.environ.get("TEST_CFLAGS", "-O2"))
    subprocess.run(command + ["-std=c99", "-Wall", "-Wextra", "-Werror"] + flags
                   + ["-I", str(ROOT / "music-app/app"), str(cfile), "-lm", "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
