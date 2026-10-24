from __future__ import annotations

import ctypes
import math
import threading
import time

import addonHandler
import inputCore
import nvwave
import synthDriverHandler
from synthDriverHandler import pre_synthSpeak
from logHandler import log

from .dsp import StreamProcessor
from . import deviceRouting as _deviceRouting
from . import mixer as _mixer

addonHandler.initTranslation()

_settingsGetter = None

def configure(settingsGetter):
    """Bind the owner of the live Audio Processor settings snapshot.

    Runtime audio stays in this module, while __init__.py continues to own the
    settings dictionary/generation and GUI/preset editing.  The callback keeps
    that ownership boundary explicit and avoids a circular import.
    """
    global _settingsGetter
    _settingsGetter = settingsGetter

def _getSettingsSnapshot():
    getter = _settingsGetter
    if getter is None:
        raise RuntimeError("Audio Processor audio runtime is not configured")
    return getter()

def _lockedAudioDeviceStatus(settings, forceRefresh=False):
    return _deviceRouting.lockedAudioDeviceStatus(settings, forceRefresh=forceRefresh)

_originalFeed = None
_originalInit = None
_originalIdle = None
_originalStop = None
_lastStreamInfo = _("No WavePlayer stream has been intercepted yet.")
_processedChunks = 0
_processedBytes = 0
_lastProcessState = _("no stream")
_lfoTriggerRequested = 0
_lfoTriggerActive = 0
_GATE_HOLD_BLOCK_SECONDS = 0.020
_GATE_HOLD_PRIME_SECONDS = 0.010
# Speech synthesizers may delay WavePlayer.idle() after their last PCM block.
# Keep at most two tiny wet-only blocks queued from the real playback-completion
# callback so Delay/Reverb can cross that synth-side gap without building backlog.
_SPEECH_BRIDGE_BLOCK_SECONDS = 0.005
_SPEECH_BRIDGE_QUEUE_BLOCKS = 2
_SPEECH_MIXER_BLOCK_SECONDS = 0.020
# TEST55: IBM ECI/IBM TTS can flush very short dry utterances when an async
# integrated tail immediately follows them. Suppress only the post-speech tail
# for <=350 ms IBM utterances; live Delay/Reverb processing remains unchanged.
_IBMECI_SHORT_TAIL_MAX_PCM_MS = 350.0

# TEST40 diagnostic-only timing probe.  It records the most recent NVDA speech
# utterance without changing any audio scheduling or DSP.  NVDA+Alt+R snapshots
# the current timeline before its own feedback speech starts and copies it to
# the clipboard, so the exact failing short utterance can be pasted for analysis.
_tailDiagLock = threading.RLock()
_tailDiagUtterance = 0
_tailDiagStart = 0.0
_tailDiagEvents = []
_tailDiagTextChars = 0

def _tailDiagBeginSpeech(speechSequence=None):
    global _tailDiagUtterance, _tailDiagStart, _tailDiagEvents, _tailDiagTextChars
    try:
        chars = sum(len(item) for item in (speechSequence or ()) if isinstance(item, str))
    except Exception:
        chars = 0
    with _tailDiagLock:
        _tailDiagUtterance += 1
        _tailDiagStart = time.perf_counter()
        _tailDiagEvents = []
        _tailDiagTextChars = int(chars)
        _tailDiagEvents.append((0.0, 'preSpeak', {'chars': int(chars)}))

def _tailDiag(event, **fields):
    try:
        now = time.perf_counter()
        with _tailDiagLock:
            if _tailDiagStart <= 0:
                return
            relMs = (now - _tailDiagStart) * 1000.0
            _tailDiagEvents.append((relMs, str(event), dict(fields)))
            if len(_tailDiagEvents) > 80:
                del _tailDiagEvents[:-80]
    except Exception:
        pass

def _tailDiagReport():
    with _tailDiagLock:
        utterance = int(_tailDiagUtterance)
        chars = int(_tailDiagTextChars)
        events = list(_tailDiagEvents)
    if not events:
        return 'Audio Processor tail diagnostic: no speech timing captured.'
    lines = [f'Audio Processor TEST40 tail diagnostic; utterance={utterance}; textChars={chars}']
    previous = 0.0
    onDoneMs = None
    idleMs = None
    firstTailMs = None
    firstTailKind = None
    for relMs, event, fields in events:
        delta = relMs - previous
        previous = relMs
        details = ', '.join(f'{key}={value}' for key, value in fields.items())
        if details:
            lines.append(f'{relMs:.3f} ms (+{delta:.3f}) {event}: {details}')
        else:
            lines.append(f'{relMs:.3f} ms (+{delta:.3f}) {event}')
        if event == 'source-onDone':
            onDoneMs = relMs
        elif event == 'idle-enter' and idleMs is None:
            idleMs = relMs
        elif event in ('bridge-first-feed-call', 'idle-tail-first-feed-call') and firstTailMs is None:
            firstTailMs = relMs
            firstTailKind = event
    summary = []
    if onDoneMs is not None and firstTailMs is not None:
        summary.append(f'firstTail-minus-onDone={firstTailMs - onDoneMs:.3f} ms')
    if idleMs is not None and onDoneMs is not None:
        summary.append(f'idle-minus-onDone={idleMs - onDoneMs:.3f} ms')
    if idleMs is not None and firstTailMs is not None:
        summary.append(f'firstTail-minus-idle={firstTailMs - idleMs:.3f} ms')
    if firstTailKind:
        summary.append(f'firstTailPath={firstTailKind}')
    if summary:
        lines.insert(1, 'Summary: ' + '; '.join(summary))
    return '\r\n'.join(lines)

# Lightweight synthesizer identification used only for compatibility decisions.
# TEST59 removes the rolling pre-mixer benchmark/telemetry from the release path.
def _currentSynthName():
    try:
        synth = synthDriverHandler.getSynth()
        if synth is None:
            return 'none'
        name = getattr(synth, 'name', None)
        if name:
            return str(name)
        return type(synth).__name__
    except Exception:
        return 'unknown'

# TEST36: speech-tail starts are dispatched by one already-running daemon thread.
# Never call WavePlayer.feed() from WavePlayer's native onDone callback: feed()
# can block for buffer space, and re-entering it from the playback-completion
# callback can eventually stall the audio callback/NVDA.  The callback now only
# publishes the latest bridge job and returns immediately.
_speechTailDispatchLock = threading.Lock()
_speechTailDispatchEvent = threading.Event()
_speechTailDispatchStop = threading.Event()
_speechTailDispatchThread = None
_speechTailPendingJob = None
_GATE_HOLD_MAX_SECONDS = 12.0
_GATE_HOLD_QUIET_RATIO = 10.0 ** (-84.0 / 20.0)

def _purposeIsSelected(purpose, settings):
    if not settings.get("enabled", True):
        return False
    target = settings.get("target", "all")
    if target == "speech":
        return purpose is nvwave.AudioPurpose.SPEECH
    if target == "sounds":
        return purpose is nvwave.AudioPurpose.SOUNDS
    return True


def _chorusIsAudible(settings):
    return (
        bool(settings.get("chorusEnabled", False))
        and float(settings.get("chorusMix", 35.0)) > 1e-9
        and float(settings.get("chorusDepth", 60.0)) > 1e-9
    )


def _delayIsAudible(settings):
    return (
        bool(settings.get("delayEnabled", False))
        and float(settings.get("delayMix", 20.0)) > 1e-9
    )


def _reverbIsAudible(settings):
    return (
        bool(settings.get("reverbEnabled", False))
        and float(settings.get("reverbMix", 15.0)) > 1e-9
    )


def _settingsNeedStereoForPurpose(settings, purpose):
    if not _purposeIsSelected(purpose, settings):
        return False
    # Parallel sends return to the same master WavePlayer. A mono source therefore
    # needs a stereo output whenever a stereo send is audible, exactly as a DAW
    # aux return is summed back into the stereo master bus.
    return (
        abs(float(settings.get("pan", 0.0))) > 1e-9
        or abs(float(settings.get("panLfoDepth", 0.0))) > 1e-9
        or _chorusIsAudible(settings)
        or _delayIsAudible(settings)
        or _reverbIsAudible(settings)
    )

def _shouldProcess(player, settings):
    return _purposeIsSelected(getattr(player, "_purpose", None), settings)


def _purposeName(player):
    p = getattr(player, "_purpose", None)
    if p is nvwave.AudioPurpose.SPEECH:
        return _("speech")
    if p is nvwave.AudioPurpose.SOUNDS:
        return _("sounds")
    return _("unknown")


def _patchedInit(
    player,
    channels,
    samplesPerSec,
    bitsPerSample,
    outputDevice=nvwave.WavePlayer.DEFAULT_DEVICE_KEY,
    wantDucking=True,
    purpose=nvwave.AudioPurpose.SPEECH,
):
    """Promote mono only when the selected processing actually needs a stereo path."""
    original = _originalInit
    if original is None:
        return None
    sourceChannels = max(1, int(channels))
    settings, settingsRevision = _getSettingsSnapshot()
    forceStereo = sourceChannels == 1 and _settingsNeedStereoForPurpose(settings, purpose)
    outputChannels = 2 if forceStereo else sourceChannels

    requestedOutputDevice = outputDevice
    effectiveOutputDevice = outputDevice
    if bool(settings.get("audioDeviceLock", False)):
        available, lockedDeviceId, _lockedDeviceName = _lockedAudioDeviceStatus(settings, forceRefresh=True)
        if not available:
            # Never let NVDA's WavePlayer fallback-to-default behaviour defeat an
            # active lock. A player cannot be safely created until the exact
            # endpoint is available again.
            error = OSError("Audio Processor locked audio device is not available")
            try:
                error.winerror = -2147023728  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
            except Exception:
                pass
            raise error
        effectiveOutputDevice = lockedDeviceId

    original(
        player,
        outputChannels,
        samplesPerSec,
        bitsPerSample,
        outputDevice=effectiveOutputDevice,
        wantDucking=wantDucking,
        purpose=purpose,
    )
    player._nvdaAudioEQSourceChannels = sourceChannels
    player._nvdaAudioEQOutputChannels = outputChannels
    player._nvdaAudioEQForcedStereo = forceStereo
    player._nvdaAudioEQRequestedOutputDevice = requestedOutputDevice
    player._nvdaAudioEQOutputDevice = effectiveOutputDevice
    player._nvdaAudioEQWantDucking = bool(wantDucking)
    # Tail work is intentionally serialized per WavePlayer. The processing lock
    # protects the live DSP state; the I/O lock prevents a cancelled tail worker
    # from feeding one last stale block after new speech has already stopped it.
    player._nvdaAudioEQProcessingLock = threading.RLock()
    player._nvdaAudioEQTailIoLock = threading.RLock()
    player._nvdaAudioEQTailSession = None
    # Speech needs one extra bit of bookkeeping: some synths declare idle well
    # after the final PCM buffer has actually played. Sequence numbers let the
    # playback-completion callback distinguish the real last queued speech block
    # from older blocks without making assumptions about synth chunk sizes.
    player._nvdaAudioEQSpeechFeedLock = threading.RLock()
    player._nvdaAudioEQSpeechFeedSeq = 0
    # TEST53: the FIFO is created lazily on the first selected speech feed.
    # Keeping it per WavePlayer preserves device/sample-rate ownership and avoids
    # a global mixer clock shared by unrelated streams.
    player._nvdaAudioEQSpeechMixer = None
    player._nvdaAudioEQSpeechMixerUsedSinceIdle = False
    player._nvdaAudioEQSpeechPcmSinceIdleMs = 0.0


def _extractRawData(data, size):
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if size is not None:
            raw = raw[: int(size)]
        return raw
    if size is None:
        return None
    return ctypes.string_at(data, int(size))


def _promoteMonoToStereo(raw, bits):
    """Duplicate each mono PCM sample to L/R without changing its value.

    TEST42 keeps the exact byte-level result of the previous implementation but
    delegates the interleave work to bytearray extended-slice assignments, which
    execute in C instead of running one Python loop iteration per PCM sample.
    This also works for 24-bit PCM without changing sample representation.
    """
    bytesPerSample = int(bits) // 8
    if bytesPerSample <= 0:
        return raw
    usable = len(raw) - (len(raw) % bytesPerSample)
    if usable <= 0:
        return b""
    source = raw[:usable]
    out = bytearray(usable * 2)
    frameStride = bytesPerSample * 2
    outLength = len(out)
    for byteIndex in range(bytesPerSample):
        component = source[byteIndex:usable:bytesPerSample]
        out[byteIndex:outLength:frameStride] = component
        out[bytesPerSample + byteIndex:outLength:frameStride] = component
    return bytes(out)



def _tailSelection(settings, state):
    """Return which existing time-effect memories may continue after source audio ends."""
    includeDelay = bool(
        settings.get("delayTailEnabled", False)
        and _delayIsAudible(settings)
        and getattr(state, "delay", None) is not None
    )
    includeReverb = bool(
        settings.get("reverbTailEnabled", False)
        and _reverbIsAudible(settings)
        and getattr(state, "reverb", None) is not None
    )
    return includeDelay, includeReverb


def _tailHardLimitSeconds(settings, state, includeDelay, includeReverb):
    """Adaptive safety limit derived from the actual Delay/Reverb controls.

    The real end is detected from the rendered tail level. This limit only stops
    pathological settings (notably 95% feedback) from keeping a screen reader
    player alive for tens of seconds.
    """
    limit = 0.0
    if includeDelay and getattr(state, "delay", None) is not None:
        delaySeconds = max(0.001, float(state.delay.delay_samples) / max(1.0, float(state.sample_rate)))
        feedback = max(0.0, min(0.95, float(state.delay.feedback)))
        if feedback <= 1e-9:
            repeats = 1
        else:
            # Render until repeats have fallen by roughly 84 dB, matching the
            # quiet detector used below. The global cap protects NVDA at very
            # high feedback values.
            repeats = max(1, int(math.ceil(math.log(_GATE_HOLD_QUIET_RATIO) / math.log(feedback))))
        limit = max(limit, delaySeconds * (repeats + 1) + 0.08)
    if includeReverb and getattr(state, "reverb", None) is not None:
        # reverbDecaySec is designed as approximately T60. Give the actual room
        # network a little more than one T60, then let the quiet detector decide.
        decay = max(0.1, min(5.0, float(settings.get("reverbDecaySec", 1.2))))
        size = max(0.0, min(100.0, float(settings.get("reverbSize", 50.0)))) / 100.0
        # Longer Hall/Cathedral network decays need a matching Gate Hold bound.
        # Classic remains exactly at the TEST61 value.
        mode_scale = max(1.0, float(getattr(state.reverb, "tail_decay_scale", 1.0)))
        limit = max(limit, decay * mode_scale * 1.35 + 0.12 + size * 0.08)
    if limit <= 0.0:
        return 0.0
    return min(_GATE_HOLD_MAX_SECONDS, max(0.20, limit))


def _tailQuietHoldFrames(settings, state, includeDelay, includeReverb):
    sampleRate = max(1, int(state.sample_rate))
    quietSeconds = 0.18
    if includeDelay and getattr(state, "delay", None) is not None:
        # A delay can be digitally silent between repeats. Never interpret that
        # gap as the end until a complete delay interval has passed quietly.
        delaySeconds = float(state.delay.delay_samples) / float(sampleRate)
        quietSeconds = max(quietSeconds, min(1.20, delaySeconds + 0.08))
    return max(1, int(round(quietSeconds * sampleRate)))


def _cancelGateHoldTail(player, stopAudio=False, resetSendMemory=False):
    """Cancel a pending post-source hold without allowing stale tail audio to leak."""
    session = getattr(player, "_nvdaAudioEQTailSession", None)
    if session is None:
        return False
    _tailDiag("tail-cancel", stopAudio=bool(stopAudio), kind=type(session).__name__)
    session.cancelEvent.set()
    # TEST38: a speech-tail worker may be sleeping on its completion Condition
    # for up to 10 ms.  Cancellation must wake it immediately; otherwise the
    # single persistent dispatcher cannot service the next short utterance until
    # that timeout expires, producing the characteristic variable ~10 ms gap.
    doneCond = getattr(session, "_doneCond", None)
    if doneCond is not None:
        try:
            with doneCond:
                doneCond.notify_all()
        except Exception:
            pass
    ioLock = getattr(player, "_nvdaAudioEQTailIoLock", None)
    try:
        if ioLock is not None:
            with ioLock:
                if stopAudio and _originalStop is not None:
                    _originalStop(player)
        elif stopAudio and _originalStop is not None:
            _originalStop(player)
    except Exception:
        log.debugWarning("Audio Processor: unable to stop gate-hold tail", exc_info=True)
    try:
        if getattr(player, "_nvdaAudioEQTailSession", None) is session:
            player._nvdaAudioEQTailSession = None
    except Exception:
        pass
    if resetSendMemory:
        procLock = getattr(player, "_nvdaAudioEQProcessingLock", None)
        try:
            if procLock is not None:
                with procLock:
                    state = getattr(player, "_nvdaAudioEQState", None)
                    if state is session.state:
                        state.reset_send_memory()
            else:
                state = getattr(player, "_nvdaAudioEQState", None)
                if state is session.state:
                    state.reset_send_memory()
        except Exception:
            log.debugWarning("Audio Processor: unable to reset time effects after tail cancel", exc_info=True)
    return True


class _GateHoldTailSession:
    """Keep the same Delay/Reverb DSP state alive with continuous silent PCM.

    No tail is reconstructed and no second WavePlayer is created. Each block
    advances the already-running Delay/Reverb buffers with zero input, and the
    resulting residual wet signal is fed back to the same WavePlayer. WavePlayer
    treats digital silence as normal PCM, which is all that is required to keep
    the stream alive while the effect memory decays.
    """

    def __init__(self, player, state, settings, includeDelay, includeReverb):
        self.player = player
        self.state = state
        self.settings = dict(settings)
        self.includeDelay = bool(includeDelay)
        self.includeReverb = bool(includeReverb)
        self.cancelEvent = threading.Event()
        self.sampleRate = max(1, int(state.sample_rate))
        self.blockFrames = max(64, min(1024, int(round(self.sampleRate * _GATE_HOLD_BLOCK_SECONDS))))
        self.primeFrames = max(32, min(self.blockFrames, int(round(self.sampleRate * _GATE_HOLD_PRIME_SECONDS))))
        self.hardFrames = max(
            self.blockFrames,
            int(round(_tailHardLimitSeconds(settings, state, self.includeDelay, self.includeReverb) * self.sampleRate)),
        )
        self.quietFramesNeeded = _tailQuietHoldFrames(settings, state, self.includeDelay, self.includeReverb)
        self.renderedFrames = 0
        self.quietFrames = 0
        self.thread = None

    def _renderAndFeed(self, frames):
        if self.cancelEvent.is_set() or frames <= 0:
            return False
        firstBlock = self.renderedFrames == 0
        procLock = getattr(self.player, "_nvdaAudioEQProcessingLock", None)
        try:
            if procLock is not None:
                with procLock:
                    if getattr(self.player, "_nvdaAudioEQState", None) is not self.state:
                        return False
                    data, peakRatio = self.state.render_gate_hold_chunk(
                        frames,
                        self.includeDelay,
                        self.includeReverb,
                    )
            else:
                if getattr(self.player, "_nvdaAudioEQState", None) is not self.state:
                    return False
                data, peakRatio = self.state.render_gate_hold_chunk(
                    frames,
                    self.includeDelay,
                    self.includeReverb,
                )
        except Exception:
            log.debugWarning("Audio Processor: gate-hold tail DSP failed", exc_info=True)
            return False
        if not data or self.cancelEvent.is_set():
            return False

        ioLock = getattr(self.player, "_nvdaAudioEQTailIoLock", None)
        if firstBlock and getattr(self.player, "_purpose", None) is nvwave.AudioPurpose.SPEECH:
            _tailDiag("idle-tail-first-feed-call", frames=int(frames))
        try:
            if ioLock is not None:
                with ioLock:
                    if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQTailSession", None) is not self:
                        return False
                    _originalFeed(self.player, data, None, None)
            else:
                if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQTailSession", None) is not self:
                    return False
                _originalFeed(self.player, data, None, None)
        except Exception:
            if not self.cancelEvent.is_set():
                log.debugWarning("Audio Processor: unable to feed gate-hold tail", exc_info=True)
            return False
        if firstBlock and getattr(self.player, "_purpose", None) is nvwave.AudioPurpose.SPEECH:
            _tailDiag("idle-tail-first-feed-return", frames=int(frames))

        self.renderedFrames += frames
        if peakRatio <= _GATE_HOLD_QUIET_RATIO:
            self.quietFrames += frames
        else:
            self.quietFrames = 0
        return True

    def prime(self):
        """Queue the first tiny block synchronously so the source/tail boundary stays continuous."""
        return self._renderAndFeed(min(self.primeFrames, self.hardFrames))

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            name="AudioProcessorGateHoldTail",
            daemon=True,
        )
        self.thread.start()

    def _run(self):
        try:
            while not self.cancelEvent.is_set() and self.renderedFrames < self.hardFrames:
                if self.quietFrames >= self.quietFramesNeeded:
                    break
                frames = min(self.blockFrames, self.hardFrames - self.renderedFrames)
                if not self._renderAndFeed(frames):
                    break
            if not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                try:
                    # Serialize the final native idle with cancellation/new-feed I/O.
                    # Recheck after taking the lock so a cancelled worker can never
                    # idle a WavePlayer that has already started a new utterance.
                    ioLock = getattr(self.player, "_nvdaAudioEQTailIoLock", None)
                    if ioLock is not None:
                        with ioLock:
                            if not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                                _originalIdle(self.player)
                    elif not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                        _originalIdle(self.player)
                except Exception:
                    log.debugWarning("Audio Processor: unable to idle after gate-hold tail", exc_info=True)
        finally:
            try:
                if getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                    self.player._nvdaAudioEQTailSession = None
                    procLock = getattr(self.player, "_nvdaAudioEQProcessingLock", None)
                    if procLock is not None:
                        with procLock:
                            if getattr(self.player, "_nvdaAudioEQState", None) is self.state:
                                _clearProcessorState(self.player)
                    elif getattr(self.player, "_nvdaAudioEQState", None) is self.state:
                        _clearProcessorState(self.player)
            except Exception:
                log.debugWarning("Audio Processor: gate-hold tail cleanup failed", exc_info=True)



class _SpeechPreIdleTailSession:
    """Bridge delayed synth idle calls using the real PCM playback boundary.

    Some speech drivers keep their WavePlayer logically "speaking" for a while
    after the last PCM block has already finished. Starting Gate Hold only from
    WavePlayer.idle() therefore leaves an audible dry gap before the wet memory
    resumes. This session starts when the newest real speech feed callback says
    that block has actually played. It keeps exactly the same Delay/Reverb state
    alive on the same WavePlayer.

    Only a two-block (5 ms each) rolling window is allowed. That is enough to keep
    WASAPI continuously fed, while ensuring that a synth which produces another
    speech chunk cannot get trapped behind a long pre-rendered reverb queue.
    """

    speechPreIdle = True

    def __init__(self, player, state, settings, includeDelay, includeReverb, feedSeq):
        self.player = player
        self.state = state
        self.settings = dict(settings)
        self.includeDelay = bool(includeDelay)
        self.includeReverb = bool(includeReverb)
        self.feedSeq = int(feedSeq)
        self.cancelEvent = threading.Event()
        self.sampleRate = max(1, int(state.sample_rate))
        self.blockFrames = max(32, min(512, int(round(self.sampleRate * _SPEECH_BRIDGE_BLOCK_SECONDS))))
        self.hardFrames = max(
            self.blockFrames,
            int(round(_tailHardLimitSeconds(settings, state, self.includeDelay, self.includeReverb) * self.sampleRate)),
        )
        self.quietFramesNeeded = _tailQuietHoldFrames(settings, state, self.includeDelay, self.includeReverb)
        self.renderedFrames = 0
        self.quietFrames = 0
        self.thread = None
        self._doneCond = threading.Condition()
        self._completedBlocks = 0
        self._outstandingBlocks = 0

    def _blockDone(self):
        with self._doneCond:
            self._completedBlocks += 1
            self._doneCond.notify_all()

    def _renderAndQueueOne(self):
        if self.cancelEvent.is_set() or self.renderedFrames >= self.hardFrames:
            return False
        if self.quietFrames >= self.quietFramesNeeded:
            return False
        frames = min(self.blockFrames, self.hardFrames - self.renderedFrames)
        firstBlock = self.renderedFrames == 0
        procLock = getattr(self.player, "_nvdaAudioEQProcessingLock", None)
        try:
            if procLock is not None:
                with procLock:
                    if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQState", None) is not self.state:
                        return False
                    data, peakRatio = self.state.render_gate_hold_chunk(
                        frames,
                        self.includeDelay,
                        self.includeReverb,
                    )
            else:
                if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQState", None) is not self.state:
                    return False
                data, peakRatio = self.state.render_gate_hold_chunk(
                    frames,
                    self.includeDelay,
                    self.includeReverb,
                )
        except Exception:
            log.debugWarning("Audio Processor: speech PCM bridge tail DSP failed", exc_info=True)
            return False
        if not data or self.cancelEvent.is_set():
            return False

        ioLock = getattr(self.player, "_nvdaAudioEQTailIoLock", None)
        if firstBlock:
            _tailDiag("bridge-first-feed-call", seq=int(self.feedSeq), frames=int(frames))
        try:
            if ioLock is not None:
                with ioLock:
                    if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQTailSession", None) is not self:
                        return False
                    _originalFeed(self.player, data, None, self._blockDone)
            else:
                if self.cancelEvent.is_set() or getattr(self.player, "_nvdaAudioEQTailSession", None) is not self:
                    return False
                _originalFeed(self.player, data, None, self._blockDone)
        except Exception:
            if not self.cancelEvent.is_set():
                log.debugWarning("Audio Processor: unable to feed speech PCM bridge tail", exc_info=True)
            return False
        if firstBlock:
            _tailDiag("bridge-first-feed-return", seq=int(self.feedSeq), frames=int(frames))

        self.renderedFrames += frames
        if peakRatio <= _GATE_HOLD_QUIET_RATIO:
            self.quietFrames += frames
        else:
            self.quietFrames = 0
        self._outstandingBlocks += 1
        return True

    def prime(self):
        """Synchronously queue the initial rolling window before starting the worker.

        The speech playback-completion callback arrives only after the final real
        PCM block has finished.  If the first wet continuation is produced by a
        newly scheduled Python thread, even a few milliseconds of scheduler
        latency become an audible reverb gap.  Pre-queue the same two 5 ms blocks
        used by the normal rolling window so the WavePlayer already owns roughly
        10 ms of continuation when this callback returns.
        """
        queued = 0
        while (
            not self.cancelEvent.is_set()
            and queued < _SPEECH_BRIDGE_QUEUE_BLOCKS
            and self._outstandingBlocks < _SPEECH_BRIDGE_QUEUE_BLOCKS
            and self.renderedFrames < self.hardFrames
            and self.quietFrames < self.quietFramesNeeded
        ):
            if not self._renderAndQueueOne():
                break
            queued += 1
        return queued > 0

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            name="AudioProcessorSpeechPcmBridgeTail",
            daemon=True,
        )
        self.thread.start()

    def _consumeCompleted(self, wait):
        with self._doneCond:
            if wait and self._completedBlocks <= 0 and not self.cancelEvent.is_set():
                # TEST38: the persistent dispatcher owns this session directly.
                # Wake frequently so a new speech feed can cancel a tail without
                # leaving the single dispatcher parked for 50 ms.
                self._doneCond.wait(timeout=0.010)
            done = self._completedBlocks
            self._completedBlocks = 0
        if done > 0:
            self._outstandingBlocks = max(0, self._outstandingBlocks - done)
        return done

    def _run(self):
        try:
            # Double-buffer the wet continuation: while one 5 ms block is playing,
            # the next one is already queued. This avoids Python scheduler holes,
            # but limits any speech latency after cancellation to roughly 10 ms.
            while (
                not self.cancelEvent.is_set()
                and self._outstandingBlocks < _SPEECH_BRIDGE_QUEUE_BLOCKS
                and self.renderedFrames < self.hardFrames
                and self.quietFrames < self.quietFramesNeeded
            ):
                if not self._renderAndQueueOne():
                    break

            while not self.cancelEvent.is_set():
                if self._outstandingBlocks <= 0:
                    break
                self._consumeCompleted(wait=True)
                if self.cancelEvent.is_set():
                    break
                while (
                    self._outstandingBlocks < _SPEECH_BRIDGE_QUEUE_BLOCKS
                    and self.renderedFrames < self.hardFrames
                    and self.quietFrames < self.quietFramesNeeded
                ):
                    if not self._renderAndQueueOne():
                        break
                if (
                    self.renderedFrames >= self.hardFrames
                    or self.quietFrames >= self.quietFramesNeeded
                ) and self._outstandingBlocks <= 0:
                    break

            while not self.cancelEvent.is_set() and self._outstandingBlocks > 0:
                self._consumeCompleted(wait=True)

            if not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                ioLock = getattr(self.player, "_nvdaAudioEQTailIoLock", None)
                try:
                    if ioLock is not None:
                        with ioLock:
                            if not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                                _originalIdle(self.player)
                    elif not self.cancelEvent.is_set() and getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                        _originalIdle(self.player)
                except Exception:
                    log.debugWarning("Audio Processor: unable to idle after speech PCM bridge tail", exc_info=True)
        finally:
            try:
                if getattr(self.player, "_nvdaAudioEQTailSession", None) is self:
                    self.player._nvdaAudioEQTailSession = None
                    procLock = getattr(self.player, "_nvdaAudioEQProcessingLock", None)
                    if procLock is not None:
                        with procLock:
                            if getattr(self.player, "_nvdaAudioEQState", None) is self.state:
                                _clearProcessorState(self.player)
                    elif getattr(self.player, "_nvdaAudioEQState", None) is self.state:
                        _clearProcessorState(self.player)
            except Exception:
                log.debugWarning("Audio Processor: speech PCM bridge tail cleanup failed", exc_info=True)



def _speechTailDispatchLoop():
    """Run speech-tail bridge starts away from nvwave's native completion callback.

    Only the newest pending job is retained. Rapid navigation therefore cannot
    build an unbounded queue of obsolete tail starts; sequence checks in
    _startSpeechPreIdleTail provide a second stale-job guard.
    """
    global _speechTailPendingJob
    while not _speechTailDispatchStop.is_set():
        _speechTailDispatchEvent.wait(timeout=0.250)
        if _speechTailDispatchStop.is_set():
            break
        job = None
        with _speechTailDispatchLock:
            job = _speechTailPendingJob
            _speechTailPendingJob = None
            _speechTailDispatchEvent.clear()
        if job is None:
            continue
        player, state, settings, feedSeq = job
        try:
            _startSpeechPreIdleTail(player, state, settings, feedSeq)
        except Exception:
            log.debugWarning("Audio Processor: speech tail dispatcher failed", exc_info=True)


def _startSpeechTailDispatcher():
    global _speechTailDispatchThread
    if _speechTailDispatchThread is not None and _speechTailDispatchThread.is_alive():
        return
    _speechTailDispatchStop.clear()
    _speechTailDispatchEvent.clear()
    _speechTailDispatchThread = threading.Thread(
        target=_speechTailDispatchLoop,
        name="AudioProcessorSpeechTailDispatcher",
        daemon=True,
    )
    _speechTailDispatchThread.start()


def _stopSpeechTailDispatcher():
    global _speechTailPendingJob, _speechTailDispatchThread
    _speechTailDispatchStop.set()
    with _speechTailDispatchLock:
        _speechTailPendingJob = None
        _speechTailDispatchEvent.set()
    # Do not join here: a native WavePlayer.feed may legitimately be waiting for
    # buffer space.  The daemon will observe the stop flag as soon as it returns.
    _speechTailDispatchThread = None


def _queueSpeechPreIdleTail(player, state, settings, feedSeq):
    """Publish the newest bridge request without doing audio I/O in onDone."""
    _tailDiag("bridge-request", seq=int(feedSeq))
    global _speechTailPendingJob
    if _speechTailDispatchStop.is_set():
        return False
    with _speechTailDispatchLock:
        _speechTailPendingJob = (player, state, settings, int(feedSeq))
        _speechTailDispatchEvent.set()
    return True


def _startSpeechPreIdleTail(player, state, settings, feedSeq):
    _tailDiag("bridge-start", seq=int(feedSeq))
    if _originalFeed is None or _originalIdle is None or state is None:
        return False
    if getattr(player, "_purpose", None) is not nvwave.AudioPurpose.SPEECH:
        return False
    includeDelay, includeReverb = _tailSelection(settings, state)
    if not includeDelay and not includeReverb:
        return False
    if int(getattr(state, "output_channels", 0) or 0) < 2:
        return False
    feedLock = getattr(player, "_nvdaAudioEQSpeechFeedLock", None)
    if feedLock is None:
        return False
    with feedLock:
        if int(getattr(player, "_nvdaAudioEQSpeechFeedSeq", 0) or 0) != int(feedSeq):
            return False
        if getattr(player, "_nvdaAudioEQTailSession", None) is not None:
            return False
        if getattr(player, "_nvdaAudioEQState", None) is not state:
            return False
        session = _SpeechPreIdleTailSession(
            player, state, settings, includeDelay, includeReverb, feedSeq
        )
        player._nvdaAudioEQTailSession = session

    # TEST36: this runs on the persistent dispatcher, never inside nvwave's
    # playback-completion callback.  Prime the same ~10 ms rolling window here,
    # outside the speech-feed lock, so new source PCM is never forced to wait on
    # a potentially blocking native WavePlayer.feed().
    if not session.prime():
        if getattr(player, "_nvdaAudioEQTailSession", None) is session:
            player._nvdaAudioEQTailSession = None
        return False
    if session.cancelEvent.is_set() or getattr(player, "_nvdaAudioEQTailSession", None) is not session:
        return False

    # TEST38: do not create AudioProcessorSpeechPcmBridgeTail threads per
    # utterance. _startSpeechPreIdleTail is itself called by the one persistent
    # AudioProcessorSpeechTailDispatcher, so let that same daemon own the rolling
    # bridge until it decays or a new speech feed cancels it. This keeps rapid
    # character/word navigation bounded to one tail worker instead of creating
    # and cancelling a Python thread for every short utterance.
    session._run()
    return True


def _onRealSpeechBlockPlayed(player, feedSeq):
    """Start Gate Hold from actual playback completion, not delayed synth idle."""
    _tailDiag("source-onDone", seq=int(feedSeq))
    try:
        feedLock = getattr(player, "_nvdaAudioEQSpeechFeedLock", None)
        if feedLock is None:
            return
        with feedLock:
            if int(getattr(player, "_nvdaAudioEQSpeechFeedSeq", 0) or 0) != int(feedSeq):
                return
            if getattr(player, "_nvdaAudioEQTailSession", None) is not None:
                return
            settings, _generation = _getSettingsSnapshot()
            state = getattr(player, "_nvdaAudioEQState", None)
            if state is None or not _shouldProcess(player, settings):
                return
        # TEST36: never feed WavePlayer re-entrantly from its native onDone
        # callback. The already-running dispatcher wakes and performs the prime.
        _queueSpeechPreIdleTail(player, state, settings, feedSeq)
    except Exception:
        log.debugWarning("Audio Processor: unable to queue speech PCM bridge tail", exc_info=True)

def _startGateHoldTail(player, state, settings):
    _tailDiag("idle-tail-start")
    if _originalFeed is None or _originalIdle is None or state is None:
        return False
    includeDelay, includeReverb = _tailSelection(settings, state)
    if not includeDelay and not includeReverb:
        return False
    if int(getattr(state, "output_channels", 0) or 0) < 2:
        return False
    _cancelGateHoldTail(player, stopAudio=False, resetSendMemory=False)
    session = _GateHoldTailSession(player, state, settings, includeDelay, includeReverb)
    player._nvdaAudioEQTailSession = session
    if not session.prime():
        if getattr(player, "_nvdaAudioEQTailSession", None) is session:
            player._nvdaAudioEQTailSession = None
        return False
    session.start()
    return True



def _feedNativeSerialized(player, data, size=None, onDone=None):
    """Call the original WavePlayer.feed under the same I/O lock as tail feeds.

    TEST38 prevents source PCM and a cancelling/finishing tail from entering the
    native WavePlayer concurrently. The processing lock is deliberately not held
    here: only the short native feed operation is serialized.
    """
    original = _originalFeed
    if original is None:
        return None
    ioLock = getattr(player, "_nvdaAudioEQTailIoLock", None)
    if ioLock is not None:
        with ioLock:
            return original(player, data, size, onDone)
    return original(player, data, size, onDone)



def _speechMixerEvent(player, event, **fields):
    """Handle only mixer lifecycle events required by the runtime.

    TEST59 deliberately drops TEST52-55 rolling benchmark telemetry.  The mixer
    callback remains only because boundary completion owns processor-state
    cleanup and mixer errors must still reach the NVDA log.
    """
    if event == 'boundaryComplete':
        _clearProcessorState(player)
        return
    if event == 'error':
        log.debugWarning('Audio Processor TEST59 speech FIFO mixer error: %s', fields.get('error'))

def _renderSpeechMixerChunk(player, raw, context):
    """Render one fixed source-aligned FIFO block through the existing DSP state."""
    global _processedChunks, _processedBytes, _lastProcessState
    settings = context['settings']
    generation = int(context['generation'])
    sampleRate = int(context['sampleRate'])
    sourceChannels = int(context['sourceChannels'])
    outputChannels = int(context['outputChannels'])
    bits = int(context['bits'])
    procLock = getattr(player, '_nvdaAudioEQProcessingLock', None)
    if procLock is None:
        procLock = threading.RLock()
        player._nvdaAudioEQProcessingLock = procLock
    with procLock:
        state = getattr(player, '_nvdaAudioEQState', None)
        sameFormat = (
            state is not None
            and state.sample_rate == sampleRate
            and state.source_channels == sourceChannels
            and state.output_channels == outputChannels
            and state.bits == bits
        )
        if state is None or not sameFormat:
            state = StreamProcessor(
                sampleRate,
                sourceChannels,
                bits,
                settings,
                generation,
                output_channels=outputChannels,
            )
            try:
                memory = getattr(player, '_nvdaAudioEQPanLfoMemory')
                phase, hold, rng = memory[:3]
                state.pan_lfo_phase = float(phase) % 1.0
                state.pan_lfo_hold = float(hold)
                state.pan_lfo_rng = int(rng) & 0xFFFFFFFF
                if len(memory) >= 4:
                    state.pan_lfo_smoothed = float(memory[3])
            except Exception:
                pass
            player._nvdaAudioEQState = state
        elif state.generation != generation:
            state = state.reconfigured(settings, generation)
            player._nvdaAudioEQState = state
        _applyPendingSharedLfoTriggerToPlayer(player, settings)
        processed, _unusedWet = state.process_split(raw)
    _processedChunks += 1
    _processedBytes += len(raw)
    _lastProcessState = _('processed through TEST55 speech FIFO mixer')
    return processed



def _speechMixerTailRequested(settings):
    """Cheap source-thread check used only to choose sync vs async idle semantics."""
    try:
        return bool(
            (settings.get('delayTailEnabled', False) and _delayIsAudible(settings))
            or (settings.get('reverbTailEnabled', False) and _reverbIsAudible(settings))
        )
    except Exception:
        return False


def _beginSpeechMixerTail(player):
    """Create a TEST54 tail plan only after all preceding FIFO source PCM is rendered."""
    settings, _generation = _getSettingsSnapshot()
    if not _shouldProcess(player, settings):
        return None
    procLock = getattr(player, '_nvdaAudioEQProcessingLock', None)
    if procLock is None:
        return None
    with procLock:
        state = getattr(player, '_nvdaAudioEQState', None)
        if state is None:
            return None
        includeDelay, includeReverb = _tailSelection(settings, state)
        if not includeDelay and not includeReverb:
            return None
        if int(getattr(state, 'output_channels', 0) or 0) < 2:
            return None
        sampleRate = max(1, int(state.sample_rate))
        blockFrames = max(1, int(round(sampleRate * _SPEECH_MIXER_BLOCK_SECONDS)))
        hardFrames = max(
            blockFrames,
            int(round(_tailHardLimitSeconds(settings, state, includeDelay, includeReverb) * sampleRate)),
        )
        quietFramesNeeded = _tailQuietHoldFrames(settings, state, includeDelay, includeReverb)
        return {
            'state': state,
            'settings': dict(settings),
            'includeDelay': bool(includeDelay),
            'includeReverb': bool(includeReverb),
            'sampleRate': sampleRate,
            'blockFrames': blockFrames,
            'hardFrames': hardFrames,
            'quietFramesNeeded': quietFramesNeeded,
            'renderedFrames': 0,
            'quietFrames': 0,
        }


def _renderSpeechMixerTail(player, plan):
    """Render one wet-only TEST54 FIFO block from the exact live Delay/Reverb state."""
    state = plan.get('state')
    if state is None:
        return None
    rendered = int(plan.get('renderedFrames', 0) or 0)
    hardFrames = int(plan.get('hardFrames', 0) or 0)
    quiet = int(plan.get('quietFrames', 0) or 0)
    quietNeeded = int(plan.get('quietFramesNeeded', 0) or 0)
    if rendered >= hardFrames or quiet >= quietNeeded:
        return None
    frames = min(int(plan.get('blockFrames', 1) or 1), hardFrames - rendered)
    procLock = getattr(player, '_nvdaAudioEQProcessingLock', None)
    if procLock is None:
        return None
    with procLock:
        if getattr(player, '_nvdaAudioEQState', None) is not state:
            return None
        data, peakRatio = state.render_gate_hold_chunk(
            frames,
            bool(plan.get('includeDelay', False)),
            bool(plan.get('includeReverb', False)),
        )
    if not data:
        return None
    rendered += frames
    if peakRatio <= _GATE_HOLD_QUIET_RATIO:
        quiet += frames
    else:
        quiet = 0
    plan['renderedFrames'] = rendered
    plan['quietFrames'] = quiet
    done = rendered >= hardFrames or quiet >= quietNeeded
    pcmMs = (frames / float(max(1, int(plan.get('sampleRate', 1) or 1)))) * 1000.0
    return data, pcmMs, bool(done), float(peakRatio)


def _getOrCreateSpeechMixer(player):
    mixer = getattr(player, '_nvdaAudioEQSpeechMixer', None)
    if mixer is not None:
        return mixer
    if _originalFeed is None or _originalIdle is None or _originalStop is None:
        return None
    mixer = _mixer.SpeechFifoMixer(
        player=player,
        renderChunk=_renderSpeechMixerChunk,
        nativeFeed=_originalFeed,
        nativeIdle=_originalIdle,
        nativeStop=_originalStop,
        eventCallback=lambda event, player=player, **fields: _speechMixerEvent(player, event, **fields),
        beginTail=_beginSpeechMixerTail,
        renderTail=_renderSpeechMixerTail,
        blockSeconds=_SPEECH_MIXER_BLOCK_SECONDS,
    )
    player._nvdaAudioEQSpeechMixer = mixer
    return mixer


def _closeSpeechMixer(player):
    mixer = getattr(player, '_nvdaAudioEQSpeechMixer', None)
    if mixer is None:
        return
    try:
        mixer.close()
    except Exception:
        log.debugWarning('Audio Processor: unable to close TEST53 speech FIFO mixer', exc_info=True)
    try:
        player._nvdaAudioEQSpeechMixer = None
        player._nvdaAudioEQSpeechMixerUsedSinceIdle = False
    except Exception:
        pass


def _patchedFeed(player, data, size=None, onDone=None):
    global _lastStreamInfo, _processedChunks, _processedBytes, _lastProcessState
    sourcePcmMs = 0.0
    speechFeedSeq = None
    isSpeech = getattr(player, '_purpose', None) is nvwave.AudioPurpose.SPEECH
    if isSpeech:
        # TEST61 startup compatibility: NVDA creates the active speech WavePlayer
        # before global plugins are loaded. Such an already-existing player has
        # never passed through our patched __init__, so the speech bookkeeping
        # attributes introduced by Audio Processor may be missing until the user
        # changes synthesizer. Initialize only the missing runtime state lazily;
        # newly-created players keep the exact existing path.
        feedLock = getattr(player, '_nvdaAudioEQSpeechFeedLock', None)
        if feedLock is None:
            feedLock = threading.RLock()
            player._nvdaAudioEQSpeechFeedLock = feedLock
            player._nvdaAudioEQSpeechFeedSeq = int(
                getattr(player, '_nvdaAudioEQSpeechFeedSeq', 0) or 0
            )
            if not hasattr(player, '_nvdaAudioEQSpeechMixer'):
                player._nvdaAudioEQSpeechMixer = None
            if not hasattr(player, '_nvdaAudioEQSpeechMixerUsedSinceIdle'):
                player._nvdaAudioEQSpeechMixerUsedSinceIdle = False
            if not hasattr(player, '_nvdaAudioEQSpeechPcmSinceIdleMs'):
                player._nvdaAudioEQSpeechPcmSinceIdleMs = 0.0
        with feedLock:
            player._nvdaAudioEQSpeechFeedSeq = int(
                getattr(player, '_nvdaAudioEQSpeechFeedSeq', 0) or 0
            ) + 1
            speechFeedSeq = player._nvdaAudioEQSpeechFeedSeq
        _tailDiag('feed-enter', seq=int(speechFeedSeq))

    # Any legacy TEST51 tail is cancelled before new source PCM. TEST53 never
    # starts a new speech tail on the FIFO path; Delay/Reverb tails are purposely
    # excluded until ordinary FIFO speech has proved stable.
    _cancelGateHoldTail(player, stopAudio=True, resetSendMemory=False)
    original = _originalFeed
    if original is None:
        return None
    raw = None
    bits = 0
    sourceChannels = 0
    outputChannels = 0
    try:
        settings, generation = _getSettingsSnapshot()

        if bool(settings.get('audioDeviceLock', False)):
            available, lockedDeviceId, _lockedDeviceName = _lockedAudioDeviceStatus(settings, forceRefresh=False)
            playerDevice = str(getattr(player, '_nvdaAudioEQOutputDevice', '') or '')
            if not available:
                _lastProcessState = _('locked audio device unavailable; audio suppressed')
                return None
            if playerDevice != lockedDeviceId:
                _lastProcessState = _('waiting for locked audio device routing')
                return None

        bits = int(getattr(player, 'bitsPerSample', 0) or 0)
        outputChannels = int(getattr(player, 'channels', 0) or 0)
        sourceChannels = int(getattr(player, '_nvdaAudioEQSourceChannels', outputChannels) or outputChannels)
        sampleRate = int(getattr(player, 'samplesPerSec', 0) or 0)
        supported = (
            bits in (8, 16, 24, 32)
            and sourceChannels > 0
            and outputChannels > 0
            and sampleRate > 0
        )
        if sourceChannels != outputChannels:
            channelText = _('{source}→{output} channels').format(source=sourceChannels, output=outputChannels)
        else:
            channelText = _('{channels} channel').format(channels=sourceChannels) if sourceChannels == 1 else _('{channels} channels').format(channels=sourceChannels)
        _lastStreamInfo = _('Last stream: {purpose}, {sampleRate} Hz, {bits} bit, {channels}.').format(
            purpose=_purposeName(player), sampleRate=sampleRate, bits=bits, channels=channelText
        )
        if not supported:
            _lastProcessState = _('unsupported format')
            return _feedNativeSerialized(player, data, size, onDone)

        raw = _extractRawData(data, size)
        if raw is None:
            _lastProcessState = _('unreadable buffer')
            return _feedNativeSerialized(player, data, size, onDone)
        sourceBytesPerFrame = max(1, sourceChannels * max(1, bits // 8))
        sourcePcmMs = (len(raw) / float(sourceBytesPerFrame * sampleRate)) * 1000.0
        if speechFeedSeq is not None:
            bytesPerFrame = max(1, outputChannels * max(1, bits // 8))
            pcmMs = (len(raw) / float(bytesPerFrame * sampleRate)) * 1000.0
            _tailDiag('feed-pcm', seq=int(speechFeedSeq), bytes=len(raw), pcmMs=f'{pcmMs:.3f}')

        shouldProcess = _shouldProcess(player, settings)
        if not shouldProcess:
            if sourceChannels == 1 and outputChannels == 2:
                passthrough = _promoteMonoToStereo(raw, bits)
                _lastProcessState = _('excluded by Process selector, neutral mono→stereo')
                return _feedNativeSerialized(player, passthrough, None, onDone)
            _lastProcessState = _('excluded by Process selector')
            return _feedNativeSerialized(player, data, size, onDone)

        # TEST54: selected speech uses the FIFO master path. Capture all settings
        # and raw PCM now (raw is already an owned bytes object), then return to
        # the synth immediately. The worker performs fixed ~20 ms DSP/native feed.
        if isSpeech:
            callback = onDone
            context = {
                'settings': dict(settings),
                'generation': int(generation),
                'sampleRate': int(sampleRate),
                'sourceChannels': int(sourceChannels),
                'outputChannels': int(outputChannels),
                'bits': int(bits),
                'speechFeedSeq': None if speechFeedSeq is None else int(speechFeedSeq),
            }
            speechMixer = _getOrCreateSpeechMixer(player)
            if speechMixer is not None and speechMixer.enqueue(raw, context, callback):
                player._nvdaAudioEQSpeechMixerUsedSinceIdle = True
                try:
                    player._nvdaAudioEQSpeechPcmSinceIdleMs = (
                        float(getattr(player, '_nvdaAudioEQSpeechPcmSinceIdleMs', 0.0) or 0.0)
                        + max(0.0, float(sourcePcmMs))
                    )
                except Exception:
                    pass
                _lastProcessState = _('queued in speech FIFO mixer')
                return None
            # If mixer creation/enqueue failed, preserve speech rather than silence.
            log.debugWarning('Audio Processor: speech FIFO unavailable; using direct path')

        # TEST52 direct path remains unchanged for Sounds and emergency speech
        # fallback. Speech tails are generated only by the TEST54 FIFO worker.
        procLock = getattr(player, '_nvdaAudioEQProcessingLock', None)
        if procLock is None:
            procLock = threading.RLock()
            player._nvdaAudioEQProcessingLock = procLock
        with procLock:
            state = getattr(player, '_nvdaAudioEQState', None)
            sameFormat = (
                state is not None
                and state.sample_rate == sampleRate
                and state.source_channels == sourceChannels
                and state.output_channels == outputChannels
                and state.bits == bits
            )
            if state is None or not sameFormat:
                state = StreamProcessor(sampleRate, sourceChannels, bits, settings, generation, output_channels=outputChannels)
                try:
                    memory = getattr(player, '_nvdaAudioEQPanLfoMemory')
                    phase, hold, rng = memory[:3]
                    state.pan_lfo_phase = float(phase) % 1.0
                    state.pan_lfo_hold = float(hold)
                    state.pan_lfo_rng = int(rng) & 0xFFFFFFFF
                    if len(memory) >= 4:
                        state.pan_lfo_smoothed = float(memory[3])
                except Exception:
                    pass
                player._nvdaAudioEQState = state
            elif state.generation != generation:
                state = state.reconfigured(settings, generation)
                player._nvdaAudioEQState = state
            _applyPendingSharedLfoTriggerToPlayer(player, settings)
            processed, _unusedWet = state.process_split(raw)
        _processedChunks += 1
        _processedBytes += len(raw)
        _lastProcessState = _('processed')

        return _feedNativeSerialized(player, processed, None, onDone)
    except Exception:
        _lastProcessState = _('DSP error, neutral audio')
        log.debugWarning('Audio Processor: DSP/FIFO error; attempting neutral audio passthrough', exc_info=True)
        try:
            if raw is None:
                raw = _extractRawData(data, size)
            if raw is not None and sourceChannels == 1 and outputChannels == 2 and bits in (8, 16, 24, 32):
                return _feedNativeSerialized(player, _promoteMonoToStereo(raw, bits), None, onDone)
        except Exception:
            pass
        return _feedNativeSerialized(player, data, size, onDone)


def _clearProcessorState(player):
    try:
        state = getattr(player, "_nvdaAudioEQState", None)
        if state is not None:
            try:
                player._nvdaAudioEQPanLfoMemory = (
                    state.pan_lfo_phase, state.pan_lfo_hold, state.pan_lfo_rng,
                    state.pan_lfo_smoothed,
                )
            except Exception:
                pass
            delattr(player, "_nvdaAudioEQState")
    except Exception:
        pass



def _patchedIdle(player):
    """TEST54: speech tails are an in-order continuation owned by the FIFO worker."""
    if getattr(player, '_purpose', None) is nvwave.AudioPurpose.SPEECH:
        _tailDiag('idle-enter')
        mixer = getattr(player, '_nvdaAudioEQSpeechMixer', None)
        used = bool(getattr(player, '_nvdaAudioEQSpeechMixerUsedSinceIdle', False))
        if mixer is not None and used:
            settings, _generation = _getSettingsSnapshot()
            try:
                utterancePcmMs = max(0.0, float(getattr(player, '_nvdaAudioEQSpeechPcmSinceIdleMs', 0.0) or 0.0))
            except Exception:
                utterancePcmMs = 0.0
            player._nvdaAudioEQSpeechPcmSinceIdleMs = 0.0
            tailRequested = _speechMixerTailRequested(settings) and _shouldProcess(player, settings)
            synthName = _currentSynthName().strip().lower()
            if (
                tailRequested
                and synthName == 'ibmeci'
                and 0.0 < utterancePcmMs <= _IBMECI_SHORT_TAIL_MAX_PCM_MS
            ):
                tailRequested = False
            try:
                mixer.idle(tailRequested=tailRequested)
                return None
            finally:
                player._nvdaAudioEQSpeechMixerUsedSinceIdle = False
                # State cleanup happens on mixerBoundaryComplete.  A tail-enabled
                # idle returns early, and a source feed may supersede that boundary,
                # in which case preserving this exact state is intentional.
        original = _originalIdle
        if original is None:
            return None
        try:
            return original(player)
        finally:
            _clearProcessorState(player)

    # Sounds retain the established TEST51 Gate Hold path.
    original = _originalIdle
    if original is None:
        return None
    try:
        activeTail = getattr(player, '_nvdaAudioEQTailSession', None)
        if activeTail is not None and getattr(activeTail, 'speechPreIdle', False):
            return None
        settings, _generation = _getSettingsSnapshot()
        state = getattr(player, '_nvdaAudioEQState', None)
        if state is not None and _shouldProcess(player, settings):
            if _startGateHoldTail(player, state, settings):
                return None
        return original(player)
    finally:
        if getattr(player, '_nvdaAudioEQTailSession', None) is None:
            _clearProcessorState(player)


def _patchedStop(player):
    if getattr(player, '_purpose', None) is nvwave.AudioPurpose.SPEECH:
        _tailDiag('stop-enter')
        # TEST54 explicit stop is a hard interruption: invalidate both source FIFO
        # and any integrated wet continuation, flush the native player immediately,
        # then discard DSP memory. New source arriving *without* explicit stop can
        # still supersede a running tail while preserving the same time-FX state.
        _cancelGateHoldTail(player, stopAudio=False, resetSendMemory=False)
        mixer = getattr(player, '_nvdaAudioEQSpeechMixer', None)
        try:
            if mixer is not None:
                mixer.stop()
            elif _originalStop is not None:
                _originalStop(player)
        finally:
            player._nvdaAudioEQSpeechMixerUsedSinceIdle = False
            player._nvdaAudioEQSpeechPcmSinceIdleMs = 0.0
            _clearProcessorState(player)
        return None

    original = _originalStop
    if original is None:
        return None
    if getattr(player, '_nvdaAudioEQTailSession', None) is not None:
        _cancelGateHoldTail(player, stopAudio=True, resetSendMemory=False)
        _clearProcessorState(player)
        return None
    settings, _generation = _getSettingsSnapshot()
    state = getattr(player, '_nvdaAudioEQState', None)
    try:
        original(player)
    except Exception:
        _clearProcessorState(player)
        raise
    if state is not None and _shouldProcess(player, settings):
        if _startGateHoldTail(player, state, settings):
            return None
    _clearProcessorState(player)
    return None


def _initialSharedLfoMemory():
    """Return the deterministic phase/S&H state used by a freshly triggered LFO."""
    rng = 0x13579BDF
    rng = (1664525 * rng + 1013904223) & 0xFFFFFFFF
    hold = (rng / 4294967295.0) * 2.0 - 1.0
    return (0.0, hold, rng, 0.0)


def _requestSharedLfoTrigger():
    """Arm one shared-LFO restart for the next speech boundary.

    Trigger requests come from either manual keyboard input or a real NVDA
    object change. Multiple requests before the same speech boundary collapse
    naturally to one phase reset because pre_synthSpeak consumes only the latest
    serial.
    """
    global _lfoTriggerRequested
    try:
        currentSettings, _generation = _getSettingsSnapshot()
        if str(currentSettings.get("lfoMode", "Free")) != "Trigger":
            return
        _lfoTriggerRequested += 1
    except Exception:
        log.debugWarning("Audio Processor: unable to arm shared LFO trigger", exc_info=True)


def _gestureIsKeyboardInput(gesture):
    """Return True for a real/non-modifier keyboard gesture.

    NVDA routes bound commands and ordinary pass-through keys through
    InputManager.executeGesture, so this catches arrows in text, navigator keys,
    NVDA+Tab, typed characters and other keyboard actions without treating
    modifier-only presses as new LFO triggers.
    """
    try:
        if bool(getattr(gesture, "isModifier", False)):
            return False
        source = str(getattr(gesture, "source", "") or "").lower()
        if source == "kb" or source.startswith("keyboard"):
            return True
        for identifier in getattr(gesture, "identifiers", ()) or ():
            text = str(identifier).lower()
            if text.startswith("kb:") or text.startswith("kb("):
                return True
    except Exception:
        pass
    return False


def _onInputGestureForLfoTrigger(gesture):
    """Arm Trigger once for each manual keyboard action, never block the gesture.

    Say All is therefore triggered once by the key command that starts it; its
    subsequent automatically generated speech does not retrigger the LFO unless
    a real focus/navigator-object change occurs.
    """
    try:
        if _gestureIsKeyboardInput(gesture):
            _requestSharedLfoTrigger()
    except Exception:
        log.debugWarning("Audio Processor: unable to process keyboard LFO trigger", exc_info=True)
    return True


def _applyPendingSharedLfoTriggerToPlayer(player, settings):
    """Apply the latest pending trigger to one speech WavePlayer exactly once."""
    if getattr(player, "_purpose", None) is not nvwave.AudioPurpose.SPEECH:
        return
    if str(settings.get("lfoMode", "Free")) != "Trigger":
        return
    serial = int(_lfoTriggerActive)
    if serial <= 0 or int(getattr(player, "_nvdaAudioEQLfoTriggerSerial", -1)) == serial:
        return
    state = getattr(player, "_nvdaAudioEQState", None)
    if state is not None:
        state.reset_shared_lfo()
    # idle()/stop() may already have discarded the StreamProcessor. Reset the
    # saved continuity memory too, so a newly created state still starts at phase 0.
    player._nvdaAudioEQPanLfoMemory = _initialSharedLfoMemory()
    player._nvdaAudioEQLfoTriggerSerial = serial


def _resetSpeechProcessorStateOnPreSpeak(speechSequence=None, **kwargs):
    """Consume only the intentional shared-LFO trigger at a speech boundary.

    TEST23 deliberately does *not* treat an NVDA utterance as a Delay/Reverb
    trigger.  The current Chorus/Delay/Reverb memories and any running Gate Hold
    are left untouched here.  They continue to decay through silence until the
    first real PCM block of the next utterance arrives, at which point _patchedFeed
    stops only the tail feeder and injects the new audio into the same DSP state.

    LFO Mode Trigger remains independent and keeps its existing keyboard/focus
    semantics; only the time-effect utterance reset has been removed.
    """
    _tailDiagBeginSpeech(speechSequence)
    global _lfoTriggerActive
    try:
        currentSettings, _generation = _getSettingsSnapshot()
        triggerLfo = str(currentSettings.get("lfoMode", "Free")) == "Trigger"
        if triggerLfo and _lfoTriggerRequested != _lfoTriggerActive:
            _lfoTriggerActive = _lfoTriggerRequested
        if triggerLfo:
            for candidate in list(nvwave.WavePlayer._instances.values()):
                if getattr(candidate, "_purpose", None) is not nvwave.AudioPurpose.SPEECH:
                    continue
                _applyPendingSharedLfoTriggerToPlayer(candidate, currentSettings)
    except Exception:
        log.debugWarning("Audio Processor: unable to prepare speech DSP at utterance boundary", exc_info=True)


def settingsNeedStereoForPurpose(settings, purpose):
    return _settingsNeedStereoForPurpose(settings, purpose)

def requestSharedLfoTrigger():
    return _requestSharedLfoTrigger()

def tailDiagReport():
    return _tailDiagReport()

def getLastStreamInfo():
    return _lastStreamInfo

def getRuntimeStatus():
    return (_lastStreamInfo, _lastProcessState, int(_processedChunks), int(_processedBytes))

def isInstalled():
    return _originalFeed is not None

def install(settingsGetter):
    """Own the complete nvwave interception lifecycle for Audio Processor."""
    global _originalFeed, _originalInit, _originalIdle, _originalStop
    configure(settingsGetter)
    _startSpeechTailDispatcher()
    if _originalInit is None:
        _originalInit = nvwave.WavePlayer.__init__
        nvwave.WavePlayer.__init__ = _patchedInit
    if _originalFeed is None:
        _originalFeed = nvwave.WavePlayer.feed
        nvwave.WavePlayer.feed = _patchedFeed
    if _originalIdle is None:
        _originalIdle = nvwave.WavePlayer.idle
        nvwave.WavePlayer.idle = _patchedIdle
    if _originalStop is None:
        _originalStop = nvwave.WavePlayer.stop
        nvwave.WavePlayer.stop = _patchedStop
    try:
        pre_synthSpeak.register(_resetSpeechProcessorStateOnPreSpeak)
    except Exception:
        log.debugWarning("Audio Processor: unable to register utterance reset hook", exc_info=True)
    try:
        inputCore.decide_executeGesture.register(_onInputGestureForLfoTrigger)
    except Exception:
        log.debugWarning("Audio Processor: unable to register keyboard LFO trigger hook", exc_info=True)

def uninstall():
    """Cancel tails, unregister hooks and restore NVDA's original WavePlayer."""
    global _originalFeed, _originalInit, _originalIdle, _originalStop
    _stopSpeechTailDispatcher()
    try:
        for player in list(nvwave.WavePlayer._instances.values()):
            _closeSpeechMixer(player)
            _cancelGateHoldTail(player, stopAudio=True, resetSendMemory=False)
    except Exception:
        log.debugWarning("Audio Processor: unable to cancel gate-hold tails during unload", exc_info=True)
    try:
        pre_synthSpeak.unregister(_resetSpeechProcessorStateOnPreSpeak)
    except Exception:
        pass
    try:
        inputCore.decide_executeGesture.unregister(_onInputGestureForLfoTrigger)
    except Exception:
        pass
    if _originalFeed is not None and nvwave.WavePlayer.feed is _patchedFeed:
        nvwave.WavePlayer.feed = _originalFeed
    if _originalInit is not None and nvwave.WavePlayer.__init__ is _patchedInit:
        nvwave.WavePlayer.__init__ = _originalInit
    if _originalIdle is not None and nvwave.WavePlayer.idle is _patchedIdle:
        nvwave.WavePlayer.idle = _originalIdle
    if _originalStop is not None and nvwave.WavePlayer.stop is _patchedStop:
        nvwave.WavePlayer.stop = _originalStop
    _originalFeed = None
    _originalInit = None
    _originalIdle = None
    _originalStop = None
