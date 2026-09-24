from __future__ import annotations

from array import array
import math
import sys

SQRT_HALF = 0.7071067811865476

# Lookup table for a 0 dB-centre balance pan law.  The louder side remains
# at unity while the opposite side is progressively attenuated.  This avoids
# the audible ~3 dB jump that occurred when mono speech was recreated as a
# stereo stream and immediately entered the old equal-power (-3 dB centre) law.
_PAN_GAIN_TABLE_SIZE = 2048
_PAN_GAIN_TABLE = tuple(
    (
        1.0 if i <= (_PAN_GAIN_TABLE_SIZE // 2) else 2.0 * (1.0 - i / _PAN_GAIN_TABLE_SIZE),
        2.0 * (i / _PAN_GAIN_TABLE_SIZE) if i < (_PAN_GAIN_TABLE_SIZE // 2) else 1.0,
    )
    for i in range(_PAN_GAIN_TABLE_SIZE + 1)
)
_SINE_TABLE_SIZE = 4096
_SINE_TABLE = tuple(math.sin(2.0 * math.pi * i / _SINE_TABLE_SIZE) for i in range(_SINE_TABLE_SIZE))


def _clamp(value, minimum, maximum):
    return minimum if value < minimum else maximum if value > maximum else value


def _safe_frequency(freq, sample_rate):
    nyquist = sample_rate * 0.5
    return _clamp(float(freq), 1.0, max(1.0, nyquist * 0.98))


def _normalize(b0, b1, b2, a0, a1, a2):
    inv = 1.0 / a0
    return b0 * inv, b1 * inv, b2 * inv, a1 * inv, a2 * inv


def lowpass_coefficients(freq, sample_rate, q=SQRT_HALF):
    freq = _safe_frequency(freq, sample_rate)
    omega = 2.0 * math.pi * freq / sample_rate
    cosw = math.cos(omega)
    sinw = math.sin(omega)
    alpha = sinw / (2.0 * max(0.001, q))
    b0 = (1.0 - cosw) * 0.5
    b1 = 1.0 - cosw
    b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cosw
    a2 = 1.0 - alpha
    return _normalize(b0, b1, b2, a0, a1, a2)


def highpass_coefficients(freq, sample_rate, q=SQRT_HALF):
    freq = _safe_frequency(freq, sample_rate)
    omega = 2.0 * math.pi * freq / sample_rate
    cosw = math.cos(omega)
    sinw = math.sin(omega)
    alpha = sinw / (2.0 * max(0.001, q))
    b0 = (1.0 + cosw) * 0.5
    b1 = -(1.0 + cosw)
    b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cosw
    a2 = 1.0 - alpha
    return _normalize(b0, b1, b2, a0, a1, a2)


def resonance_to_q(amount):
    """Map the accessible 0..100 Resonance control to a stable biquad Q.

    0 preserves the historical Butterworth value exactly; the exponential curve
    reaches Q=12 at 100 so most of the useful range remains controllable while
    still allowing a clearly resonant Low/High Pass sound.
    """
    amount = _clamp(float(amount), 0.0, 100.0) / 100.0
    return SQRT_HALF * ((12.0 / SQRT_HALF) ** amount)


def peaking_coefficients(freq, gain_db, q, sample_rate):
    freq = _safe_frequency(freq, sample_rate)
    q = _clamp(float(q), 0.05, 50.0)
    gain_db = _clamp(float(gain_db), -36.0, 36.0)
    a = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * freq / sample_rate
    cosw = math.cos(omega)
    sinw = math.sin(omega)
    alpha = sinw / (2.0 * q)
    b0 = 1.0 + alpha * a
    b1 = -2.0 * cosw
    b2 = 1.0 - alpha * a
    a0 = 1.0 + alpha / a
    a1 = -2.0 * cosw
    a2 = 1.0 - alpha / a
    return _normalize(b0, b1, b2, a0, a1, a2)


class BiquadStage:
    __slots__ = ("b0", "b1", "b2", "a1", "a2", "states")

    def __init__(self, coeffs, channels):
        self.b0, self.b1, self.b2, self.a1, self.a2 = coeffs
        self.states = [[0.0, 0.0, 0.0, 0.0] for _ in range(channels)]

    def set_coefficients(self, coeffs):
        self.b0, self.b1, self.b2, self.a1, self.a2 = coeffs

    def process(self, sample, channel):
        st = self.states[channel]
        x1, x2, y1, y2 = st
        y = self.b0 * sample + self.b1 * x1 + self.b2 * x2 - self.a1 * y1 - self.a2 * y2
        st[1] = x1
        st[0] = sample
        st[3] = y1
        st[2] = y
        return y


class StereoChorus:
    """Low-CPU Juno-inspired stereo chorus with BBD-style wet-path softening."""

    __slots__ = (
        "sample_rate", "mix", "depth", "rate", "width", "mode", "buffer_l", "buffer_r", "size", "index",
        "phase1", "phase2", "wet_lpf_l", "wet_lpf_r", "wet_lpf_coeff",
    )

    def __init__(self, sample_rate, settings):
        self.sample_rate = float(sample_rate)
        self.mix = _clamp(float(settings.get("chorusMix", 35.0)), 0.0, 100.0) / 100.0
        self.depth = _clamp(float(settings.get("chorusDepth", 60.0)), 0.0, 100.0) / 100.0
        # Chorus Rate is a musical 0-100 control. 50 preserves the exact TEST38
        # modulation rates; the range is exponential from 1/4x at 0 to 4x at 100.
        rate_control = _clamp(float(settings.get("chorusRate", 50.0)), 0.0, 100.0)
        self.rate = 4.0 ** ((rate_control - 50.0) / 50.0)
        self.width = _clamp(float(settings.get("chorusWidth", 100.0)), 0.0, 100.0) / 100.0
        self.mode = str(settings.get("chorusMode", "I"))
        self.size = max(16, int(self.sample_rate * 0.040) + 8)
        self.buffer_l = [0.0] * self.size
        self.buffer_r = [0.0] * self.size
        self.index = 0
        self.phase1 = 0.0
        self.phase2 = math.pi * 0.37
        self.wet_lpf_l = 0.0
        self.wet_lpf_r = 0.0
        # A real Juno chorus uses BBD delay lines with limited bandwidth.
        # A single-pole low-pass gives a subtle version of that colour at very low CPU cost.
        cutoff = min(12000.0, self.sample_rate * 0.42)
        self.wet_lpf_coeff = math.exp(-2.0 * math.pi * cutoff / self.sample_rate)

    def _read(self, buf, delay_samples):
        pos = self.index - delay_samples
        while pos < 0.0:
            pos += self.size
        base = math.floor(pos)
        i0 = int(base) % self.size
        frac = pos - base
        # Fractional delay interpolation must move toward the newer sample.
        # TEST17 used i0 - 1, which interpolated in the wrong direction.
        i1 = (i0 + 1) % self.size
        return buf[i0] * (1.0 - frac) + buf[i1] * frac

    def _tap(self, buf, phase, base_ms, mod_ms):
        delay = (base_ms + mod_ms * (0.5 + 0.5 * math.sin(phase))) * self.sample_rate * 0.001
        return self._read(buf, delay)

    def _soften_wet(self, wet_l, wet_r):
        coeff = self.wet_lpf_coeff
        one_minus = 1.0 - coeff
        self.wet_lpf_l = wet_l * one_minus + self.wet_lpf_l * coeff
        self.wet_lpf_r = wet_r * one_minus + self.wet_lpf_r * coeff
        return self.wet_lpf_l, self.wet_lpf_r

    def process_wet(self, left, right):
        """Return the 100% wet chorus signal for true aux/send operation."""
        if self.depth <= 0.0:
            return 0.0, 0.0

        # This is intentionally written as one hot path rather than routing every
        # fractional-delay tap through _tap() -> _read() and the wet filter
        # through _soften_wet(). Chorus is evaluated once per stereo frame, so
        # those tiny Python calls are disproportionately expensive. Keep the
        # arithmetic expressions and update order identical to TEST43.
        index = self.index
        size = self.size
        sample_rate = self.sample_rate
        buffer_l = self.buffer_l
        buffer_r = self.buffer_r
        buffer_l[index] = left
        buffer_r[index] = right

        d = self.depth
        rate_scale = self.rate
        mode = self.mode
        phase1 = self.phase1

        if mode == "II":
            rate1, base1, mod1 = 0.86 * rate_scale, 6.8, 3.8 * d

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_l = buffer_l[i0] * (1.0 - frac) + buffer_l[i1] * frac

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1 + math.pi))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_r = buffer_r[i0] * (1.0 - frac) + buffer_r[i1] * frac

        elif mode == "I+II":
            rate1, base1, mod1 = 0.52 * rate_scale, 5.8, 2.1 * d
            rate2, base2, mod2 = 0.86 * rate_scale, 7.2, 3.8 * d
            phase2 = self.phase2

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_l1 = buffer_l[i0] * (1.0 - frac) + buffer_l[i1] * frac

            delay = (base2 + mod2 * (0.5 + 0.5 * math.sin(phase2))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_l2 = buffer_l[i0] * (1.0 - frac) + buffer_l[i1] * frac
            wet_l = 0.5 * (wet_l1 + wet_l2)

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1 + math.pi))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_r1 = buffer_r[i0] * (1.0 - frac) + buffer_r[i1] * frac

            delay = (base2 + mod2 * (0.5 + 0.5 * math.sin(phase2 + math.pi))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_r2 = buffer_r[i0] * (1.0 - frac) + buffer_r[i1] * frac
            wet_r = 0.5 * (wet_r1 + wet_r2)

            self.phase2 = phase2 + 2.0 * math.pi * rate2 / sample_rate
            if self.phase2 >= 2.0 * math.pi:
                self.phase2 -= 2.0 * math.pi

        else:
            rate1, base1, mod1 = 0.52 * rate_scale, 5.8, 2.1 * d

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_l = buffer_l[i0] * (1.0 - frac) + buffer_l[i1] * frac

            delay = (base1 + mod1 * (0.5 + 0.5 * math.sin(phase1 + math.pi))) * sample_rate * 0.001
            pos = index - delay
            while pos < 0.0:
                pos += size
            base = math.floor(pos)
            i0 = int(base) % size
            frac = pos - base
            i1 = (i0 + 1) % size
            wet_r = buffer_r[i0] * (1.0 - frac) + buffer_r[i1] * frac

        coeff = self.wet_lpf_coeff
        one_minus = 1.0 - coeff
        self.wet_lpf_l = wet_l * one_minus + self.wet_lpf_l * coeff
        self.wet_lpf_r = wet_r * one_minus + self.wet_lpf_r * coeff
        wet_l, wet_r = self.wet_lpf_l, self.wet_lpf_r

        width = self.width
        if width < 1.0:
            mid = 0.5 * (wet_l + wet_r)
            wet_l = mid + (wet_l - mid) * width
            wet_r = mid + (wet_r - mid) * width

        self.phase1 = phase1 + 2.0 * math.pi * rate1 / sample_rate
        if self.phase1 >= 2.0 * math.pi:
            self.phase1 -= 2.0 * math.pi
        index += 1
        if index >= size:
            index = 0
        self.index = index
        return wet_l, wet_r

    def process(self, left, right):
        """Backward-compatible insert renderer; the add-on uses process_wet()."""
        wet_l, wet_r = self.process_wet(left, right)
        mix = self.mix
        dry = 1.0 - mix
        return left * dry + wet_l * mix, right * dry + wet_r * mix


class StereoDelay:
    __slots__ = ("sample_rate", "delay_samples", "feedback", "mix", "pingpong", "buf_l", "buf_r", "size", "index")

    def __init__(self, sample_rate, settings):
        self.sample_rate = float(sample_rate)
        time_ms = _clamp(float(settings.get("delayTimeMs", 250.0)), 1.0, 1000.0)
        self.delay_samples = max(1, int(round(self.sample_rate * time_ms * 0.001)))
        self.feedback = _clamp(float(settings.get("delayFeedback", 30.0)), 0.0, 95.0) / 100.0
        self.mix = _clamp(float(settings.get("delayMix", 20.0)), 0.0, 100.0) / 100.0
        self.pingpong = _clamp(float(settings.get("delayPingPong", 100.0)), 0.0, 100.0) / 100.0
        self.size = self.delay_samples + 1
        self.buf_l = [0.0] * self.size
        self.buf_r = [0.0] * self.size
        self.index = 0

    def process_wet(self, left, right):
        """Process one frame and return the 100% wet delay signal.

        Send level is applied by StreamProcessor before this method. Keeping the
        delay line itself fully wet makes Delay Send behave as a true aux send.
        """
        delayed_l = self.buf_l[self.index]
        delayed_r = self.buf_r[self.index]
        pp = self.pingpong
        fb = self.feedback

        # Ping-pong needs asymmetric input injection as well as cross-feedback.
        # With NVDA speech, mono is promoted to identical L/R before the delay.
        # Cross-feeding two identical delay lines can never create stereo: swapping
        # L and R simply swaps equal values and every repeat stays in the centre.
        #
        # At 0% keep the original independent stereo delay exactly.  As Ping Pong
        # increases, progressively remove the direct injection into the right delay
        # line and feed the mono sum into the left line.  At 100% the first repeat
        # is left, then cross-feedback makes the following repeats alternate
        # right -> left -> right.
        feedback_l = delayed_l * (1.0 - pp) + delayed_r * pp
        feedback_r = delayed_r * (1.0 - pp) + delayed_l * pp
        if pp <= 1e-12:
            input_l = left
            input_r = right
        else:
            mono_input = 0.5 * (left + right)
            input_l = left * (1.0 - pp) + mono_input * pp
            input_r = right * (1.0 - pp)
        self.buf_l[self.index] = input_l + feedback_l * fb
        self.buf_r[self.index] = input_r + feedback_r * fb
        self.index += 1
        if self.index >= self.size:
            self.index = 0
        return delayed_l, delayed_r

    def process(self, left, right):
        """Backward-compatible insert rendering; the add-on send path uses process_wet()."""
        wet_l, wet_r = self.process_wet(left, right)
        mix = self.mix
        dry = 1.0 - mix
        return left * dry + wet_l * mix, right * dry + wet_r * mix


class _Comb:
    __slots__ = ("buffer", "index", "feedback", "damp", "store")

    def __init__(self, length, feedback, damp):
        self.buffer = [0.0] * max(2, int(length))
        self.index = 0
        self.feedback = feedback
        self.damp = damp
        self.store = 0.0

    def process(self, value):
        out = self.buffer[self.index]
        self.store = out * (1.0 - self.damp) + self.store * self.damp
        self.buffer[self.index] = value + self.store * self.feedback
        self.index += 1
        if self.index >= len(self.buffer):
            self.index = 0
        return out


class _AllPass:
    __slots__ = ("buffer", "index", "feedback")

    def __init__(self, length, feedback=0.5):
        self.buffer = [0.0] * max(2, int(length))
        self.index = 0
        self.feedback = feedback

    def process(self, value):
        buf = self.buffer[self.index]
        out = -value + buf
        self.buffer[self.index] = value + buf * self.feedback
        self.index += 1
        if self.index >= len(self.buffer):
            self.index = 0
        return out


class StereoReverb:
    """Small Schroeder-style stereo reverb tuned for low CPU and screen-reader use."""

    __slots__ = ("mix", "combs_l", "combs_r", "ap_l", "ap_r")

    def __init__(self, sample_rate, settings):
        sr = float(sample_rate)
        self.mix = _clamp(float(settings.get("reverbMix", 15.0)), 0.0, 100.0) / 100.0
        size = _clamp(float(settings.get("reverbSize", 50.0)), 0.0, 100.0) / 100.0
        decay = _clamp(float(settings.get("reverbDecaySec", 1.2)), 0.1, 5.0)
        damping = _clamp(float(settings.get("reverbDamping", 50.0)), 0.0, 100.0) / 100.0
        scale = 0.72 + size * 0.62
        base_l = (0.0297, 0.0371, 0.0411)
        base_r = (0.0309, 0.0383, 0.0437)
        damp = damping * 0.92

        def make_combs(times):
            result = []
            for t in times:
                delay_sec = t * scale
                feedback = 10.0 ** (-3.0 * delay_sec / decay)
                result.append(_Comb(int(sr * delay_sec), min(0.985, feedback), damp))
            return result

        self.combs_l = make_combs(base_l)
        self.combs_r = make_combs(base_r)
        self.ap_l = _AllPass(int(sr * (0.0050 + 0.0020 * size)), 0.5)
        self.ap_r = _AllPass(int(sr * (0.0057 + 0.0022 * size)), 0.5)

    def process_wet(self, left, right):
        """Process one frame and return only the room response (100% wet).

        TEST43 keeps the exact TEST42 Schroeder network and arithmetic order, but
        executes the six combs and two all-pass stages inline.  These eight tiny
        Python method calls happened for every stereo frame and were a measurable
        hot spot.  The buffers, feedback, damping and update order are unchanged.
        """
        # Reverb follows the source pan only partially. A 45% cross-feed keeps
        # the dominant side clear while leaving an audible room response on
        # the opposite side, even with a hard-panned source.
        in_l = left + right * 0.45
        in_r = right + left * 0.45

        c0, c1, c2 = self.combs_l
        buf = c0.buffer
        idx = c0.index
        out0 = buf[idx]
        store = out0 * (1.0 - c0.damp) + c0.store * c0.damp
        buf[idx] = in_l + store * c0.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c0.index = idx
        c0.store = store

        buf = c1.buffer
        idx = c1.index
        out1 = buf[idx]
        store = out1 * (1.0 - c1.damp) + c1.store * c1.damp
        buf[idx] = in_l + store * c1.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c1.index = idx
        c1.store = store

        buf = c2.buffer
        idx = c2.index
        out2 = buf[idx]
        store = out2 * (1.0 - c2.damp) + c2.store * c2.damp
        buf[idx] = in_l + store * c2.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c2.index = idx
        c2.store = store
        wet_l = out0 + out1 + out2

        c0, c1, c2 = self.combs_r
        buf = c0.buffer
        idx = c0.index
        out0 = buf[idx]
        store = out0 * (1.0 - c0.damp) + c0.store * c0.damp
        buf[idx] = in_r + store * c0.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c0.index = idx
        c0.store = store

        buf = c1.buffer
        idx = c1.index
        out1 = buf[idx]
        store = out1 * (1.0 - c1.damp) + c1.store * c1.damp
        buf[idx] = in_r + store * c1.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c1.index = idx
        c1.store = store

        buf = c2.buffer
        idx = c2.index
        out2 = buf[idx]
        store = out2 * (1.0 - c2.damp) + c2.store * c2.damp
        buf[idx] = in_r + store * c2.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        c2.index = idx
        c2.store = store
        wet_r = out0 + out1 + out2

        value = wet_l / 3
        ap = self.ap_l
        buf = ap.buffer
        idx = ap.index
        buf_value = buf[idx]
        wet_l = -value + buf_value
        buf[idx] = value + buf_value * ap.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        ap.index = idx

        value = wet_r / 3
        ap = self.ap_r
        buf = ap.buffer
        idx = ap.index
        buf_value = buf[idx]
        wet_r = -value + buf_value
        buf[idx] = value + buf_value * ap.feedback
        idx += 1
        if idx >= len(buf):
            idx = 0
        ap.index = idx
        return wet_l, wet_r

    def process(self, left, right):
        """Backward-compatible insert rendering; the add-on send path uses process_wet()."""
        if self.mix <= 0.0:
            return left, right
        wet_l, wet_r = self.process_wet(left, right)
        mix = self.mix
        dry = 1.0 - mix
        return left * dry + wet_l * mix, right * dry + wet_r * mix


def _decode_pcm(raw, bits):
    if bits == 8:
        return [b - 128 for b in raw], 127.0, -128.0
    if bits == 16:
        a = array("h")
        a.frombytes(raw[: len(raw) - (len(raw) % 2)])
        if sys.byteorder != "little":
            a.byteswap()
        return a, 32767.0, -32768.0
    if bits == 24:
        usable = len(raw) - (len(raw) % 3)
        vals = []
        append = vals.append
        for i in range(0, usable, 3):
            v = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            append(v)
        return vals, 8388607.0, -8388608.0
    if bits == 32:
        a = array("i")
        if a.itemsize != 4:
            raise RuntimeError("array('i') non e' a 32 bit")
        a.frombytes(raw[: len(raw) - (len(raw) % 4)])
        if sys.byteorder != "little":
            a.byteswap()
        return a, 2147483647.0, -2147483648.0
    raise ValueError("PCM bits non supportati")


def _encode_pcm(samples, bits, max_pos, min_neg):
    if bits == 8:
        out = bytearray(len(samples))
        for i, v in enumerate(samples):
            iv = int(round(v))
            if iv > max_pos:
                iv = int(max_pos)
            elif iv < min_neg:
                iv = int(min_neg)
            out[i] = iv + 128
        return bytes(out)
    if bits == 16:
        out = array("h")
        for v in samples:
            iv = int(round(v))
            if iv > max_pos:
                iv = int(max_pos)
            elif iv < min_neg:
                iv = int(min_neg)
            out.append(iv)
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()
    if bits == 24:
        out = bytearray()
        extend = out.extend
        for v in samples:
            iv = int(round(v))
            if iv > max_pos:
                iv = int(max_pos)
            elif iv < min_neg:
                iv = int(min_neg)
            if iv < 0:
                iv += 0x1000000
            extend((iv & 0xFF, (iv >> 8) & 0xFF, (iv >> 16) & 0xFF))
        return bytes(out)
    if bits == 32:
        out = array("i")
        for v in samples:
            iv = int(round(v))
            if iv > max_pos:
                iv = int(max_pos)
            elif iv < min_neg:
                iv = int(min_neg)
            out.append(iv)
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()
    raise ValueError("PCM bits non supportati")


class FETCompressor1176:
    """Lightweight 1176-style linked peak compressor. No lookahead, no tail thread."""

    __slots__ = (
        "sample_rate", "input_gain", "output_gain", "ratio", "threshold_ratio",
        "attack_coeff", "release_coeff", "gain",
    )

    def __init__(self, sample_rate, settings):
        self.sample_rate = float(sample_rate)
        input_db = _clamp(float(settings.get("compressorInputDb", 0.0)), -12.0, 24.0)
        output_db = _clamp(float(settings.get("compressorOutputDb", 0.0)), -24.0, 24.0)
        self.input_gain = 10.0 ** (input_db / 20.0)
        # Fixed compressor makeup restores the perceived level lost to gain
        # reduction. The existing 1176 Output control remains an additional
        # trim around this +6 dB baseline.
        makeup_db = 6.0
        self.output_gain = 10.0 ** ((output_db + makeup_db) / 20.0)
        ratio_name = str(settings.get("compressorRatio", "4:1"))
        self.ratio = {"4:1": 4.0, "8:1": 8.0, "12:1": 12.0, "20:1": 20.0, "All": 50.0}.get(ratio_name, 4.0)
        # Like the hardware, threshold is fixed and Input determines how hard it is driven.
        self.threshold_ratio = 10.0 ** (-18.0 / 20.0)
        attack_us = _clamp(float(settings.get("compressorAttackUs", 200.0)), 20.0, 800.0)
        release_ms = _clamp(float(settings.get("compressorReleaseMs", 200.0)), 50.0, 1100.0)
        attack_sec = max(1e-6, attack_us * 1e-6)
        release_sec = max(1e-4, release_ms * 1e-3)
        self.attack_coeff = math.exp(-1.0 / (self.sample_rate * attack_sec))
        self.release_coeff = math.exp(-1.0 / (self.sample_rate * release_sec))
        self.gain = 1.0

    def process_frame(self, values, channel_count, max_pos):
        input_gain = self.input_gain
        peak = 0.0
        for ch in range(channel_count):
            x = values[ch] * input_gain
            values[ch] = x
            ax = abs(x)
            if ax > peak:
                peak = ax
        threshold = max(1e-12, float(max_pos) * self.threshold_ratio)
        if peak > threshold:
            over = peak / threshold
            target = over ** ((1.0 / self.ratio) - 1.0)
        else:
            target = 1.0
        gain = self.gain
        coeff = self.attack_coeff if target < gain else self.release_coeff
        gain = target + coeff * (gain - target)
        self.gain = gain
        total_gain = gain * self.output_gain
        for ch in range(channel_count):
            values[ch] *= total_gain


_EQ_STATE_KEYS = (
    "highPassHz", "highPassResonance", "highPassLfoDepth",
    "band1Freq", "band1Gain", "band1Q",
    "band2Freq", "band2Gain", "band2Q",
    "band3Freq", "band3Gain", "band3Q",
    "band4Freq", "band4Gain", "band4Q",
    "lowPassHz", "lowPassResonance", "lowPassLfoDepth",
)
_REVERB_STRUCTURE_KEYS = ("reverbSize", "reverbDecaySec", "reverbDamping")


def _settings_signature(settings, keys):
    return tuple(settings.get(key) for key in keys)


class StreamProcessor:
    """Stateful dry processor with Chorus send and orderable serial Delay/Reverb sends."""

    __slots__ = (
        "sample_rate", "source_channels", "output_channels", "bits", "generation", "settings_snapshot", "stages",
        "highpass_stage", "lowpass_stage", "highpass_base_hz", "lowpass_base_hz",
        "highpass_q", "lowpass_q", "highpass_lfo_depth", "lowpass_lfo_depth",
        "filter_lfo_counter", "filter_lfo_interval",
        "master_gain", "master_active", "compressor", "pan_value", "pan_left", "pan_right",
        "pan_lfo_wave", "pan_lfo_rate", "pan_lfo_smooth", "pan_lfo_smooth_coeff",
        "pan_lfo_smoothed", "pan_lfo_depth", "pan_lfo_phase", "pan_lfo_phase_inc",
        "pan_lfo_hold", "pan_lfo_rng", "chorus", "delay", "reverb", "chorus_send", "delay_send", "reverb_send",
        "delay_before_reverb", "limiter_enabled", "limiter_ceiling_ratio", "limiter_release_coeff", "limiter_gain", "wet_limiter_gain", "passthrough",
    )

    def __init__(self, sample_rate, channels, bits, settings, generation=0, output_channels=None):
        self.sample_rate = int(sample_rate)
        self.source_channels = max(1, int(channels))
        self.output_channels = max(1, int(output_channels or channels))
        self.bits = int(bits)
        self.generation = generation
        self.settings_snapshot = dict(settings)
        self.stages = []

        # Resonant High/Low Pass filters. Resonance 0 preserves the historical
        # Butterworth Q exactly. The two signed LFO depths share the same LFO
        # waveform/rate/smoothing as Pan and map +/-100 to +/-4 octaves.
        self.highpass_stage = None
        self.lowpass_stage = None
        self.highpass_base_hz = _clamp(float(settings.get("highPassHz", 20.0)), 20.0, 20000.0)
        self.lowpass_base_hz = _clamp(float(settings.get("lowPassHz", 20000.0)), 20.0, 20000.0)
        self.highpass_q = resonance_to_q(settings.get("highPassResonance", 0.0))
        self.lowpass_q = resonance_to_q(settings.get("lowPassResonance", 0.0))
        self.highpass_lfo_depth = _clamp(float(settings.get("highPassLfoDepth", 0.0)), -100.0, 100.0) / 100.0
        self.lowpass_lfo_depth = _clamp(float(settings.get("lowPassLfoDepth", 0.0)), -100.0, 100.0) / 100.0
        self.filter_lfo_interval = 8
        self.filter_lfo_counter = 0

        hp_active = (
            self.highpass_base_hz > 20.0001
            or float(settings.get("highPassResonance", 0.0)) > 1e-9
            or abs(self.highpass_lfo_depth) > 1e-12
        )
        if hp_active:
            self.highpass_stage = BiquadStage(
                highpass_coefficients(self.highpass_base_hz, self.sample_rate, self.highpass_q),
                self.source_channels,
            )
            self.stages.append(self.highpass_stage)
        for index in range(1, 5):
            gain = float(settings.get(f"band{index}Gain", 0.0))
            if abs(gain) < 1e-9:
                continue
            freq = float(settings.get(f"band{index}Freq", 1000.0))
            q = float(settings.get(f"band{index}Q", 1.0))
            self.stages.append(BiquadStage(peaking_coefficients(freq, gain, q, self.sample_rate), self.source_channels))
        lp_active = (
            self.lowpass_base_hz < 19999.999
            or float(settings.get("lowPassResonance", 0.0)) > 1e-9
            or abs(self.lowpass_lfo_depth) > 1e-12
        )
        if lp_active:
            self.lowpass_stage = BiquadStage(
                lowpass_coefficients(self.lowpass_base_hz, self.sample_rate, self.lowpass_q),
                self.source_channels,
            )
            self.stages.append(self.lowpass_stage)

        master_db = _clamp(float(settings.get("masterGainDb", 0.0)), -60.0, 12.0)
        self.master_gain = 10.0 ** (master_db / 20.0)
        self.master_active = abs(master_db) > 1e-12
        self.compressor = FETCompressor1176(self.sample_rate, settings) if bool(settings.get("compressorEnabled", False)) else None
        self.pan_value = _clamp(float(settings.get("pan", 0.0)), -100.0, 100.0) / 100.0
        # 0 dB-centre balance pan. Mono speech is normally full-level before pan;
        # keeping the dominant side at unity avoids the old ~3 dB level step as
        # soon as pan moved away from centre and forced a stereo speech stream.
        if self.pan_value < 0.0:
            self.pan_left = 1.0
            self.pan_right = 1.0 + self.pan_value
        else:
            self.pan_left = 1.0 - self.pan_value
            self.pan_right = 1.0

        # Pan LFO. Depth 0 is a true bypass and preserves TEST22's pan path exactly.
        wave = str(settings.get("panLfoWave", "Sine"))
        self.pan_lfo_wave = wave if wave in ("Sine", "Tri", "Saw", "Square", "S&H") else "Sine"
        self.pan_lfo_rate = _clamp(float(settings.get("panLfoRateHz", 0.50)), 0.01, 80.0)
        self.pan_lfo_smooth = _clamp(float(settings.get("panLfoSmooth", 0.0)), 0.0, 100.0) / 100.0
        # One-pole slew/low-pass on the LFO waveform. 0 is an exact bypass.
        # The control maps quadratically up to 100 ms so low values stay subtle,
        # while high values can strongly round Saw/Square/S&H transitions.
        smooth_ms = 100.0 * self.pan_lfo_smooth * self.pan_lfo_smooth
        self.pan_lfo_smooth_coeff = (
            0.0 if smooth_ms <= 1e-12
            else math.exp(-1.0 / (self.sample_rate * smooth_ms * 0.001))
        )
        self.pan_lfo_smoothed = 0.0
        self.pan_lfo_depth = _clamp(float(settings.get("panLfoDepth", 0.0)), -100.0, 100.0) / 100.0
        self.pan_lfo_phase = 0.0
        self.pan_lfo_phase_inc = self.pan_lfo_rate / float(self.sample_rate)
        self.pan_lfo_rng = 0x13579BDF
        self.pan_lfo_rng = (1664525 * self.pan_lfo_rng + 1013904223) & 0xFFFFFFFF
        self.pan_lfo_hold = (self.pan_lfo_rng / 4294967295.0) * 2.0 - 1.0

        # TEST19 real bypass: an enabled stereo effect with no audible wet path is
        # not even instantiated. This also avoids forcing mono speech to stereo.
        chorus_active = (
            bool(settings.get("chorusEnabled", False))
            and float(settings.get("chorusMix", 35.0)) > 1e-9
            and float(settings.get("chorusDepth", 60.0)) > 1e-9
        )
        self.chorus_send = _clamp(float(settings.get("chorusMix", 35.0)), 0.0, 100.0) / 100.0
        self.delay_send = _clamp(float(settings.get("delayMix", 20.0)), 0.0, 100.0) / 100.0
        self.reverb_send = _clamp(float(settings.get("reverbMix", 15.0)), 0.0, 100.0) / 100.0
        # Routing toggle for the time-effect chain. False keeps the requested
        # default order Reverb -> Delay; True moves Delay before Reverb.
        self.delay_before_reverb = bool(settings.get("delayBeforeReverb", False))
        delay_active = bool(settings.get("delayEnabled", False)) and self.delay_send > 1e-12
        reverb_active = bool(settings.get("reverbEnabled", False)) and self.reverb_send > 1e-12
        # Chorus remains an independent auxiliary send. Delay and Reverb use
        # send-style serial routing: the first wet return is audible directly and
        # is also included in the signal sent to the second effect. This preserves
        # the full dry speech level while making the selected order audible.
        self.chorus = StereoChorus(self.sample_rate, settings) if chorus_active else None
        self.delay = StereoDelay(self.sample_rate, settings) if delay_active else None
        self.reverb = StereoReverb(self.sample_rate, settings) if reverb_active else None

        self.limiter_enabled = bool(settings.get("limiterEnabled", True))
        ceiling_db = _clamp(float(settings.get("limiterCeilingDb", -0.1)), -24.0, 0.0)
        self.limiter_ceiling_ratio = 10.0 ** (ceiling_db / 20.0)
        release_ms = _clamp(float(settings.get("limiterReleaseMs", 60.0)), 1.0, 2000.0)
        self.limiter_release_coeff = math.exp(-1.0 / (self.sample_rate * release_ms * 0.001))
        self.limiter_gain = 1.0
        self.wet_limiter_gain = 1.0

        # Exact byte passthrough is possible only when channel count is unchanged and
        # every audible stage is bypassed. Pan zero is neutral in this all-bypassed case.
        self.passthrough = (
            self.source_channels == self.output_channels
            and not self.stages
            and not self.master_active
            and self.compressor is None
            and abs(self.pan_value) <= 1e-12
            and abs(self.pan_lfo_depth) <= 1e-12
            and self.chorus is None
            and self.delay is None
            and self.reverb is None
            and not self.limiter_enabled
        )

    def reset_send_memory(self):
        """Clear only Chorus/Delay/Reverb history at a new speech utterance.

        EQ biquad memory, compressor gain and Pan-LFO continuity are intentionally
        left untouched. This prevents time-effect material from a previously spoken
        item from entering the next item without resetting unrelated processing.
        """
        settings = self.settings_snapshot
        if self.chorus is not None:
            self.chorus = StereoChorus(self.sample_rate, settings)
        if self.delay is not None:
            self.delay = StereoDelay(self.sample_rate, settings)
        if self.reverb is not None:
            self.reverb = StereoReverb(self.sample_rate, settings)
        self.wet_limiter_gain = 1.0

    def reset_shared_lfo(self):
        """Restart the shared Pan/Low Pass/High Pass LFO from a deterministic phase.

        Trigger mode uses manual keyboard actions or real focus/navigator-object
        changes to arm this reset and consumes it immediately before the resulting
        speech. Phase 0 is the natural start of each waveform. Smoothing and S&H
        restart too, while automatic continuation such as Say All remains continuous.
        """
        self.pan_lfo_phase = 0.0
        self.pan_lfo_smoothed = 0.0
        self.filter_lfo_counter = 0
        self.pan_lfo_rng = 0x13579BDF
        self.pan_lfo_rng = (1664525 * self.pan_lfo_rng + 1013904223) & 0xFFFFFFFF
        self.pan_lfo_hold = (self.pan_lfo_rng / 4294967295.0) * 2.0 - 1.0

    def reconfigured(self, settings, generation):
        """Build new coefficients while preserving unrelated DSP memory whenever safe."""
        replacement = StreamProcessor(
            self.sample_rate,
            self.source_channels,
            self.bits,
            settings,
            generation,
            output_channels=self.output_channels,
        )
        old = self.settings_snapshot

        # EQ filter memories survive changes elsewhere, but an EQ topology/parameter change
        # gets fresh biquads to avoid mixing state generated with different coefficients.
        if _settings_signature(old, _EQ_STATE_KEYS) == _settings_signature(settings, _EQ_STATE_KEYS):
            replacement.stages = self.stages
            replacement.highpass_stage = self.highpass_stage
            replacement.lowpass_stage = self.lowpass_stage
            replacement.filter_lfo_counter = self.filter_lfo_counter

        # Compressor envelope can safely continue even while its controls change.
        if self.compressor is not None and replacement.compressor is not None:
            replacement.compressor.gain = self.compressor.gain

        # Pan LFO is intentionally continuous across live parameter changes.
        replacement.pan_lfo_phase = self.pan_lfo_phase
        replacement.pan_lfo_hold = self.pan_lfo_hold
        replacement.pan_lfo_rng = self.pan_lfo_rng
        replacement.pan_lfo_smoothed = self.pan_lfo_smoothed

        # Chorus delay memory and LFO phase survive mode/depth/mix changes.
        if self.chorus is not None and replacement.chorus is not None and self.chorus.size == replacement.chorus.size:
            replacement.chorus.buffer_l = self.chorus.buffer_l
            replacement.chorus.buffer_r = self.chorus.buffer_r
            replacement.chorus.index = self.chorus.index
            replacement.chorus.phase1 = self.chorus.phase1
            replacement.chorus.phase2 = self.chorus.phase2
            replacement.chorus.wet_lpf_l = self.chorus.wet_lpf_l
            replacement.chorus.wet_lpf_r = self.chorus.wet_lpf_r

        # Feedback/mix/ping-pong changes keep the existing delay line. A time change
        # necessarily creates a different buffer and therefore starts a fresh delay.
        if (
            self.delay is not None
            and replacement.delay is not None
            and self.delay.delay_samples == replacement.delay.delay_samples
            and self.delay.size == replacement.delay.size
        ):
            replacement.delay.buf_l = self.delay.buf_l
            replacement.delay.buf_r = self.delay.buf_r
            replacement.delay.index = self.delay.index

        # Reverb Send is non-structural; preserve the network for that change and for
        # changes to unrelated processors. Size/decay/damping rebuild the network.
        if (
            self.reverb is not None
            and replacement.reverb is not None
            and _settings_signature(old, _REVERB_STRUCTURE_KEYS)
            == _settings_signature(settings, _REVERB_STRUCTURE_KEYS)
        ):
            replacement.reverb = self.reverb

        if self.limiter_enabled and replacement.limiter_enabled:
            replacement.limiter_gain = self.limiter_gain
            replacement.wet_limiter_gain = self.wet_limiter_gain
        return replacement

    def _next_lfo_value(self):
        """Return the next shared modulation LFO sample in the range -1..+1."""
        phase = self.pan_lfo_phase
        wave = self.pan_lfo_wave
        if wave == "Tri":
            lfo = 1.0 - 4.0 * abs(phase - 0.5)
        elif wave == "Saw":
            lfo = 2.0 * phase - 1.0
        elif wave == "Square":
            lfo = 1.0 if phase < 0.5 else -1.0
        elif wave == "S&H":
            lfo = self.pan_lfo_hold
        else:
            index = int(phase * _SINE_TABLE_SIZE) % _SINE_TABLE_SIZE
            lfo = _SINE_TABLE[index]

        phase += self.pan_lfo_phase_inc
        if phase >= 1.0:
            phase -= 1.0
            if wave == "S&H":
                rng = (1664525 * self.pan_lfo_rng + 1013904223) & 0xFFFFFFFF
                self.pan_lfo_rng = rng
                self.pan_lfo_hold = (rng / 4294967295.0) * 2.0 - 1.0
        self.pan_lfo_phase = phase

        coeff = self.pan_lfo_smooth_coeff
        if coeff > 0.0:
            smoothed = coeff * self.pan_lfo_smoothed + (1.0 - coeff) * lfo
            self.pan_lfo_smoothed = smoothed
            lfo = smoothed
        else:
            self.pan_lfo_smoothed = lfo
        return lfo

    def _pan_gains_from_lfo(self, lfo):
        """Return 0 dB-centre balance L/R gains for one shared LFO sample."""
        pan = _clamp(self.pan_value + lfo * self.pan_lfo_depth, -1.0, 1.0)
        table_index = int(round((pan + 1.0) * 0.5 * _PAN_GAIN_TABLE_SIZE))
        if table_index < 0:
            table_index = 0
        elif table_index > _PAN_GAIN_TABLE_SIZE:
            table_index = _PAN_GAIN_TABLE_SIZE
        return _PAN_GAIN_TABLE[table_index]

    def _update_modulated_filter_coefficients(self, lfo):
        """Update resonant filter coefficients at a small control-rate interval.

        The LFO itself still advances once per audio frame. Recalculating biquad
        trig coefficients every 8 frames keeps modulation smooth while avoiding
        unnecessary Python CPU load in a screen-reader audio path.
        """
        if self.filter_lfo_counter > 0:
            self.filter_lfo_counter -= 1
            return
        self.filter_lfo_counter = self.filter_lfo_interval - 1
        max_hz = min(20000.0, max(20.0, self.sample_rate * 0.49))
        if self.highpass_stage is not None and abs(self.highpass_lfo_depth) > 1e-12:
            octaves = 4.0 * self.highpass_lfo_depth * lfo
            freq = _clamp(self.highpass_base_hz * (2.0 ** octaves), 20.0, max_hz)
            self.highpass_stage.set_coefficients(highpass_coefficients(freq, self.sample_rate, self.highpass_q))
        if self.lowpass_stage is not None and abs(self.lowpass_lfo_depth) > 1e-12:
            octaves = 4.0 * self.lowpass_lfo_depth * lfo
            freq = _clamp(self.lowpass_base_hz * (2.0 ** octaves), 20.0, max_hz)
            self.lowpass_stage.set_coefficients(lowpass_coefficients(freq, self.sample_rate, self.lowpass_q))

    def process_split(self, raw):
        """Return (master PCM, None) for one source buffer.

        The dry path stays at full level. Chorus remains a parallel auxiliary
        send. Delay and Reverb form an orderable serial send chain: the first wet
        return is heard directly and is also fed, together with the dry source,
        into the second effect. Optional Gate Hold tails reuse the same routing
        and the same live effect memories.
        """
        if not raw:
            return raw, None
        if self.passthrough:
            return raw, None
        samples, max_pos, min_neg = _decode_pcm(raw, self.bits)
        source_channels = self.source_channels
        output_channels = self.output_channels
        frame_count = len(samples) // source_channels
        if frame_count <= 0:
            return raw, None
        usable_count = frame_count * source_channels
        if len(samples) != usable_count:
            samples = samples[:usable_count]

        stages = self.stages
        source_values = [0.0] * source_channels
        frame_values = [0.0] * output_channels
        output = [0.0] * (frame_count * output_channels)
        wet_active = self.chorus is not None or self.delay is not None or self.reverb is not None
        # TEST60: Chain Blend is deliberately read from the immutable settings snapshot
        # instead of changing StreamProcessor's instance layout. This keeps the
        # validated TEST59 processor construction/reconfiguration path untouched.
        try:
            chain_first_direct = 1.0 - (_clamp(float(self.settings_snapshot.get("chainBlend", 0.0)), 0.0, 100.0) / 100.0)
        except Exception:
            chain_first_direct = 1.0
        source_pos = 0
        output_pos = 0
        limiter_gain = self.limiter_gain
        limiter_enabled = self.limiter_enabled
        limiter_release_coeff = self.limiter_release_coeff
        ceiling = max_pos * self.limiter_ceiling_ratio
        master_active = self.master_active
        master_gain = self.master_gain
        pan_lfo_depth = self.pan_lfo_depth
        pan_lfo_requested = abs(pan_lfo_depth) > 1e-12
        pan_lfo_active = pan_lfo_requested and output_channels >= 2
        pan_left_static = self.pan_left
        pan_right_static = self.pan_right
        pan_value = self.pan_value
        shared_lfo_active = (
            pan_lfo_requested
            or abs(self.highpass_lfo_depth) > 1e-12
            or abs(self.lowpass_lfo_depth) > 1e-12
        )
        filter_lfo_active = abs(self.highpass_lfo_depth) > 1e-12 or abs(self.lowpass_lfo_depth) > 1e-12

        # The shared Pan/Filter LFO advances once per frame. Its waveform,
        # phase increment and smoothing coefficient are invariant for the whole
        # PCM chunk, so keep its changing phase/S&H/smoothing state local and
        # commit it back after the chunk. This preserves _next_lfo_value()'s
        # exact arithmetic/update order while avoiding a Python method call and
        # repeated attribute resolution for every source frame.
        if shared_lfo_active:
            lfo_phase = self.pan_lfo_phase
            lfo_phase_inc = self.pan_lfo_phase_inc
            lfo_wave = self.pan_lfo_wave
            lfo_hold = self.pan_lfo_hold
            lfo_rng = self.pan_lfo_rng
            lfo_smooth_coeff = self.pan_lfo_smooth_coeff
            lfo_smoothed = self.pan_lfo_smoothed

        # Delay is intentionally kept as a chunk-local hot path. Buffer references,
        # feedback coefficients and the write index do not change while one PCM
        # chunk is processed, so repeatedly resolving them through a Python method
        # call for every stereo frame only adds interpreter overhead. The arithmetic
        # and update order below remain identical to StereoDelay.process_wet().
        delay = self.delay
        if delay is not None:
            delay_buf_l = delay.buf_l
            delay_buf_r = delay.buf_r
            delay_index = delay.index
            delay_size = delay.size
            delay_pp = delay.pingpong
            delay_one_minus_pp = 1.0 - delay_pp
            delay_feedback = delay.feedback

        # 1176 is also kept chunk-local. Input/output gain, ratio, envelope
        # coefficients and threshold do not change during one PCM chunk, so there
        # is no reason to resolve them through FETCompressor1176.process_frame()
        # for every source frame. The arithmetic and envelope update order below
        # intentionally match process_frame() exactly; only Python method/attribute
        # overhead is removed.
        compressor = self.compressor
        if compressor is not None:
            compressor_input_gain = compressor.input_gain
            compressor_output_gain = compressor.output_gain
            compressor_ratio = compressor.ratio
            compressor_exponent = (1.0 / compressor_ratio) - 1.0
            compressor_threshold = max(1e-12, float(max_pos) * compressor.threshold_ratio)
            compressor_attack_coeff = compressor.attack_coeff
            compressor_release_coeff = compressor.release_coeff
            compressor_gain = compressor.gain

        for _ in range(frame_count):
            if shared_lfo_active:
                phase = lfo_phase
                if lfo_wave == "Tri":
                    lfo_value = 1.0 - 4.0 * abs(phase - 0.5)
                elif lfo_wave == "Saw":
                    lfo_value = 2.0 * phase - 1.0
                elif lfo_wave == "Square":
                    lfo_value = 1.0 if phase < 0.5 else -1.0
                elif lfo_wave == "S&H":
                    lfo_value = lfo_hold
                else:
                    index = int(phase * _SINE_TABLE_SIZE) % _SINE_TABLE_SIZE
                    lfo_value = _SINE_TABLE[index]

                phase += lfo_phase_inc
                if phase >= 1.0:
                    phase -= 1.0
                    if lfo_wave == "S&H":
                        lfo_rng = (1664525 * lfo_rng + 1013904223) & 0xFFFFFFFF
                        lfo_hold = (lfo_rng / 4294967295.0) * 2.0 - 1.0
                lfo_phase = phase

                if lfo_smooth_coeff > 0.0:
                    lfo_smoothed = lfo_smooth_coeff * lfo_smoothed + (1.0 - lfo_smooth_coeff) * lfo_value
                    lfo_value = lfo_smoothed
                else:
                    lfo_smoothed = lfo_value

                if filter_lfo_active:
                    self._update_modulated_filter_coefficients(lfo_value)
            else:
                lfo_value = 0.0

            # Biquad stages are a per-sample hot path. High Pass, the four
            # parametric bands and Low Pass keep exactly the same Direct Form I
            # arithmetic/state update order as BiquadStage.process(), but mono
            # and stereo speech are handled directly here so Python does not pay
            # one method call for every active stage and every source sample.
            if source_channels == 1:
                x = float(samples[source_pos])
                for stage in stages:
                    st = stage.states[0]
                    x1, x2, y1, y2 = st
                    y = stage.b0 * x + stage.b1 * x1 + stage.b2 * x2 - stage.a1 * y1 - stage.a2 * y2
                    st[1] = x1
                    st[0] = x
                    st[3] = y1
                    st[2] = y
                    x = y
                source_values[0] = x * master_gain if master_active else x
            elif source_channels == 2:
                x = float(samples[source_pos])
                for stage in stages:
                    st = stage.states[0]
                    x1, x2, y1, y2 = st
                    y = stage.b0 * x + stage.b1 * x1 + stage.b2 * x2 - stage.a1 * y1 - stage.a2 * y2
                    st[1] = x1
                    st[0] = x
                    st[3] = y1
                    st[2] = y
                    x = y
                source_values[0] = x * master_gain if master_active else x

                x = float(samples[source_pos + 1])
                for stage in stages:
                    st = stage.states[1]
                    x1, x2, y1, y2 = st
                    y = stage.b0 * x + stage.b1 * x1 + stage.b2 * x2 - stage.a1 * y1 - stage.a2 * y2
                    st[1] = x1
                    st[0] = x
                    st[3] = y1
                    st[2] = y
                    x = y
                source_values[1] = x * master_gain if master_active else x
            else:
                for ch in range(source_channels):
                    x = float(samples[source_pos + ch])
                    for stage in stages:
                        st = stage.states[ch]
                        x1, x2, y1, y2 = st
                        y = stage.b0 * x + stage.b1 * x1 + stage.b2 * x2 - stage.a1 * y1 - stage.a2 * y2
                        st[1] = x1
                        st[0] = x
                        st[3] = y1
                        st[2] = y
                        x = y
                    source_values[ch] = x * master_gain if master_active else x

            if compressor is not None:
                if source_channels == 1:
                    x = source_values[0] * compressor_input_gain
                    source_values[0] = x
                    peak = abs(x)
                elif source_channels == 2:
                    x0 = source_values[0] * compressor_input_gain
                    source_values[0] = x0
                    peak = abs(x0)
                    x1 = source_values[1] * compressor_input_gain
                    source_values[1] = x1
                    ax = abs(x1)
                    if ax > peak:
                        peak = ax
                else:
                    peak = 0.0
                    for ch in range(source_channels):
                        x = source_values[ch] * compressor_input_gain
                        source_values[ch] = x
                        ax = abs(x)
                        if ax > peak:
                            peak = ax
                if peak > compressor_threshold:
                    over = peak / compressor_threshold
                    target = over ** compressor_exponent
                else:
                    target = 1.0
                coeff = compressor_attack_coeff if target < compressor_gain else compressor_release_coeff
                compressor_gain = target + coeff * (compressor_gain - target)
                total_gain = compressor_gain * compressor_output_gain
                if source_channels == 1:
                    source_values[0] *= total_gain
                elif source_channels == 2:
                    source_values[0] *= total_gain
                    source_values[1] *= total_gain
                else:
                    for ch in range(source_channels):
                        source_values[ch] *= total_gain

            if pan_lfo_active:
                pan = pan_value + lfo_value * pan_lfo_depth
                if pan < -1.0:
                    pan = -1.0
                elif pan > 1.0:
                    pan = 1.0
                table_index = int(round((pan + 1.0) * 0.5 * _PAN_GAIN_TABLE_SIZE))
                if table_index < 0:
                    table_index = 0
                elif table_index > _PAN_GAIN_TABLE_SIZE:
                    table_index = _PAN_GAIN_TABLE_SIZE
                pan_left, pan_right = _PAN_GAIN_TABLE[table_index]
            else:
                pan_left, pan_right = pan_left_static, pan_right_static

            if source_channels == 1 and output_channels >= 2:
                mono = source_values[0]
                frame_values[0] = mono * pan_left
                frame_values[1] = mono * pan_right
                for ch in range(2, output_channels):
                    frame_values[ch] = mono
            elif source_channels == 2 and output_channels == 2:
                frame_values[0] = source_values[0] * pan_left
                frame_values[1] = source_values[1] * pan_right
            else:
                common = min(source_channels, output_channels)
                for ch in range(common):
                    frame_values[ch] = source_values[ch]
                for ch in range(common, output_channels):
                    frame_values[ch] = 0.0
                if output_channels >= 2:
                    frame_values[0] *= pan_left
                    frame_values[1] *= pan_right

            # The dry path stays untouched. Chorus is still a parallel send.
            # Delay/Reverb are now a serial send chain so the first effect can
            # genuinely excite the second. The second Send controls how much of
            # the combined dry + first wet signal enters that effect.
            if wet_active and output_channels >= 2:
                send_l, send_r = frame_values[0], frame_values[1]
                wet_l = wet_r = 0.0
                if self.chorus is not None:
                    cl, cr = self.chorus.process_wet(send_l * self.chorus_send, send_r * self.chorus_send)
                    wet_l += cl
                    wet_r += cr

                if self.delay_before_reverb:
                    # Dry -> Delay -> Reverb. The audible Delay return is also
                    # included in the Reverb input, so echoes acquire room decay.
                    dl = dr = 0.0
                    if delay is not None:
                        delay_in_l = send_l * self.delay_send
                        delay_in_r = send_r * self.delay_send
                        dl = delay_buf_l[delay_index]
                        dr = delay_buf_r[delay_index]
                        feedback_l = dl * delay_one_minus_pp + dr * delay_pp
                        feedback_r = dr * delay_one_minus_pp + dl * delay_pp
                        if delay_pp <= 1e-12:
                            input_l = delay_in_l
                            input_r = delay_in_r
                        else:
                            mono_input = 0.5 * (delay_in_l + delay_in_r)
                            input_l = delay_in_l * delay_one_minus_pp + mono_input * delay_pp
                            input_r = delay_in_r * delay_one_minus_pp
                        delay_buf_l[delay_index] = input_l + feedback_l * delay_feedback
                        delay_buf_r[delay_index] = input_r + feedback_r * delay_feedback
                        delay_index += 1
                        if delay_index >= delay_size:
                            delay_index = 0
                        first_direct = chain_first_direct if self.reverb is not None else 1.0
                        wet_l += dl * first_direct
                        wet_r += dr * first_direct
                    if self.reverb is not None:
                        rl, rr = self.reverb.process_wet(
                            (send_l + dl) * self.reverb_send,
                            (send_r + dr) * self.reverb_send,
                        )
                        wet_l += rl
                        wet_r += rr
                else:
                    # Dry -> Reverb -> Delay. The audible Reverb return is also
                    # included in the Delay input, so the echoes repeat the room.
                    rl = rr = 0.0
                    if self.reverb is not None:
                        rl, rr = self.reverb.process_wet(send_l * self.reverb_send, send_r * self.reverb_send)
                        first_direct = chain_first_direct if delay is not None else 1.0
                        wet_l += rl * first_direct
                        wet_r += rr * first_direct
                    if delay is not None:
                        delay_in_l = (send_l + rl) * self.delay_send
                        delay_in_r = (send_r + rr) * self.delay_send
                        dl = delay_buf_l[delay_index]
                        dr = delay_buf_r[delay_index]
                        feedback_l = dl * delay_one_minus_pp + dr * delay_pp
                        feedback_r = dr * delay_one_minus_pp + dl * delay_pp
                        if delay_pp <= 1e-12:
                            input_l = delay_in_l
                            input_r = delay_in_r
                        else:
                            mono_input = 0.5 * (delay_in_l + delay_in_r)
                            input_l = delay_in_l * delay_one_minus_pp + mono_input * delay_pp
                            input_r = delay_in_r * delay_one_minus_pp
                        delay_buf_l[delay_index] = input_l + feedback_l * delay_feedback
                        delay_buf_r[delay_index] = input_r + feedback_r * delay_feedback
                        delay_index += 1
                        if delay_index >= delay_size:
                            delay_index = 0
                        wet_l += dl
                        wet_r += dr

                frame_values[0] += wet_l
                frame_values[1] += wet_r

            # The limiter sees the complete master sum (dry + all send returns),
            # just like a master-bus limiter after aux returns in a DAW. Mono and
            # stereo are by far the common NVDA stream layouts, so handle them
            # directly instead of running two tiny Python channel loops per frame.
            if limiter_enabled:
                if output_channels == 1:
                    peak = abs(frame_values[0])
                elif output_channels == 2:
                    peak = abs(frame_values[0])
                    ax = abs(frame_values[1])
                    if ax > peak:
                        peak = ax
                else:
                    peak = 0.0
                    for ch in range(output_channels):
                        ax = abs(frame_values[ch])
                        if ax > peak:
                            peak = ax
                if peak > ceiling and peak > 0.0:
                    target = ceiling / peak
                    if target < limiter_gain:
                        limiter_gain = target
                else:
                    limiter_gain = 1.0 - (1.0 - limiter_gain) * limiter_release_coeff
                if output_channels == 1:
                    output[output_pos] = frame_values[0] * limiter_gain
                elif output_channels == 2:
                    output[output_pos] = frame_values[0] * limiter_gain
                    output[output_pos + 1] = frame_values[1] * limiter_gain
                else:
                    for ch in range(output_channels):
                        output[output_pos + ch] = frame_values[ch] * limiter_gain
            else:
                limiter_gain = 1.0
                if output_channels == 1:
                    output[output_pos] = frame_values[0]
                elif output_channels == 2:
                    output[output_pos] = frame_values[0]
                    output[output_pos + 1] = frame_values[1]
                else:
                    for ch in range(output_channels):
                        output[output_pos + ch] = frame_values[ch]

            source_pos += source_channels
            output_pos += output_channels

        if delay is not None:
            delay.index = delay_index
        if compressor is not None:
            compressor.gain = compressor_gain
        if shared_lfo_active:
            self.pan_lfo_phase = lfo_phase
            self.pan_lfo_hold = lfo_hold
            self.pan_lfo_rng = lfo_rng
            self.pan_lfo_smoothed = lfo_smoothed
        self.limiter_gain = limiter_gain
        self.wet_limiter_gain = 1.0
        return _encode_pcm(output, self.bits, max_pos, min_neg), None

    def render_gate_hold_chunk(self, frame_count, include_delay=True, include_reverb=True):
        """Advance the existing Delay/Reverb memories with digital-silent input.

        This is a stream hold, not a reconstructed effect. The exact delay lines,
        combs and all-pass buffers that processed the final source sample continue
        from their current indices. Feeding zero input is sufficient: WavePlayer
        receives continuous PCM while the stored effect energy decays naturally.
        """
        frame_count = max(0, int(frame_count))
        if frame_count <= 0 or self.passthrough or self.output_channels < 2:
            return b"", 0.0

        delay_active = bool(include_delay and self.delay is not None)
        reverb_active = bool(include_reverb and self.reverb is not None)
        if not delay_active and not reverb_active:
            return b"", 0.0

        if self.bits == 8:
            max_pos, min_neg = 127.0, -128.0
        elif self.bits == 16:
            max_pos, min_neg = 32767.0, -32768.0
        elif self.bits == 24:
            max_pos, min_neg = 8388607.0, -8388608.0
        elif self.bits == 32:
            max_pos, min_neg = 2147483647.0, -2147483648.0
        else:
            return b"", 0.0

        output_channels = self.output_channels
        output = [0.0] * (frame_count * output_channels)
        output_pos = 0
        peak_abs = 0.0
        limiter_gain = self.limiter_gain
        ceiling = max_pos * self.limiter_ceiling_ratio

        try:
            chain_first_direct = 1.0 - (_clamp(float(self.settings_snapshot.get("chainBlend", 0.0)), 0.0, 100.0) / 100.0)
        except Exception:
            chain_first_direct = 1.0

        delay = self.delay if delay_active else None
        if delay is not None:
            delay_buf_l = delay.buf_l
            delay_buf_r = delay.buf_r
            delay_index = delay.index
            delay_size = delay.size
            delay_pp = delay.pingpong
            delay_one_minus_pp = 1.0 - delay_pp
            delay_feedback = delay.feedback

        for _ in range(frame_count):
            wet_l = wet_r = 0.0
            if self.delay_before_reverb:
                # Tail follows the same Delay -> Reverb live routing.
                dl = dr = 0.0
                if delay is not None:
                    dl = delay_buf_l[delay_index]
                    dr = delay_buf_r[delay_index]
                    feedback_l = dl * delay_one_minus_pp + dr * delay_pp
                    feedback_r = dr * delay_one_minus_pp + dl * delay_pp
                    # Input is digital silence. Preserve the exact zero-input
                    # result of StereoDelay.process_wet() without the method call.
                    delay_buf_l[delay_index] = feedback_l * delay_feedback
                    delay_buf_r[delay_index] = feedback_r * delay_feedback
                    delay_index += 1
                    if delay_index >= delay_size:
                        delay_index = 0
                    first_direct = chain_first_direct if reverb_active else 1.0
                    wet_l += dl * first_direct
                    wet_r += dr * first_direct
                if reverb_active:
                    rl, rr = self.reverb.process_wet(dl * self.reverb_send, dr * self.reverb_send)
                    wet_l += rl
                    wet_r += rr
            else:
                # Tail follows the same Reverb -> Delay live routing.
                rl = rr = 0.0
                if reverb_active:
                    rl, rr = self.reverb.process_wet(0.0, 0.0)
                    first_direct = chain_first_direct if delay is not None else 1.0
                    wet_l += rl * first_direct
                    wet_r += rr * first_direct
                if delay is not None:
                    delay_in_l = rl * self.delay_send
                    delay_in_r = rr * self.delay_send
                    dl = delay_buf_l[delay_index]
                    dr = delay_buf_r[delay_index]
                    feedback_l = dl * delay_one_minus_pp + dr * delay_pp
                    feedback_r = dr * delay_one_minus_pp + dl * delay_pp
                    if delay_pp <= 1e-12:
                        input_l = delay_in_l
                        input_r = delay_in_r
                    else:
                        mono_input = 0.5 * (delay_in_l + delay_in_r)
                        input_l = delay_in_l * delay_one_minus_pp + mono_input * delay_pp
                        input_r = delay_in_r * delay_one_minus_pp
                    delay_buf_l[delay_index] = input_l + feedback_l * delay_feedback
                    delay_buf_r[delay_index] = input_r + feedback_r * delay_feedback
                    delay_index += 1
                    if delay_index >= delay_size:
                        delay_index = 0
                    wet_l += dl
                    wet_r += dr

            if self.limiter_enabled:
                peak = max(abs(wet_l), abs(wet_r))
                if peak > ceiling and peak > 0.0:
                    target = ceiling / peak
                    if target < limiter_gain:
                        limiter_gain = target
                else:
                    limiter_gain = 1.0 - (1.0 - limiter_gain) * self.limiter_release_coeff
                wet_l *= limiter_gain
                wet_r *= limiter_gain

            output[output_pos] = wet_l
            output[output_pos + 1] = wet_r
            for ch in range(2, output_channels):
                output[output_pos + ch] = 0.0
            peak_abs = max(peak_abs, abs(wet_l), abs(wet_r))
            output_pos += output_channels

        if delay is not None:
            delay.index = delay_index
        self.limiter_gain = limiter_gain
        peak_ratio = peak_abs / max_pos if max_pos > 0.0 else 0.0
        return _encode_pcm(output, self.bits, max_pos, min_neg), peak_ratio

    def process(self, raw):
        """Compatibility wrapper returning only the dry stream."""
        dry, _wet = self.process_split(raw)
        return dry

