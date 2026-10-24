# Audio Processor for NVDA

Audio Processor is a real-time multi-effect audio processor for the NVDA screen reader. It intercepts audio that uses NVDA's `nvwave/WavePlayer` path and can process speech, NVDA sounds, or both.

Current source version: **1.0.62 (TEST62)**.

## Main features

- Resonant High Pass and Low Pass filters.
- Four-band parametric EQ.
- Master Gain.
- Lightweight 1176-style compressor.
- Shared Free/Trigger LFO for Pan, Low Pass and High Pass.
- Stereo Chorus.
- Stereo Delay and Stereo Reverb with selectable serial order.
- Reverb Type selector: Classic, Room, Chamber, Hall, Cathedral, Plate and Spring.
- Chain Blend control for the Delay/Reverb serial return.
- Limiter.
- Optional preset-aware NVDA Audio Device Lock.
- Independent Delay/Reverb post-source tails, disabled by default.
- 32 Factory presets plus User presets.
- Custom Actions API integration.
- Documentation and localization in English, Italian, Spanish and French.

## Reverb Type (TEST62)

In the Audio Processor dialog, use **Tab** to reach **Reverb Type** after the
Reverb Tail checkbox and use Up/Down to choose an acoustic character. The
change is applied live. The seven types use different network configurations,
not just parameter presets. Reverb Size, Decay, Damping and Send continue to
work for every type. `Classic` is the original TEST61 algorithm, unchanged.
Previously saved settings, factory presets and user presets without the new key
remain on `Classic`, preserving their sound. The choice is saved in complete
user presets and Custom Actions snapshots.

You can also assign gestures to **Select the next Reverb Type** and **Select
the previous Reverb Type** in NVDA's Input Gestures, Audio Processor category.
The Spring type is an inexpensive digital spring-inspired algorithm, not an
impulse-response emulation of a specific physical spring tank.

## Compatibility

The supplied manifest declares:

- Minimum NVDA version: **2025.0.0**
- Last tested NVDA version: **2026.1.0**

The current implementation is intended for **64-bit NVDA** and audio paths that use `nvwave/WavePlayer`.

## Quick start

- `NVDA+Alt+E`: open Audio Processor.
- `NVDA+Alt+Q`: enable or disable Audio Processor.
- Other processor commands are available in **NVDA Input Gestures**, under the **Audio Processor** category.

For the complete user guide, see:

- `doc/en/readme.html`
- `doc/it/readme.html`
- `doc/es/readme.html`
- `doc/fr/readme.html`

## Building the NVDA add-on

No external Python packages are required for packaging.

From the repository root:

```bash
python build.py
```

The generated package is written to `dist/`.

The GitHub Actions workflow also validates the Python files and builds the `.nvda-addon` automatically.

## Repository layout

- `globalPlugins/audioEQ/` — Audio Processor implementation.
- `locale/` — translation sources and compiled catalogs.
- `doc/` — localized user guides.
- `manifest.ini` — NVDA add-on metadata.
- `build.py` — reproducible package builder.

The internal add-on/package name remains `audioEQ` for compatibility with existing installations and stored configuration.

## License

The source package supplied for this repository did not contain an explicit license file. A license should be selected and added before presenting the project as open-source software or accepting contributions under specific terms.
