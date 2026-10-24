"""Standalone DSP tests. No Windows, NVDA installation, or third-party dependencies.

Run with: python -B tests/test_reverb_modes.py
"""
import importlib.util
import math
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_processor_dsp", root / "globalPlugins" / "audioEQ" / "dsp.py")
dsp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dsp)

SETTINGS = {
    "reverbEnabled": True,
    "reverbSize": 53.0,
    "reverbDecaySec": 1.5,
    "reverbDamping": 45.0,
    "reverbMix": 18.0,
}


class ReverbModesTests(unittest.TestCase):
    def test_names_and_classic_default(self):
        self.assertEqual(
            dsp.REVERB_MODES,
            ("Classic", "Room", "Chamber", "Hall", "Cathedral", "Plate", "Spring"),
        )
        reverb = dsp.StereoReverb(8000, SETTINGS)
        self.assertEqual(reverb.mode, "Classic")
        # Golden delay lengths from unmodified TEST61 at this sample rate.
        self.assertEqual([len(c.buffer) for c in reverb.combs_l], [249, 311, 344])
        self.assertEqual(reverb.crossfeed, 0.45)
        self.assertEqual(reverb.ap_l.feedback, 0.5)
        self.assertEqual(reverb.tail_decay_scale, 1.0)

    def test_unknown_types_are_classic(self):
        unknown = dsp.StereoReverb(8000, SETTINGS | {"reverbMode": "invalid"})
        standard = dsp.StereoReverb(8000, SETTINGS)
        self.assertEqual(unknown.mode, "Classic")
        for i in range(1800):
            l, r = (0.8, 0.3) if i == 0 else (0.0, 0.0)
            self.assertEqual(unknown.process_wet(l, r), standard.process_wet(l, r))

    def test_all_structures_are_distinct_and_finite(self):
        signatures = set()
        for mode in dsp.REVERB_MODES:
            rv = dsp.StereoReverb(8000, SETTINGS | {"reverbMode": mode})
            signature = (tuple(len(c.buffer) for c in rv.combs_l), rv.crossfeed, rv.ap_l.feedback)
            self.assertNotIn(signature, signatures)
            signatures.add(signature)
            max_peak = 0.0
            for i in range(13000):
                l, r = rv.process_wet(0.6 if i == 0 else 0.0, 0.3 if i == 0 else 0.0)
                self.assertTrue(math.isfinite(l) and math.isfinite(r))
                max_peak = max(max_peak, abs(l), abs(r))
            self.assertGreater(max_peak, 0.01, mode)

    def test_reconfigured_memory(self):
        cfg = SETTINGS | {"reverbMode": "Hall", "limiterEnabled": False}
        proc = dsp.StreamProcessor(8000, 2, 16, cfg, 1, output_channels=2)
        existing = proc.reverb
        same = proc.reconfigured(cfg | {"masterGainDb": 2}, 2)
        self.assertIs(existing, same.reverb)
        changed = same.reconfigured(cfg | {"reverbMode": "Plate"}, 3)
        self.assertIsNot(existing, changed.reverb)
        self.assertEqual(changed.reverb.mode, "Plate")

    def test_spring_contains_negative_feedback(self):
        rv = dsp.StereoReverb(8000, SETTINGS | {"reverbMode": "Spring"})
        self.assertLess(rv.combs_l[1].feedback, 0)
        self.assertLess(rv.combs_r[1].feedback, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
