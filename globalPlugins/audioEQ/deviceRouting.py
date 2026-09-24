from __future__ import annotations

import threading
import time

import addonHandler
from logHandler import log
from utils import mmdevice

addonHandler.initTranslation()

_AUDIO_DEVICE_CACHE_SECONDS = 0.5
_audioDeviceCacheLock = threading.RLock()
_audioDeviceCacheTime = 0.0
_audioDeviceCache = []


def getAudioOutputDevices(forceRefresh=False):
    """Return active WASAPI/MMDevice output endpoints as ``(id, name)`` pairs.

    NVDA's own Audio settings panel uses ``utils.mmdevice.getOutputDevices``.
    Keep a very short cache because audio routing checks can occur many times per
    second, while device enumeration itself may take a few milliseconds.
    """
    global _audioDeviceCacheTime, _audioDeviceCache
    now = time.monotonic()
    with _audioDeviceCacheLock:
        if (
            not forceRefresh
            and _audioDeviceCache
            and now - _audioDeviceCacheTime < _AUDIO_DEVICE_CACHE_SECONDS
        ):
            return list(_audioDeviceCache)
    devices = []
    try:
        seen = set()
        for device in mmdevice.getOutputDevices(includeDefault=False):
            deviceId = str(device.id)
            deviceName = str(device.friendlyName)
            if not deviceId or deviceId in seen:
                continue
            seen.add(deviceId)
            devices.append((deviceId, deviceName))
    except Exception:
        log.debugWarning("Audio Processor: unable to enumerate audio output devices", exc_info=True)
    with _audioDeviceCacheLock:
        _audioDeviceCache = list(devices)
        _audioDeviceCacheTime = now
    return devices


def lockedAudioDeviceStatus(settings, forceRefresh=False):
    """Return ``(available, id, displayName)`` for a locked routing state.

    An unlocked state is always valid. A locked state is valid only when the
    exact persisted MMDevice endpoint ID is currently active.
    """
    if not bool(settings.get("audioDeviceLock", False)):
        return True, "", ""
    deviceId = str(settings.get("audioDeviceId", "") or "")
    savedName = str(settings.get("audioDeviceName", "") or "")
    if not deviceId:
        return False, "", savedName or _("unknown audio device")
    for currentId, currentName in getAudioOutputDevices(forceRefresh=forceRefresh):
        if currentId == deviceId:
            return True, deviceId, currentName
    return False, deviceId, savedName or deviceId


def settingsAudioRoutingKey(settings):
    if bool(settings.get("audioDeviceLock", False)):
        return ("locked", str(settings.get("audioDeviceId", "") or ""))
    return ("nvda", "")


def audioRoutingChanged(oldSettings, newSettings):
    return settingsAudioRoutingKey(oldSettings) != settingsAudioRoutingKey(newSettings)


def reloadCurrentSynth():
    """Recreate the current speech synth so its WavePlayer is constructed again."""
    try:
        import synthDriverHandler
        synth = synthDriverHandler.getSynth()
        if synth is None:
            return
        name = getattr(synth, "name", None)
        if not name or name == "silence":
            return
        synthDriverHandler.setSynth(name)
    except Exception:
        log.debugWarning("Audio Processor: unable to refresh current synth", exc_info=True)


def reloadNvdaAudioRouting():
    """Recreate NVDA speech and tone players after a routing-lock change.

    This mirrors NVDA's own Audio settings behaviour: changing output device
    reloads the synth and reinitializes the tones module. The Audio Processor
    WavePlayer hook then substitutes the locked endpoint when required.
    """
    reloadCurrentSynth()
    try:
        import tones
        tones.terminate()
        tones.initialize()
    except Exception:
        log.debugWarning("Audio Processor: unable to refresh NVDA tones routing", exc_info=True)
