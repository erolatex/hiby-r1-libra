/* Software attenuation for S16 sources, after any time stretching.
 * Keep fractional gain until the final PCM conversion. S24_LE uses the
 * low 24 bits of a 32-bit container; S32_LE uses all 32 bits. */
#ifndef PCM_GAIN_H
#define PCM_GAIN_H

#include <stdint.h>

#define PCM_GAIN_ONE INT32_C(16777216) /* Q24 */

static int32_t pcm_gain_for_volume(int volume) {
    if (volume <= 0) return 0;
    if (volume >= 100) return PCM_GAIN_ONE;
    return (int32_t)(((int64_t)volume * volume * PCM_GAIN_ONE + 5000) / 10000);
}

/* Worker-local PRNG: never share rand()'s state with another subsystem. */
static uint32_t pcm_gain_random(uint32_t *state) {
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

/* Symmetric rounding, without an implementation-defined signed right shift. */
static int32_t pcm_gain_round(int64_t value, unsigned shift) {
    int64_t half = INT64_C(1) << (shift - 1);
    return value < 0 ? (int32_t)(-((-value + half) >> shift))
                     : (int32_t)((value + half) >> shift);
}

/* bits is the negotiated output depth: 16, 24 or 32. Full volume is an
 * exact promotion, mute is digital zero. Only a 16-bit attenuated output
 * needs TPDF dither here; wider output retains the fractional S16 samples. */
static int32_t pcm_gain_s16(int16_t sample, int32_t gain, unsigned bits,
                            uint32_t *random_state) {
    if (!gain) return 0;
    if (gain == PCM_GAIN_ONE)
        return (int32_t)((int64_t)sample * (INT64_C(1) << (bits - 16)));
    int64_t value = (int64_t)sample * gain;
    unsigned shift = 40 - bits;
    if (bits == 16) {
        int32_t a = (int32_t)(pcm_gain_random(random_state) & UINT32_C(0xffffff));
        int32_t b = (int32_t)(pcm_gain_random(random_state) & UINT32_C(0xffffff));
        value += (int64_t)a - b; /* triangular, +/- one output LSB */
    }
    return pcm_gain_round(value, shift);
}

#endif
