from __future__ import annotations

import os
import threading

import addonHandler
import api
import globalPluginHandler
import globalVars
import gui
import nvwave
import scriptHandler
import ui
import wx
from logHandler import log

from . import storage as _storage
from . import deviceRouting as _deviceRouting
from . import audioRuntime as _audioRuntime

addonHandler.initTranslation()

_CONFIG_FILE = "nvdaAudioEQ.json"
_settingsLock = threading.RLock()
_settingsGeneration = 0

# Public cross-add-on integration contract used by Custom Actions.
# Keep this deliberately small: Custom Actions stores complete snapshots and
# asks Audio Processor itself to edit/apply them, so DSP/editor internals stay
# owned by this add-on.
CUSTOM_ACTIONS_API_VERSION = 1
_customActionsEditDialog = None

_DEFAULTS = {
    "enabled": True,
    "target": "all",
    # Optional hard routing lock. Disabled by default so NVDA behaves exactly
    # as it does without Audio Processor. When enabled, the exact MMDevice ID
    # below is forced for every nvwave/WavePlayer created while the lock is active.
    "audioDeviceLock": False,
    "audioDeviceId": "",
    "audioDeviceName": "",
    "lowPassHz": 20000.0,
    "lowPassResonance": 0.0,
    "lowPassLfoDepth": 0.0,
    "band1Freq": 100.0,
    "band1Gain": 0.0,
    "band1Q": 1.0,
    "band2Freq": 500.0,
    "band2Gain": 0.0,
    "band2Q": 1.0,
    "band3Freq": 2000.0,
    "band3Gain": 0.0,
    "band3Q": 1.0,
    "band4Freq": 8000.0,
    "band4Gain": 0.0,
    "band4Q": 1.0,
    "highPassHz": 20.0,
    "highPassResonance": 0.0,
    "highPassLfoDepth": 0.0,
    "masterGainDb": 0.0,
    "compressorEnabled": False,
    "compressorInputDb": 0.0,
    "compressorOutputDb": 0.0,
    "compressorAttackUs": 200.0,
    "compressorReleaseMs": 200.0,
    "compressorRatio": "4:1",
    "pan": 0.0,
    "panLfoWave": "Sine",
    "lfoMode": "Free",
    "panLfoRateHz": 0.50,
    "panLfoSmooth": 0.0,
    "panLfoDepth": 0.0,
    "chorusEnabled": False,
    "chorusMode": "I",
    "chorusDepth": 60.0,
    "chorusRate": 50.0,
    "chorusWidth": 100.0,
    "chorusMix": 35.0,
    "delayEnabled": False,
    "delayTimeMs": 250.0,
    "delayFeedback": 30.0,
    "delayMix": 20.0,
    "delayPingPong": 100.0,
    "delayTailEnabled": False,
    "delayBeforeReverb": False,
    "chainBlend": 0.0,
    "reverbEnabled": False,
    "reverbSize": 50.0,
    "reverbDecaySec": 1.2,
    "reverbDamping": 50.0,
    "reverbMix": 15.0,
    "reverbTailEnabled": False,
    "limiterEnabled": True,
    "limiterCeilingDb": -0.1,
    "limiterReleaseMs": 60.0,
}
_settings = dict(_DEFAULTS)


def _localizedAddonFolderName():
    return _storage.localizedAddonFolderName()


def _presetDirectory():
    return _storage.presetDirectory()


def _migrateLegacyPresetDirectory():
    return _storage.migrateLegacyPresetDirectory()


def _factoryPresetPath():
    return _storage.factoryPresetPath()


def _normalizePresetSettings(values):
    return _storage.normalizePresetSettings(values, _DEFAULTS)


def _loadFactoryPresets():
    return _storage.loadFactoryPresets(_DEFAULTS)


def _loadUserPresets():
    return _storage.loadUserPresets(_DEFAULTS)


def _safePresetFileStem(name):
    return _storage.safePresetFileStem(name)


def _findUserPresetPathByName(name):
    return _storage.findUserPresetPathByName(name, _DEFAULTS)


def _saveUserPreset(name, values):
    return _storage.saveUserPreset(name, values, _DEFAULTS)


def _deleteUserPreset(item):
    return _storage.deleteUserPreset(item)


def _renameUserPreset(item, newName, replaceItem=None):
    return _storage.renameUserPreset(item, newName, _DEFAULTS, replaceItem=replaceItem)


def _configPath():
    return _storage.configPath(_CONFIG_FILE)


def _loadSettings():
    global _settings
    merged = _storage.loadSettings(_DEFAULTS, _CONFIG_FILE)
    with _settingsLock:
        _settings = merged


def _saveSettings():
    with _settingsLock:
        data = _settings.copy()
    _storage.saveSettings(data, _CONFIG_FILE)

def _getAudioOutputDevices(forceRefresh=False):
    return _deviceRouting.getAudioOutputDevices(forceRefresh=forceRefresh)


def _lockedAudioDeviceStatus(settings, forceRefresh=False):
    return _deviceRouting.lockedAudioDeviceStatus(settings, forceRefresh=forceRefresh)


def _settingsAudioRoutingKey(settings):
    return _deviceRouting.settingsAudioRoutingKey(settings)


def _audioRoutingChanged(oldSettings, newSettings):
    return _deviceRouting.audioRoutingChanged(oldSettings, newSettings)

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


def _reloadCurrentSynthForChannelMode():
    return _deviceRouting.reloadCurrentSynth()


def _reloadNvdaAudioRouting():
    return _deviceRouting.reloadNvdaAudioRouting()

def _scheduleRuntimeRefresh(oldSettings, newSettings):
    oldNeed = _settingsNeedStereoForPurpose(oldSettings, nvwave.AudioPurpose.SPEECH)
    newNeed = _settingsNeedStereoForPurpose(newSettings, nvwave.AudioPurpose.SPEECH)
    routingChanged = _audioRoutingChanged(oldSettings, newSettings)
    if routingChanged:
        # One routing refresh also recreates the synth, so do not queue a second
        # synth reload when stereo requirements changed in the same preset.
        wx.CallAfter(_reloadNvdaAudioRouting)
    elif oldNeed != newNeed:
        wx.CallAfter(_reloadCurrentSynthForChannelMode)


def _getSettingsSnapshot():
    with _settingsLock:
        return _settings.copy(), _settingsGeneration


def _applySettings(values, save=False):
    global _settings, _settingsGeneration
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in values.items() if k in merged})
    with _settingsLock:
        previous = _settings.copy()
        _settings = merged
        _settingsGeneration += 1
    _scheduleRuntimeRefresh(previous, merged)
    if save:
        _saveSettings()


def _updateSettings(values):
    _applySettings(values, save=True)


def customActionsIsAvailable():
    """Return True only when the active Audio Processor runtime owns nvwave."""
    return _audioRuntime.isInstalled()


def customActionsGetSettings():
    """Return one complete, detached processor snapshot for Custom Actions."""
    settings, _generation = _getSettingsSnapshot()
    return _normalizePresetSettings(settings)


def customActionsApplySettings(values):
    """Apply one Custom Actions snapshot live without writing Audio Processor config."""
    if not customActionsIsAvailable():
        return False
    if not isinstance(values, dict):
        return False
    normalized = _normalizePresetSettings(values)
    available, _deviceId, deviceName = _lockedAudioDeviceStatus(normalized, forceRefresh=True)
    if not available:
        ui.message(_("Audio device not available: {name}").format(name=deviceName))
        return False
    _applySettings(normalized, save=False)
    return True


def _focusCustomActionsEditDialog():
    dlg = _customActionsEditDialog
    if dlg is None:
        return False
    try:
        if dlg.IsBeingDeleted():
            return False
        dlg.Raise()
        focused = wx.Window.FindFocus()
        try:
            inside = focused is not None and (focused is dlg or dlg.IsDescendant(focused))
        except Exception:
            inside = False
        if not inside:
            dlg.SetFocus()
        return True
    except Exception:
        return False


def customActionsEditSettings(parent, values=None):
    """Edit one detached processor snapshot using the native Audio Processor UI.

    The supplied snapshot is applied temporarily so every existing shortcut and
    live preview behaves exactly as in the normal editor.  OK returns a complete
    snapshot; Cancel/Close returns None.  In every case the processor state that
    existed before entering this editor is restored and is not saved to disk.
    """
    global _customActionsEditDialog
    if not customActionsIsAvailable():
        return None
    if _focusCustomActionsEditDialog():
        return None

    previous, _generation = _getSettingsSnapshot()
    initial = _normalizePresetSettings(values if isinstance(values, dict) else previous)
    dlg = None
    try:
        _applySettings(initial, save=False)
        dlg = AudioEQDialog(
            parent or gui.mainFrame,
            customActionsMode=True,
            restoreSettings=previous,
        )
        _customActionsEditDialog = dlg
        result = dlg.ShowModal()
        if result == wx.ID_OK and isinstance(dlg._customActionsResult, dict):
            return dict(dlg._customActionsResult)
        return None
    finally:
        # Belt-and-braces restore: also covers construction failures and forced
        # window destruction.  This remains live-only and never writes config.
        try:
            _applySettings(previous, save=False)
        except Exception:
            log.debugWarning("Audio Processor: unable to restore state after Custom Actions editor", exc_info=True)
        _customActionsEditDialog = None
        if dlg is not None:
            try:
                dlg.Destroy()
            except Exception:
                pass



def _formatStep(value):
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return (f"{value:.3f}").rstrip("0").rstrip(".")


def _formatNumericValue(value, digits):
    value = float(value)
    if int(digits) <= 0:
        return str(int(round(value)))
    return f"{value:.{int(digits)}f}"


def _numericValue(ctrl, fallback=0.0):
    """Read one editable numeric ComboBox safely while the user may be typing."""
    try:
        text = str(ctrl.GetValue()).strip().replace(",", ".")
        if not text or text in ("-", "+", ".", "-.", "+."):
            return float(fallback)
        value = float(text)
        minimum = float(ctrl._audioEQMinimum)
        maximum = float(ctrl._audioEQMaximum)
        return max(minimum, min(maximum, value))
    except Exception:
        return float(fallback)


def _numericCombo(parent, label, value, minimum, maximum, increment, digits=0, steps=None):
    """Editable ComboBox numeric field, matching the accessible pattern used by Custom Actions Synth."""
    row = wx.BoxSizer(wx.HORIZONTAL)
    text = wx.StaticText(parent, label=label)
    row.Add(text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
    accessibleName = str(label).rstrip(":")
    ctrl = wx.ComboBox(parent, choices=[], style=wx.CB_DROPDOWN)
    ctrl.SetName(accessibleName)
    ctrl._audioEQMinimum = float(minimum)
    ctrl._audioEQMaximum = float(maximum)
    ctrl._audioEQDigits = int(digits)
    ctrl._audioEQLastValidValue = max(float(minimum), min(float(maximum), float(value)))
    ctrl.ChangeValue(_formatNumericValue(ctrl._audioEQLastValidValue, ctrl._audioEQDigits))

    stepValues = [float(v) for v in (steps or [increment])]
    if not stepValues:
        stepValues = [float(increment)]
    try:
        stepIndex = min(range(len(stepValues)), key=lambda i: abs(stepValues[i] - float(increment)))
    except Exception:
        stepIndex = 0
    ctrl._audioEQStepValues = stepValues
    ctrl._audioEQStepIndex = stepIndex

    def applyValue(value):
        value = max(ctrl._audioEQMinimum, min(ctrl._audioEQMaximum, float(value)))
        ctrl._audioEQLastValidValue = value
        ctrl.ChangeValue(_formatNumericValue(value, ctrl._audioEQDigits))
        # ChangeValue intentionally does not emit EVT_TEXT; send the same live
        # update explicitly so the next NVDA utterance uses the new EQ value.
        dialog = ctrl.GetTopLevelParent()
        if isinstance(dialog, AudioEQDialog):
            dialog._applyLiveFromControls()

    def applyDelta(direction, multiplier=1.0):
        current = _numericValue(ctrl, ctrl._audioEQLastValidValue)
        step = ctrl._audioEQStepValues[ctrl._audioEQStepIndex]
        applyValue(current + (step * float(direction) * float(multiplier)))

    def _consume(evt):
        # EVT_CHAR_HOOK sees navigation keys before the editable child of the
        # ComboBox.  Stopping propagation prevents wx/NVDA from also treating
        # Left/Right as caret movement and announcing the field value again.
        try:
            evt.StopPropagation()
        except Exception:
            pass

    def onKey(evt):
        key = evt.GetKeyCode()
        if key == wx.WXK_UP:
            _consume(evt)
            applyDelta(1)
            return
        if key == wx.WXK_DOWN:
            _consume(evt)
            applyDelta(-1)
            return
        if key == wx.WXK_PAGEUP:
            _consume(evt)
            applyDelta(1, 10)
            return
        if key == wx.WXK_PAGEDOWN:
            _consume(evt)
            applyDelta(-1, 10)
            return
        evt.Skip()

    def onText(evt):
        try:
            textValue = str(ctrl.GetValue()).strip().replace(",", ".")
            value = float(textValue)
            if ctrl._audioEQMinimum <= value <= ctrl._audioEQMaximum:
                ctrl._audioEQLastValidValue = value
                dialog = ctrl.GetTopLevelParent()
                if isinstance(dialog, AudioEQDialog):
                    dialog._applyLiveFromControls()
        except Exception:
            pass
        evt.Skip()

    def onKillFocus(evt):
        value = _numericValue(ctrl, ctrl._audioEQLastValidValue)
        ctrl._audioEQLastValidValue = value
        ctrl.ChangeValue(_formatNumericValue(value, ctrl._audioEQDigits))
        evt.Skip()

    ctrl.Bind(wx.EVT_CHAR_HOOK, onKey)
    ctrl.Bind(wx.EVT_TEXT, onText)
    ctrl.Bind(wx.EVT_KILL_FOCUS, onKillFocus)
    row.Add(ctrl, 1, wx.EXPAND)
    return row, ctrl


class AudioEQDialog(wx.Dialog):
    def __init__(self, parent, customActionsMode=False, restoreSettings=None):
        super().__init__(parent, title=_("Audio Processor"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        settings, settingsRevision = _getSettingsSnapshot()
        self._customActionsMode = bool(customActionsMode)
        self._customActionsResult = None
        self._originalSettings = dict(settings)
        self._restoreSettings = dict(
            restoreSettings if isinstance(restoreSettings, dict) else settings
        )
        self._committed = False
        self._restored = False
        outer = wx.BoxSizer(wx.VERTICAL)
        body = wx.BoxSizer(wx.VERTICAL)

        self.enabled = wx.CheckBox(self, label=_("Audio processing enabled"))
        self.enabled.SetValue(bool(settings["enabled"]))
        body.Add(self.enabled, 0, wx.BOTTOM, 8)

        self._loadingPreset = False
        self._presetEntries = []
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Preset:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.preset = wx.Choice(self, choices=[])
        self.preset.SetName("Preset")
        row.Add(self.preset, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)
        self._refreshPresetList()
        self.preset.Bind(wx.EVT_CHOICE, self._onPresetSelected)
        self.preset.Bind(wx.EVT_CHAR_HOOK, self._onPresetKeyDown)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label=_("Process:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.target = wx.Choice(self, choices=[_("All NVDA audio"), _("Speech only"), _("Sounds only")])
        self.target.SetName(_("Process"))
        self.target.SetSelection({"all": 0, "speech": 1, "sounds": 2}.get(settings.get("target"), 0))
        row.Add(self.target, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        self.audioDeviceLock = wx.CheckBox(self, label=_("Lock NVDA Audio Device"))
        self.audioDeviceLock.SetName(_("Lock NVDA Audio Device"))
        self.audioDeviceLock.SetValue(bool(settings.get("audioDeviceLock", False)))
        body.Add(self.audioDeviceLock, 0, wx.BOTTOM, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label=_("Audio Device:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.audioDevice = wx.Choice(self, choices=[])
        self.audioDevice.SetName(_("Audio Device"))
        self._audioDeviceIds = []
        self._audioDeviceNames = []
        self._audioDeviceStoredNames = []
        self._audioDeviceAvailableIds = set()
        self._refreshAudioDeviceChoices(
            preferredId=str(settings.get("audioDeviceId", "") or ""),
            preferredName=str(settings.get("audioDeviceName", "") or ""),
        )
        self.audioDevice.Enable(self.audioDeviceLock.IsChecked())
        row.Add(self.audioDevice, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        row, self.lowPass = _numericCombo(self, "Low Pass Hz:", settings["lowPassHz"], 20, 20000, 10, 0, steps=[1, 10, 100, 1000])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.lowPassResonance = _numericCombo(self, "Low Pass Resonance 0-100:", settings.get("lowPassResonance", 0.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 6)

        self.bands = []
        for i in range(1, 5):
            row, freq = _numericCombo(self, "Band {index} Frequency Hz:".format(index=i), settings[f"band{i}Freq"], 20, 20000, 10, 0, steps=[1, 10, 100, 1000])
            body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
            row, gain = _numericCombo(self, "Band {index} Gain dB:".format(index=i), settings[f"band{i}Gain"], -24, 24, 0.5, 1, steps=[0.1, 0.5, 1, 3, 6])
            body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
            row, q = _numericCombo(self, "Band {index} Q:".format(index=i), settings[f"band{i}Q"], 0.1, 20, 0.1, 2, steps=[0.01, 0.1, 0.5, 1])
            body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)
            self.bands.append((freq, gain, q))

        row, self.highPass = _numericCombo(self, "High Pass Hz:", settings["highPassHz"], 20, 20000, 10, 0, steps=[1, 10, 100, 1000])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.highPassResonance = _numericCombo(self, "High Pass Resonance 0-100:", settings.get("highPassResonance", 0.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        row, self.masterGain = _numericCombo(self, "Master Gain dB:", settings["masterGainDb"], -60, 12, 0.5, 1, steps=[0.1, 0.5, 1, 3, 6])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 6)

        self.compressorEnabled = wx.CheckBox(self, label=_("1176 Compressor enabled"))
        self.compressorEnabled.SetName(_("1176 Compressor enabled"))
        self.compressorEnabled.SetValue(bool(settings.get("compressorEnabled", False)))
        body.Add(self.compressorEnabled, 0, wx.BOTTOM, 4)
        row, self.compressorInput = _numericCombo(self, "1176 Input dB:", settings.get("compressorInputDb", 0.0), -12, 24, 0.5, 1, steps=[0.1, 0.5, 1, 3, 6])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.compressorOutput = _numericCombo(self, "1176 Output dB:", settings.get("compressorOutputDb", 0.0), -24, 24, 0.5, 1, steps=[0.1, 0.5, 1, 3, 6])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.compressorAttack = _numericCombo(self, "1176 Attack microseconds:", settings.get("compressorAttackUs", 200.0), 20, 800, 20, 0, steps=[10, 20, 50, 100, 200])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.compressorRelease = _numericCombo(self, "1176 Release ms:", settings.get("compressorReleaseMs", 200.0), 50, 1100, 25, 0, steps=[10, 25, 50, 100, 250])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="1176 Ratio:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.compressorRatio = wx.Choice(self, choices=["4:1", "8:1", "12:1", "20:1", "All"])
        self.compressorRatio.SetName("1176 Ratio")
        ratios = ["4:1", "8:1", "12:1", "20:1", "All"]
        try:
            self.compressorRatio.SetSelection(ratios.index(str(settings.get("compressorRatio", "4:1"))))
        except ValueError:
            self.compressorRatio.SetSelection(0)
        row.Add(self.compressorRatio, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        row, self.pan = _numericCombo(self, "Pan -100/+100:", settings["pan"], -100, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="LFO Waveform:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.panLfoWave = wx.Choice(self, choices=["Sine", "Triangle", "Saw", "Square", "S&H"])
        self.panLfoWave.SetName("LFO Waveform")
        panLfoWaves = ["Sine", "Tri", "Saw", "Square", "S&H"]
        try:
            self.panLfoWave.SetSelection(panLfoWaves.index(str(settings.get("panLfoWave", "Sine"))))
        except ValueError:
            self.panLfoWave.SetSelection(0)
        row.Add(self.panLfoWave, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="LFO Mode:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lfoMode = wx.Choice(self, choices=["Free", "Trigger"])
        self.lfoMode.SetName("LFO Mode")
        try:
            self.lfoMode.SetSelection(("Free", "Trigger").index(str(settings.get("lfoMode", "Free"))))
        except ValueError:
            self.lfoMode.SetSelection(0)
        row.Add(self.lfoMode, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)

        row, self.panLfoRate = _numericCombo(self, "LFO Rate Hz:", settings.get("panLfoRateHz", 0.50), 0.01, 80.0, 0.05, 2, steps=[0.01, 0.05, 0.1, 0.5, 1])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.panLfoSmooth = _numericCombo(self, "LFO Smoothing 0-100:", settings.get("panLfoSmooth", 0.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.panLfoDepth = _numericCombo(self, "Pan LFO Depth -100 to +100:", settings.get("panLfoDepth", 0.0), -100, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.lowPassLfoDepth = _numericCombo(self, "Low Pass LFO Depth -100 to +100:", settings.get("lowPassLfoDepth", 0.0), -100, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.highPassLfoDepth = _numericCombo(self, "High Pass LFO Depth -100 to +100:", settings.get("highPassLfoDepth", 0.0), -100, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 10)

        self.chorusEnabled = wx.CheckBox(self, label=_("Stereo Chorus enabled"))
        self.chorusEnabled.SetName(_("Stereo Chorus enabled"))
        self.chorusEnabled.SetValue(bool(settings["chorusEnabled"]))
        body.Add(self.chorusEnabled, 0, wx.BOTTOM, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Chorus Mode:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.chorusMode = wx.Choice(self, choices=["I", "II", "I+II"])
        self.chorusMode.SetName("Chorus Mode")
        modes = ["I", "II", "I+II"]
        try:
            self.chorusMode.SetSelection(modes.index(str(settings.get("chorusMode", "I"))))
        except ValueError:
            self.chorusMode.SetSelection(0)
        row.Add(self.chorusMode, 1, wx.EXPAND)
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.chorusDepth = _numericCombo(self, "Chorus Depth 0-100:", settings["chorusDepth"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.chorusRate = _numericCombo(self, "Chorus Rate 0-100:", settings.get("chorusRate", 50.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.chorusWidth = _numericCombo(self, "Chorus Width 0-100:", settings.get("chorusWidth", 100.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.chorusMix = _numericCombo(self, "Chorus Send 0-100:", settings["chorusMix"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 10)

        self.delayEnabled = wx.CheckBox(self, label=_("Stereo Delay enabled"))
        self.delayEnabled.SetName(_("Stereo Delay enabled"))
        self.delayEnabled.SetValue(bool(settings["delayEnabled"]))
        body.Add(self.delayEnabled, 0, wx.BOTTOM, 4)
        self.delayTailEnabled = wx.CheckBox(self, label=_("Delay tail enabled"))
        self.delayTailEnabled.SetName(_("Delay tail enabled"))
        self.delayTailEnabled.SetValue(bool(settings.get("delayTailEnabled", False)))
        body.Add(self.delayTailEnabled, 0, wx.BOTTOM, 4)
        self.delayBeforeReverb = wx.CheckBox(self, label="Delay before Reverb")
        self.delayBeforeReverb.SetName("Delay before Reverb")
        self.delayBeforeReverb.SetValue(bool(settings.get("delayBeforeReverb", False)))
        body.Add(self.delayBeforeReverb, 0, wx.BOTTOM, 4)
        row, self.chainBlend = _numericCombo(self, "Chain Blend 0-100:", settings.get("chainBlend", 0.0), 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.delayTime = _numericCombo(self, "Delay Time ms:", settings["delayTimeMs"], 1, 1000, 10, 0, steps=[1, 10, 25, 50, 100])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.delayFeedback = _numericCombo(self, "Delay Feedback 0-95:", settings["delayFeedback"], 0, 95, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.delayMix = _numericCombo(self, "Delay Send 0-100:", settings["delayMix"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.delayPingPong = _numericCombo(self, "Delay Ping-Pong 0-100:", settings["delayPingPong"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 10)

        self.reverbEnabled = wx.CheckBox(self, label=_("Stereo Reverb enabled"))
        self.reverbEnabled.SetName(_("Stereo Reverb enabled"))
        self.reverbEnabled.SetValue(bool(settings["reverbEnabled"]))
        body.Add(self.reverbEnabled, 0, wx.BOTTOM, 4)
        self.reverbTailEnabled = wx.CheckBox(self, label=_("Reverb tail enabled"))
        self.reverbTailEnabled.SetName(_("Reverb tail enabled"))
        self.reverbTailEnabled.SetValue(bool(settings.get("reverbTailEnabled", False)))
        body.Add(self.reverbTailEnabled, 0, wx.BOTTOM, 4)
        row, self.reverbSize = _numericCombo(self, "Reverb Size 0-100:", settings["reverbSize"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.reverbDecay = _numericCombo(self, "Reverb Decay seconds:", settings["reverbDecaySec"], 0.1, 5, 0.1, 2, steps=[0.05, 0.1, 0.25, 0.5, 1])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.reverbDamping = _numericCombo(self, "Reverb Damping 0-100:", settings["reverbDamping"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.reverbMix = _numericCombo(self, "Reverb Send 0-100:", settings["reverbMix"], 0, 100, 5, 0, steps=[1, 5, 10, 25])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 10)

        self.limiterEnabled = wx.CheckBox(self, label=_("Limiter enabled"))
        self.limiterEnabled.SetValue(bool(settings["limiterEnabled"]))
        body.Add(self.limiterEnabled, 0, wx.BOTTOM, 6)
        row, self.limiterCeiling = _numericCombo(self, "Limiter Ceiling dB:", settings["limiterCeilingDb"], -24, 0, 0.1, 1, steps=[0.1, 0.5, 1, 3, 6])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 4)
        row, self.limiterRelease = _numericCombo(self, "Limiter Release ms:", settings["limiterReleaseMs"], 1, 2000, 5, 0, steps=[1, 5, 10, 50, 100])
        body.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Keep a flat list so local step shortcuts can resolve the currently
        # focused editable ComboBox even when wx reports its internal TextCtrl.
        self._numericControls = [self.lowPass, self.lowPassResonance]
        for freq, gain, q in self.bands:
            self._numericControls.extend((freq, gain, q))
        self._numericControls.extend((
            self.highPass, self.highPassResonance, self.masterGain,
            self.compressorInput, self.compressorOutput, self.compressorAttack, self.compressorRelease,
            self.pan, self.panLfoRate, self.panLfoSmooth, self.panLfoDepth, self.lowPassLfoDepth, self.highPassLfoDepth,
            self.chorusDepth, self.chorusRate, self.chorusWidth, self.chorusMix,
            self.chainBlend, self.delayTime, self.delayFeedback, self.delayMix, self.delayPingPong,
            self.reverbSize, self.reverbDecay, self.reverbDamping, self.reverbMix,
            self.limiterCeiling, self.limiterRelease,
        ))

        self.statusButton = wx.Button(self, label=_("Report intercepted audio format"))
        self.statusButton.Bind(wx.EVT_BUTTON, self._onStatus)
        body.Add(self.statusButton, 0, wx.EXPAND)

        outer.Add(body, 1, wx.ALL | wx.EXPAND, 12)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            outer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.SetSizer(outer)
        self.Bind(wx.EVT_BUTTON, self._onOk, id=wx.ID_OK)
        self.SetMinSize((520, 620))
        self.Fit()
        self.CentreOnScreen()
        self._bindLiveControls()
        self._bindLocalShortcuts()
        self.Bind(wx.EVT_BUTTON, self._onCancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._onClose)
        self.enabled.SetFocus()

    def _bindLocalShortcuts(self):
        self._shortcutIds = {
            "lowPass": int(wx.NewIdRef()),
            "highPass": int(wx.NewIdRef()),
            "band1": int(wx.NewIdRef()),
            "band2": int(wx.NewIdRef()),
            "band3": int(wx.NewIdRef()),
            "band4": int(wx.NewIdRef()),
            "volume": int(wx.NewIdRef()),
            "pan": int(wx.NewIdRef()),
            "chorusCompressor": int(wx.NewIdRef()),
            "delay": int(wx.NewIdRef()),
            "reverb": int(wx.NewIdRef()),
            "limiter": int(wx.NewIdRef()),
            "stepDown": int(wx.NewIdRef()),
            "stepUp": int(wx.NewIdRef()),
            "savePreset": int(wx.NewIdRef()),
        }
        entries = [
            (wx.ACCEL_ALT, ord("Q"), self._shortcutIds["lowPass"]),
            (wx.ACCEL_ALT, ord("W"), self._shortcutIds["highPass"]),
            (wx.ACCEL_ALT, ord("1"), self._shortcutIds["band1"]),
            (wx.ACCEL_ALT, ord("2"), self._shortcutIds["band2"]),
            (wx.ACCEL_ALT, ord("3"), self._shortcutIds["band3"]),
            (wx.ACCEL_ALT, ord("4"), self._shortcutIds["band4"]),
            (wx.ACCEL_ALT, ord("V"), self._shortcutIds["volume"]),
            (wx.ACCEL_ALT, ord("P"), self._shortcutIds["pan"]),
            (wx.ACCEL_ALT, ord("C"), self._shortcutIds["chorusCompressor"]),
            (wx.ACCEL_ALT, ord("D"), self._shortcutIds["delay"]),
            (wx.ACCEL_ALT, ord("R"), self._shortcutIds["reverb"]),
            (wx.ACCEL_ALT, ord("L"), self._shortcutIds["limiter"]),
            (wx.ACCEL_ALT, ord("A"), self._shortcutIds["stepDown"]),
            (wx.ACCEL_ALT, ord("S"), self._shortcutIds["stepUp"]),
            (wx.ACCEL_CTRL, ord("S"), self._shortcutIds["savePreset"]),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))
        self.Bind(wx.EVT_MENU, lambda evt: self.lowPass.SetFocus(), id=self._shortcutIds["lowPass"])
        self.Bind(wx.EVT_MENU, lambda evt: self.highPass.SetFocus(), id=self._shortcutIds["highPass"])
        self.Bind(wx.EVT_MENU, lambda evt: self.masterGain.SetFocus(), id=self._shortcutIds["volume"])
        self.Bind(wx.EVT_MENU, lambda evt: self._cycleShortcutFocus("P"), id=self._shortcutIds["pan"])
        self.Bind(wx.EVT_MENU, lambda evt: self._cycleShortcutFocus("C"), id=self._shortcutIds["chorusCompressor"])
        self.Bind(wx.EVT_MENU, lambda evt: self.delayEnabled.SetFocus(), id=self._shortcutIds["delay"])
        self.Bind(wx.EVT_MENU, lambda evt: self.reverbEnabled.SetFocus(), id=self._shortcutIds["reverb"])
        self.Bind(wx.EVT_MENU, lambda evt: self.limiterEnabled.SetFocus(), id=self._shortcutIds["limiter"])
        for i in range(4):
            self.Bind(
                wx.EVT_MENU,
                lambda evt, index=i: self.bands[index][0].SetFocus(),
                id=self._shortcutIds[f"band{i + 1}"],
            )
        self.Bind(wx.EVT_MENU, lambda evt: self._changeFocusedStep(-1), id=self._shortcutIds["stepDown"])
        self.Bind(wx.EVT_MENU, lambda evt: self._changeFocusedStep(1), id=self._shortcutIds["stepUp"])
        self.Bind(wx.EVT_MENU, self._onSavePreset, id=self._shortcutIds["savePreset"])

    def _refreshAudioDeviceChoices(self, preferredId="", preferredName=""):
        devices = _getAudioOutputDevices(forceRefresh=True)
        self._audioDeviceAvailableIds = {deviceId for deviceId, _name in devices}
        ids = [deviceId for deviceId, _name in devices]
        names = [name for _deviceId, name in devices]
        storedNames = list(names)

        preferredId = str(preferredId or "")
        preferredName = str(preferredName or "")
        if preferredId and preferredId not in self._audioDeviceAvailableIds:
            # Preserve a missing stored endpoint visibly instead of silently
            # substituting another card in the editor.
            rawName = preferredName or preferredId
            ids.insert(0, preferredId)
            storedNames.insert(0, rawName)
            names.insert(0, _("Unavailable: {name}").format(name=rawName))

        self._audioDeviceIds = ids
        self._audioDeviceNames = names
        self._audioDeviceStoredNames = storedNames
        self.audioDevice.SetItems(names)
        selection = 0 if ids else wx.NOT_FOUND
        if preferredId and preferredId in ids:
            selection = ids.index(preferredId)
        if selection != wx.NOT_FOUND:
            self.audioDevice.SetSelection(selection)

    def _refreshPresetList(self, selectKind=None, selectName=None):
        entries = [{"kind": "current", "name": _("Current settings"), "settings": None}]
        entries.extend(_loadFactoryPresets())
        entries.extend(_loadUserPresets())
        labels = []
        selected = 0
        for index, item in enumerate(entries):
            if item["kind"] == "current":
                label = item["name"]
            elif item["kind"] == "factory":
                label = _("Factory: {name}").format(name=item["name"])
            else:
                label = _("User: {name}").format(name=item["name"])
            labels.append(label)
            if selectKind == item["kind"] and selectName is not None and item["name"].casefold() == str(selectName).casefold():
                selected = index
        self._presetEntries = entries
        self.preset.SetItems(labels)
        self.preset.SetSelection(selected)

    def _setNumericControlValue(self, ctrl, value):
        value = max(ctrl._audioEQMinimum, min(ctrl._audioEQMaximum, float(value)))
        ctrl._audioEQLastValidValue = value
        ctrl.ChangeValue(_formatNumericValue(value, ctrl._audioEQDigits))

    def _setChoiceByValue(self, ctrl, values, value, fallback=0):
        try:
            ctrl.SetSelection(values.index(str(value)))
        except ValueError:
            ctrl.SetSelection(fallback)

    def _setControlsFromValues(self, values):
        values = _normalizePresetSettings(values)
        self._loadingPreset = True
        try:
            self.enabled.SetValue(bool(values["enabled"]))
            self.target.SetSelection({"all": 0, "speech": 1, "sounds": 2}.get(values.get("target"), 0))
            self.audioDeviceLock.SetValue(bool(values.get("audioDeviceLock", False)))
            self._refreshAudioDeviceChoices(
                preferredId=str(values.get("audioDeviceId", "") or ""),
                preferredName=str(values.get("audioDeviceName", "") or ""),
            )
            self.audioDevice.Enable(self.audioDeviceLock.IsChecked())
            self._setNumericControlValue(self.lowPass, values["lowPassHz"])
            self._setNumericControlValue(self.lowPassResonance, values["lowPassResonance"])
            self._setNumericControlValue(self.highPass, values["highPassHz"])
            self._setNumericControlValue(self.highPassResonance, values["highPassResonance"])
            self._setNumericControlValue(self.masterGain, values["masterGainDb"])
            for i, (freq, gain, q) in enumerate(self.bands, 1):
                self._setNumericControlValue(freq, values[f"band{i}Freq"])
                self._setNumericControlValue(gain, values[f"band{i}Gain"])
                self._setNumericControlValue(q, values[f"band{i}Q"])
            self.compressorEnabled.SetValue(bool(values["compressorEnabled"]))
            self._setNumericControlValue(self.compressorInput, values["compressorInputDb"])
            self._setNumericControlValue(self.compressorOutput, values["compressorOutputDb"])
            self._setNumericControlValue(self.compressorAttack, values["compressorAttackUs"])
            self._setNumericControlValue(self.compressorRelease, values["compressorReleaseMs"])
            self._setChoiceByValue(self.compressorRatio, ["4:1", "8:1", "12:1", "20:1", "All"], values["compressorRatio"])
            self._setNumericControlValue(self.pan, values["pan"])
            self._setChoiceByValue(self.panLfoWave, ["Sine", "Tri", "Saw", "Square", "S&H"], values["panLfoWave"])
            self._setChoiceByValue(self.lfoMode, ["Free", "Trigger"], values.get("lfoMode", "Free"))
            self._setNumericControlValue(self.panLfoRate, values["panLfoRateHz"])
            self._setNumericControlValue(self.panLfoSmooth, values["panLfoSmooth"])
            self._setNumericControlValue(self.panLfoDepth, values["panLfoDepth"])
            self._setNumericControlValue(self.lowPassLfoDepth, values["lowPassLfoDepth"])
            self._setNumericControlValue(self.highPassLfoDepth, values["highPassLfoDepth"])
            self.chorusEnabled.SetValue(bool(values["chorusEnabled"]))
            self._setChoiceByValue(self.chorusMode, ["I", "II", "I+II"], values["chorusMode"])
            self._setNumericControlValue(self.chorusDepth, values["chorusDepth"])
            self._setNumericControlValue(self.chorusRate, values["chorusRate"])
            self._setNumericControlValue(self.chorusWidth, values["chorusWidth"])
            self._setNumericControlValue(self.chorusMix, values["chorusMix"])
            self.delayEnabled.SetValue(bool(values["delayEnabled"]))
            self.delayTailEnabled.SetValue(bool(values.get("delayTailEnabled", False)))
            self.delayBeforeReverb.SetValue(bool(values.get("delayBeforeReverb", False)))
            self._setNumericControlValue(self.chainBlend, values.get("chainBlend", 0.0))
            self._setNumericControlValue(self.delayTime, values["delayTimeMs"])
            self._setNumericControlValue(self.delayFeedback, values["delayFeedback"])
            self._setNumericControlValue(self.delayMix, values["delayMix"])
            self._setNumericControlValue(self.delayPingPong, values["delayPingPong"])
            self.reverbEnabled.SetValue(bool(values["reverbEnabled"]))
            self.reverbTailEnabled.SetValue(bool(values.get("reverbTailEnabled", False)))
            self._setNumericControlValue(self.reverbSize, values["reverbSize"])
            self._setNumericControlValue(self.reverbDecay, values["reverbDecaySec"])
            self._setNumericControlValue(self.reverbDamping, values["reverbDamping"])
            self._setNumericControlValue(self.reverbMix, values["reverbMix"])
            self.limiterEnabled.SetValue(bool(values["limiterEnabled"]))
            self._setNumericControlValue(self.limiterCeiling, values["limiterCeilingDb"])
            self._setNumericControlValue(self.limiterRelease, values["limiterReleaseMs"])
            _applySettings(values, save=False)
        finally:
            self._loadingPreset = False

    def _onPresetSelected(self, evt):
        index = self.preset.GetSelection()
        if index <= 0 or index >= len(self._presetEntries):
            return
        item = self._presetEntries[index]
        try:
            previousSettings, previousRevision = _getSettingsSnapshot()
            newSettings = _normalizePresetSettings(item["settings"])

            available, _deviceId, deviceName = _lockedAudioDeviceStatus(newSettings, forceRefresh=True)
            if not available:
                # Keep the previous processor/routing state completely intact, but
                # leave the Preset choice on this unavailable entry. Arrowing again
                # therefore continues naturally to the next/previous preset.
                ui.message(_("Audio device not available: {name}").format(name=deviceName))
                return

            oldNeedsStereo = _settingsNeedStereoForPurpose(previousSettings, nvwave.AudioPurpose.SPEECH)
            newNeedsStereo = _settingsNeedStereoForPurpose(newSettings, nvwave.AudioPurpose.SPEECH)
            routingChanged = _audioRoutingChanged(previousSettings, newSettings)

            # Apply first, so the selected preset is already audible. If the preset
            # changes channels or routing, the queued synth refresh would cancel an
            # immediate announcement. Re-announce after that refresh.
            self._setControlsFromValues(newSettings)
            message = _("Preset loaded: {name}").format(name=item["name"])
            if oldNeedsStereo != newNeedsStereo or routingChanged:
                wx.CallAfter(lambda msg=message: wx.CallAfter(ui.message, msg))
            else:
                ui.message(message)
        except Exception:
            log.error("Audio Processor: unable to load preset", exc_info=True)
            ui.message(_("Unable to load preset."))

    def _selectedPresetEntry(self):
        index = self.preset.GetSelection()
        if index < 0 or index >= len(self._presetEntries):
            return None
        return self._presetEntries[index]

    def _onPresetKeyDown(self, evt):
        key = evt.GetKeyCode()
        if key == wx.WXK_DELETE:
            self._onDeletePreset()
            return
        if key == wx.WXK_F2:
            self._onRenamePreset()
            return
        evt.Skip()

    def _onDeletePreset(self):
        item = self._selectedPresetEntry()
        if item is None or item.get("kind") == "current":
            ui.message(_("Select a user preset to delete."))
            return
        if item.get("kind") == "factory":
            ui.message(_("Factory presets cannot be deleted."))
            return
        name = item["name"]
        result = wx.MessageBox(
            _("Delete user preset '{name}'?").format(name=name),
            _("Delete preset"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
            self,
        )
        if result != wx.YES:
            self.preset.SetFocus()
            return
        try:
            _deleteUserPreset(item)
            self._refreshPresetList()
            self.preset.SetFocus()
            ui.message(_("Preset deleted: {name}").format(name=name))
        except Exception:
            log.error("Audio Processor: unable to delete user preset", exc_info=True)
            ui.message(_("Unable to delete preset."))

    def _onRenamePreset(self):
        item = self._selectedPresetEntry()
        if item is None or item.get("kind") == "current":
            ui.message(_("Select a user preset to rename."))
            return
        if item.get("kind") == "factory":
            ui.message(_("Factory presets cannot be renamed."))
            return
        oldName = item["name"]
        dialog = wx.TextEntryDialog(
            self,
            _("Enter the new preset name:"),
            _("Rename preset"),
            value=oldName,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                self.preset.SetFocus()
                return
            newName = str(dialog.GetValue()).strip()
        finally:
            dialog.Destroy()
        if not newName:
            ui.message(_("Preset name cannot be empty."))
            self.preset.SetFocus()
            return
        if newName == oldName:
            self.preset.SetFocus()
            return

        existing = next(
            (candidate for candidate in _loadUserPresets()
             if candidate["name"].casefold() == newName.casefold()
             and os.path.normcase(os.path.abspath(candidate.get("path", ""))) != os.path.normcase(os.path.abspath(item.get("path", "")))),
            None,
        )
        if existing is not None:
            result = wx.MessageBox(
                _("A user preset named '{name}' already exists. Replace it?").format(name=newName),
                _("Replace preset"),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            if result != wx.YES:
                self.preset.SetFocus()
                return
        try:
            _renameUserPreset(item, newName, replaceItem=existing)
            self._refreshPresetList(selectKind="user", selectName=newName)
            self.preset.SetFocus()
            ui.message(_("Preset renamed: {oldName} to {newName}").format(oldName=oldName, newName=newName))
        except Exception:
            log.error("Audio Processor: unable to rename user preset", exc_info=True)
            ui.message(_("Unable to rename preset."))

    def _onSavePreset(self, evt):
        dialog = wx.TextEntryDialog(self, _("Enter a name for the preset:"), _("Save preset as"))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = str(dialog.GetValue()).strip()
        finally:
            dialog.Destroy()
        if not name:
            ui.message(_("Preset name cannot be empty."))
            return
        existing = next(
            (item for item in _loadUserPresets() if item["name"].casefold() == name.casefold()),
            None,
        )
        if existing is not None:
            result = wx.MessageBox(
                _("A user preset named '{name}' already exists. Replace it?").format(name=name),
                _("Replace preset"),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self,
            )
            if result != wx.YES:
                return
        try:
            _saveUserPreset(name, self._valuesFromControls())
            self._refreshPresetList(selectKind="user", selectName=name)
            self.preset.SetFocus()
            ui.message(_("Preset saved: {name}").format(name=name))
        except Exception:
            log.error("Audio Processor: unable to save user preset", exc_info=True)
            ui.message(_("Unable to save preset."))

    def _focusIsWithinAny(self, controls):
        current = wx.Window.FindFocus()
        while current is not None:
            if current in controls:
                return True
            try:
                current = current.GetParent()
            except Exception:
                break
        return False

    def _cycleShortcutFocus(self, key):
        """Cycle a repeated Alt mnemonic in real dialog order.

        Each entry is (focusTarget, sectionControls).  If focus is currently
        anywhere inside one section, the shortcut advances to the next one;
        from any unrelated control it starts at the first occurrence in the
        dialog.  Keeping the groups here makes repeated accelerators behave
        consistently instead of requiring one-off Pan/Preset or Chorus/
        Compressor rules.
        """
        compressorControls = (
            self.compressorEnabled, self.compressorInput, self.compressorOutput,
            self.compressorAttack, self.compressorRelease, self.compressorRatio,
        )
        chorusControls = (
            self.chorusEnabled, self.chorusMode, self.chorusDepth, self.chorusRate,
            self.chorusWidth, self.chorusMix,
        )
        groups = {
            # Dialog order: Preset, Process, ... Pan.
            "P": (
                (self.preset, (self.preset,)),
                (self.target, (self.target,)),
                (self.pan, (self.pan,)),
            ),
            # Dialog order: 1176 Compressor section, then Stereo Chorus.
            "C": (
                (self.compressorEnabled, compressorControls),
                (self.chorusEnabled, chorusControls),
            ),
        }.get(str(key).upper(), ())
        if not groups:
            return
        for index, (_target, sectionControls) in enumerate(groups):
            if self._focusIsWithinAny(sectionControls):
                groups[(index + 1) % len(groups)][0].SetFocus()
                return
        groups[0][0].SetFocus()

    def _focusedNumericControl(self):
        focus = wx.Window.FindFocus()
        current = focus
        while current is not None:
            if current in self._numericControls:
                return current
            try:
                current = current.GetParent()
            except Exception:
                break
        return None

    def _changeFocusedStep(self, direction):
        ctrl = self._focusedNumericControl()
        if ctrl is None:
            ui.message(_("No numeric parameter selected"))
            return
        values = ctrl._audioEQStepValues
        index = int(ctrl._audioEQStepIndex) + (1 if direction > 0 else -1)
        ctrl._audioEQStepIndex = max(0, min(len(values) - 1, index))
        ui.message(_("Step {step}").format(step=_formatStep(values[ctrl._audioEQStepIndex])))

    def _valuesFromControls(self):
        targetIndex = self.target.GetSelection()
        if targetIndex < 0:
            targetIndex = 0
        deviceIndex = self.audioDevice.GetSelection()
        if 0 <= deviceIndex < len(self._audioDeviceIds):
            audioDeviceId = self._audioDeviceIds[deviceIndex]
            audioDeviceName = self._audioDeviceStoredNames[deviceIndex]
        else:
            audioDeviceId = str(self._originalSettings.get("audioDeviceId", "") or "")
            audioDeviceName = str(self._originalSettings.get("audioDeviceName", "") or "")
        values = {
            "enabled": self.enabled.IsChecked(),
            "target": ("all", "speech", "sounds")[targetIndex],
            "audioDeviceLock": self.audioDeviceLock.IsChecked(),
            "audioDeviceId": audioDeviceId,
            "audioDeviceName": audioDeviceName,
            "lowPassHz": _numericValue(self.lowPass, self._originalSettings["lowPassHz"]),
            "lowPassResonance": _numericValue(self.lowPassResonance, self._originalSettings.get("lowPassResonance", 0.0)),
            "lowPassLfoDepth": _numericValue(self.lowPassLfoDepth, self._originalSettings.get("lowPassLfoDepth", 0.0)),
            "highPassHz": _numericValue(self.highPass, self._originalSettings["highPassHz"]),
            "highPassResonance": _numericValue(self.highPassResonance, self._originalSettings.get("highPassResonance", 0.0)),
            "highPassLfoDepth": _numericValue(self.highPassLfoDepth, self._originalSettings.get("highPassLfoDepth", 0.0)),
            "masterGainDb": _numericValue(self.masterGain, self._originalSettings["masterGainDb"]),
            "compressorEnabled": self.compressorEnabled.IsChecked(),
            "compressorInputDb": _numericValue(self.compressorInput, self._originalSettings.get("compressorInputDb", 0.0)),
            "compressorOutputDb": _numericValue(self.compressorOutput, self._originalSettings.get("compressorOutputDb", 0.0)),
            "compressorAttackUs": _numericValue(self.compressorAttack, self._originalSettings.get("compressorAttackUs", 200.0)),
            "compressorReleaseMs": _numericValue(self.compressorRelease, self._originalSettings.get("compressorReleaseMs", 200.0)),
            "compressorRatio": ("4:1", "8:1", "12:1", "20:1", "All")[max(0, self.compressorRatio.GetSelection())],
            "pan": _numericValue(self.pan, self._originalSettings["pan"]),
            "panLfoWave": ("Sine", "Tri", "Saw", "Square", "S&H")[max(0, self.panLfoWave.GetSelection())],
            "lfoMode": ("Free", "Trigger")[max(0, self.lfoMode.GetSelection())],
            "panLfoRateHz": _numericValue(self.panLfoRate, self._originalSettings.get("panLfoRateHz", 0.50)),
            "panLfoSmooth": _numericValue(self.panLfoSmooth, self._originalSettings.get("panLfoSmooth", 0.0)),
            "panLfoDepth": _numericValue(self.panLfoDepth, self._originalSettings.get("panLfoDepth", 0.0)),
            "chorusEnabled": self.chorusEnabled.IsChecked(),
            "chorusMode": ("I", "II", "I+II")[max(0, self.chorusMode.GetSelection())],
            "chorusDepth": _numericValue(self.chorusDepth, self._originalSettings["chorusDepth"]),
            "chorusRate": _numericValue(self.chorusRate, self._originalSettings.get("chorusRate", 50.0)),
            "chorusWidth": _numericValue(self.chorusWidth, self._originalSettings.get("chorusWidth", 100.0)),
            "chorusMix": _numericValue(self.chorusMix, self._originalSettings["chorusMix"]),
            "delayEnabled": self.delayEnabled.IsChecked(),
            "delayTailEnabled": self.delayTailEnabled.IsChecked(),
            "delayBeforeReverb": self.delayBeforeReverb.IsChecked(),
            "chainBlend": _numericValue(self.chainBlend, self._originalSettings.get("chainBlend", 0.0)),
            "delayTimeMs": _numericValue(self.delayTime, self._originalSettings["delayTimeMs"]),
            "delayFeedback": _numericValue(self.delayFeedback, self._originalSettings["delayFeedback"]),
            "delayMix": _numericValue(self.delayMix, self._originalSettings["delayMix"]),
            "delayPingPong": _numericValue(self.delayPingPong, self._originalSettings["delayPingPong"]),
            "reverbEnabled": self.reverbEnabled.IsChecked(),
            "reverbTailEnabled": self.reverbTailEnabled.IsChecked(),
            "reverbSize": _numericValue(self.reverbSize, self._originalSettings["reverbSize"]),
            "reverbDecaySec": _numericValue(self.reverbDecay, self._originalSettings["reverbDecaySec"]),
            "reverbDamping": _numericValue(self.reverbDamping, self._originalSettings["reverbDamping"]),
            "reverbMix": _numericValue(self.reverbMix, self._originalSettings["reverbMix"]),
            "limiterEnabled": self.limiterEnabled.IsChecked(),
            "limiterCeilingDb": _numericValue(self.limiterCeiling, self._originalSettings["limiterCeilingDb"]),
            "limiterReleaseMs": _numericValue(self.limiterRelease, self._originalSettings["limiterReleaseMs"]),
        }
        for i, (freq, gain, q) in enumerate(self.bands, 1):
            values[f"band{i}Freq"] = _numericValue(freq, self._originalSettings[f"band{i}Freq"])
            values[f"band{i}Gain"] = _numericValue(gain, self._originalSettings[f"band{i}Gain"])
            values[f"band{i}Q"] = _numericValue(q, self._originalSettings[f"band{i}Q"])
        return values

    def _bindLiveControls(self):
        self.enabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.target.Bind(wx.EVT_CHOICE, self._onLiveChanged)
        self.audioDeviceLock.Bind(wx.EVT_CHECKBOX, self._onAudioDeviceLockChanged)
        self.audioDevice.Bind(wx.EVT_CHOICE, self._onAudioDeviceChanged)
        self.compressorEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.compressorRatio.Bind(wx.EVT_CHOICE, self._onLiveChanged)
        self.panLfoWave.Bind(wx.EVT_CHOICE, self._onLiveChanged)
        self.lfoMode.Bind(wx.EVT_CHOICE, self._onLiveChanged)
        self.chorusEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.chorusMode.Bind(wx.EVT_CHOICE, self._onLiveChanged)
        self.delayEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.delayTailEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.delayBeforeReverb.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.reverbEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.reverbTailEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)
        self.limiterEnabled.Bind(wx.EVT_CHECKBOX, self._onLiveChanged)

    def _onAudioDeviceLockChanged(self, evt):
        checked = self.audioDeviceLock.IsChecked()
        self.audioDevice.Enable(checked)
        if checked:
            if not self._audioDeviceIds:
                self.audioDeviceLock.SetValue(False)
                self.audioDevice.Disable()
                ui.message(_("No audio output devices are available."))
                return
            selected = self.audioDevice.GetSelection()
            if selected < 0:
                self.audioDevice.SetSelection(0)
                selected = 0
            selectedId = self._audioDeviceIds[selected]
            if selectedId not in self._audioDeviceAvailableIds:
                # The stored card is currently absent. Keep the checkbox visible as
                # requested, but do not apply an unsafe routing change. The user can
                # Tab to Audio Device and choose an available endpoint.
                ui.message(_("Audio device not available: {name}").format(name=self._audioDeviceNames[selected]))
                return
        self._applyLiveFromControls()
        evt.Skip()

    def _onAudioDeviceChanged(self, evt):
        selected = self.audioDevice.GetSelection()
        if self.audioDeviceLock.IsChecked() and 0 <= selected < len(self._audioDeviceIds):
            if self._audioDeviceIds[selected] not in self._audioDeviceAvailableIds:
                ui.message(_("Audio device not available: {name}").format(name=self._audioDeviceNames[selected]))
                return
        self._applyLiveFromControls()
        evt.Skip()

    def _applyLiveFromControls(self):
        # Numeric ComboBoxes call this directly after keyboard or valid text edits.
        _applySettings(self._valuesFromControls(), save=False)
        if not self._loadingPreset and hasattr(self, "preset"):
            self.preset.SetSelection(0)

    def _onLiveChanged(self, evt):
        # Apply immediately so the very next NVDA utterance is heard through the new EQ.
        self._applyLiveFromControls()
        evt.Skip()

    def _restoreOriginal(self):
        if self._committed or self._restored:
            return
        self._restored = True
        restoreValues = (
            self._restoreSettings
            if self._customActionsMode
            else self._originalSettings
        )
        _applySettings(restoreValues, save=False)

    def _onStatus(self, evt):
        streamInfo, state, chunks, _bytesProcessed = _audioRuntime.getRuntimeStatus()
        ui.message(
            _("{streamInfo} Status: {state}. Processed chunks: {chunks}.").format(
                streamInfo=streamInfo, state=state, chunks=chunks
            )
        )

    def _onOk(self, evt):
        values = self._valuesFromControls()
        available, _deviceId, deviceName = _lockedAudioDeviceStatus(values, forceRefresh=True)
        if not available:
            ui.message(_("Audio device not available: {name}").format(name=deviceName))
            self.audioDevice.SetFocus()
            return
        self._committed = True
        if self._customActionsMode:
            # Custom Actions owns this snapshot.  Keep live preview while the
            # editor is open, but never overwrite Audio Processor's persistent
            # configuration just because a Custom Action row was edited.
            self._customActionsResult = _normalizePresetSettings(values)
            self._restored = True
            _applySettings(self._restoreSettings, save=False)
        else:
            _updateSettings(values)
        self.EndModal(wx.ID_OK)

    def _onCancel(self, evt):
        self._restoreOriginal()
        self.EndModal(wx.ID_CANCEL)

    def _onClose(self, evt):
        self._restoreOriginal()
        evt.Skip()


_PARAM_META = {
    "lowPassHz": (20.0, 20000.0, 10.0, "Low Pass", "Hz", 0),
    "lowPassResonance": (0.0, 100.0, 5.0, "Low Pass Resonance", "", 0),
    "lowPassLfoDepth": (-100.0, 100.0, 5.0, "Low Pass LFO Depth", "", 0),
    "band1Freq": (20.0, 20000.0, 10.0, "Band 1 Frequency", "Hz", 0),
    "band1Gain": (-24.0, 24.0, 0.5, "Band 1 Gain", "dB", 1),
    "band1Q": (0.1, 20.0, 0.1, "Band 1 Q", "", 2),
    "band2Freq": (20.0, 20000.0, 10.0, "Band 2 Frequency", "Hz", 0),
    "band2Gain": (-24.0, 24.0, 0.5, "Band 2 Gain", "dB", 1),
    "band2Q": (0.1, 20.0, 0.1, "Band 2 Q", "", 2),
    "band3Freq": (20.0, 20000.0, 10.0, "Band 3 Frequency", "Hz", 0),
    "band3Gain": (-24.0, 24.0, 0.5, "Band 3 Gain", "dB", 1),
    "band3Q": (0.1, 20.0, 0.1, "Band 3 Q", "", 2),
    "band4Freq": (20.0, 20000.0, 10.0, "Band 4 Frequency", "Hz", 0),
    "band4Gain": (-24.0, 24.0, 0.5, "Band 4 Gain", "dB", 1),
    "band4Q": (0.1, 20.0, 0.1, "Band 4 Q", "", 2),
    "highPassHz": (20.0, 20000.0, 10.0, "High Pass", "Hz", 0),
    "highPassResonance": (0.0, 100.0, 5.0, "High Pass Resonance", "", 0),
    "highPassLfoDepth": (-100.0, 100.0, 5.0, "High Pass LFO Depth", "", 0),
    "masterGainDb": (-60.0, 12.0, 0.5, "Master Gain", "dB", 1),
    "compressorInputDb": (-12.0, 24.0, 0.5, "1176 Input", "dB", 1),
    "compressorOutputDb": (-24.0, 24.0, 0.5, "1176 Output", "dB", 1),
    "compressorAttackUs": (20.0, 800.0, 20.0, "1176 Attack", "microseconds", 0),
    "compressorReleaseMs": (50.0, 1100.0, 25.0, "1176 Release", "ms", 0),
    "pan": (-100.0, 100.0, 5.0, "Pan", "", 0),
    "panLfoRateHz": (0.01, 80.0, 0.05, "LFO Rate", "Hz", 2),
    "panLfoSmooth": (0.0, 100.0, 5.0, "LFO Smoothing", "", 0),
    "panLfoDepth": (-100.0, 100.0, 5.0, "Pan LFO Depth", "", 0),
    "chorusDepth": (0.0, 100.0, 5.0, "Chorus Depth", "", 0),
    "chorusRate": (0.0, 100.0, 5.0, "Chorus Rate", "", 0),
    "chorusWidth": (0.0, 100.0, 5.0, "Chorus Width", "", 0),
    "chorusMix": (0.0, 100.0, 5.0, "Chorus Send", "", 0),
    "delayTimeMs": (1.0, 1000.0, 10.0, "Delay Time", "ms", 0),
    "delayFeedback": (0.0, 95.0, 5.0, "Delay Feedback", "", 0),
    "delayMix": (0.0, 100.0, 5.0, "Delay Send", "", 0),
    "delayPingPong": (0.0, 100.0, 5.0, "Delay Ping-Pong", "", 0),
    "reverbSize": (0.0, 100.0, 5.0, "Reverb Size", "", 0),
    "reverbDecaySec": (0.1, 5.0, 0.1, "Reverb Decay", "seconds", 2),
    "reverbDamping": (0.0, 100.0, 5.0, "Reverb Damping", "", 0),
    "reverbMix": (0.0, 100.0, 5.0, "Reverb Send", "", 0),
    "limiterCeilingDb": (-24.0, 0.0, 0.1, "Limiter Ceiling", "dB", 1),
    "limiterReleaseMs": (1.0, 2000.0, 5.0, "Limiter Release", "ms", 0),
}


def _changeNumericSetting(key, direction):
    minimum, maximum, step, label, unit, digits = _PARAM_META[key]
    settings, settingsRevision = _getSettingsSnapshot()
    current = float(settings.get(key, _DEFAULTS[key]))
    value = current + float(direction) * step
    value = max(minimum, min(maximum, value))
    value = round(value, digits)
    settings[key] = value
    _updateSettings(settings)
    if digits == 0:
        spoken = str(int(round(value)))
    else:
        spoken = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    suffix = f" {unit}" if unit else ""
    ui.message("{label} {value}{unit}".format(label=label, value=spoken, unit=suffix))


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("Audio Processor")

    def __init__(self):
        super().__init__()
        self._menuItem = None
        # Keep exactly one Audio Processor dialog alive. wx modal dialogs run their own
        # event loop, so the global NVDA+Alt+E gesture can be received again while
        # ShowModal() is still active. Track both the live dialog and the short
        # construction window to prevent a second instance from being created.
        self._dialog = None
        self._dialogOpening = False
        if getattr(globalVars.appArgs, "secure", False):
            return
        _loadSettings()
        try:
            _migrateLegacyPresetDirectory()
            os.makedirs(_presetDirectory(), exist_ok=True)
        except Exception:
            log.debugWarning("Audio Processor: unable to create or migrate user preset directory", exc_info=True)
        _audioRuntime.install(_getSettingsSnapshot)
        try:
            # Speech is initialized before global plugins. Recreate it only when the
            # current settings genuinely require a forced stereo path. Pure EQ, gain,
            # compressor or limiter can therefore leave a mono synth completely mono.
            settings, settingsRevision = _getSettingsSnapshot()
            if bool(settings.get("audioDeviceLock", False)):
                available, _deviceId, deviceName = _lockedAudioDeviceStatus(settings, forceRefresh=True)
                if available:
                    wx.CallAfter(_reloadNvdaAudioRouting)
                else:
                    log.warning("Audio Processor: locked audio device unavailable at startup: %s", deviceName)
            else:
                needsStereo = _settingsNeedStereoForPurpose(settings, nvwave.AudioPurpose.SPEECH)
                existingMonoSpeech = any(
                    int(getattr(p, "channels", 0) or 0) == 1
                    and getattr(p, "_purpose", None) is nvwave.AudioPurpose.SPEECH
                    for p in list(nvwave.WavePlayer._instances.values())
                )
                if needsStereo and existingMonoSpeech:
                    wx.CallAfter(_reloadCurrentSynthForChannelMode)
        except Exception:
            log.debugWarning("Audio Processor: unable to schedule synth channel mode reload", exc_info=True)
        try:
            menu = gui.mainFrame.sysTrayIcon.preferencesMenu
            self._menuItem = menu.Append(wx.ID_ANY, _("Audio Processor..."))
            gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._onMenu, self._menuItem)
        except Exception:
            log.debugWarning("Audio Processor: unable to add Preferences menu item", exc_info=True)

    def event_gainFocus(self, obj, nextHandler):
        """Arm Trigger when the real system/NVDA focus changes control."""
        _audioRuntime.requestSharedLfoTrigger()
        nextHandler()

    def event_becomeNavigatorObject(self, obj, nextHandler, **kwargs):
        """Arm Trigger for an independent NVDA Navigator Object change.

        Focus-following navigator changes are already covered by gainFocus, so
        only independent navigator movement needs a second source here. Accept
        event keyword arguments generically for compatibility across NVDA builds.
        """
        if not bool(kwargs.get("isFocus", False)):
            _audioRuntime.requestSharedLfoTrigger()
        nextHandler()

    def _focusExistingDialog(self):
        """Bring the already open Audio Processor dialog forward without recreating it."""
        dlg = self._dialog
        if dlg is None:
            return False
        try:
            if dlg.IsBeingDeleted():
                self._dialog = None
                return False
            dlg.Raise()
            # Preserve the currently focused child whenever possible. If focus has
            # escaped the dialog, SetFocus brings keyboard navigation back to it.
            focused = wx.Window.FindFocus()
            try:
                focusInside = focused is not None and (focused is dlg or dlg.IsDescendant(focused))
            except Exception:
                focusInside = False
            if not focusInside:
                dlg.SetFocus()
            return True
        except Exception:
            # The wx object may have just been destroyed between events. Clear the
            # stale reference so a later invocation can open a fresh dialog.
            self._dialog = None
            return False

    def _showDialog(self):
        # ShowModal() starts a nested wx event loop. A second NVDA+Alt+E can therefore
        # reach this method while the first dialog is still open. Never create a
        # second instance: just return focus to the existing one.  The same rule
        # applies while Custom Actions is editing a detached processor snapshot.
        if _focusCustomActionsEditDialog():
            return
        if self._focusExistingDialog():
            return
        if self._dialogOpening:
            return

        self._dialogOpening = True
        popupPrepared = False
        dlg = None
        try:
            gui.mainFrame.prePopup()
            popupPrepared = True
            dlg = AudioEQDialog(gui.mainFrame)
            self._dialog = dlg
            self._dialogOpening = False
            dlg.ShowModal()
        except Exception as e:
            log.error("Audio Processor: unable to open dialog", exc_info=True)
            try:
                gui.messageBox(
                    _("Unable to open Audio Processor.\n\n{errorType}: {error}").format(errorType=type(e).__name__, error=e),
                    _("Audio Processor"),
                    wx.OK | wx.ICON_ERROR,
                )
            except Exception:
                ui.message(_("Unable to open Audio Processor. Check the NVDA log."))
        finally:
            self._dialogOpening = False
            if dlg is not None:
                if self._dialog is dlg:
                    self._dialog = None
                try:
                    dlg.Destroy()
                except Exception:
                    pass
            if popupPrepared:
                gui.mainFrame.postPopup()

    def _onMenu(self, evt):
        # Let the wx menu event return before entering a modal dialog.
        wx.CallAfter(self._showDialog)

    @scriptHandler.script(
        description=_("Open Audio Processor settings."),
        gesture="kb:NVDA+alt+e",
        category=_("Audio Processor"),
    )
    def script_openAudioEQ(self, gesture):
        # Do not block NVDA input/event processing inside the gesture handler.
        wx.CallAfter(self._showDialog)

    @scriptHandler.script(
        description=_("Enable or disable Audio Processor."),
        gesture="kb:NVDA+alt+q",
        category=_("Audio Processor"),
    )
    def script_toggleAudioEQ(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        oldNeedsStereo = _settingsNeedStereoForPurpose(settings, nvwave.AudioPurpose.SPEECH)
        settings["enabled"] = not bool(settings.get("enabled", True))
        newNeedsStereo = _settingsNeedStereoForPurpose(settings, nvwave.AudioPurpose.SPEECH)
        _updateSettings(settings)
        message = _("Audio Processor enabled") if settings["enabled"] else _("Audio Processor disabled")
        if oldNeedsStereo != newNeedsStereo:
            # _updateSettings has just queued the synth reload needed for a mono/stereo
            # mode change. Announcing immediately would let that reload cancel the
            # feedback speech. Queue one extra turn so the message is spoken only
            # after the reload has completed.
            wx.CallAfter(lambda: wx.CallAfter(ui.message, message))
        else:
            ui.message(message)

    @scriptHandler.script(
        description=_("Report the format of the last intercepted audio stream."),
        category=_("Audio Processor"),
    )
    def script_reportAudioEQStream(self, gesture):
        ui.message(_audioRuntime.getLastStreamInfo())

    @scriptHandler.script(
        description=_("Copy the last Reverb/Delay tail timing diagnostic to the clipboard."),
        gesture="kb:NVDA+alt+r",
        category=_("Audio Processor"),
    )
    def script_reportAudioEQTailTiming(self, gesture):
        # Snapshot first: the confirmation speech generated below must not replace
        # the failing utterance that the user is trying to diagnose.
        report = _audioRuntime.tailDiagReport()
        try:
            api.copyToClip(report)
            ui.message(_("Tail timing diagnostic copied to clipboard"))
        except Exception:
            ui.message(report)

    @scriptHandler.script(description=_("Select the next audio type to process."), category=_("Audio Processor"))
    def script_nextAudioEQTarget(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        values = ["all", "speech", "sounds"]
        labels = [_("All NVDA audio"), _("Speech only"), _("Sounds only")]
        try:
            index = values.index(settings.get("target", "all"))
        except ValueError:
            index = 0
        index = (index + 1) % len(values)
        settings["target"] = values[index]
        _updateSettings(settings)
        ui.message(_("Process {target}").format(target=labels[index]))

    @scriptHandler.script(description=_("Select the previous audio type to process."), category=_("Audio Processor"))
    def script_previousAudioEQTarget(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        values = ["all", "speech", "sounds"]
        labels = [_("All NVDA audio"), _("Speech only"), _("Sounds only")]
        try:
            index = values.index(settings.get("target", "all"))
        except ValueError:
            index = 0
        index = (index - 1) % len(values)
        settings["target"] = values[index]
        _updateSettings(settings)
        ui.message(_("Process {target}").format(target=labels[index]))

    @scriptHandler.script(description=_("Enable or disable 1176 Compressor."), category=_("Audio Processor"))
    def script_toggleAudioEQCompressor1176(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["compressorEnabled"] = not bool(settings.get("compressorEnabled", False))
        _updateSettings(settings)
        ui.message(_("1176 Compressor enabled") if settings["compressorEnabled"] else _("1176 Compressor disabled"))

    @scriptHandler.script(description=_("Select the next 1176 Ratio."), category=_("Audio Processor"))
    def script_nextAudioEQCompressorRatio(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        ratios = ["4:1", "8:1", "12:1", "20:1", "All"]
        try:
            index = ratios.index(str(settings.get("compressorRatio", "4:1")))
        except ValueError:
            index = 0
        index = (index + 1) % len(ratios)
        settings["compressorRatio"] = ratios[index]
        _updateSettings(settings)
        ratioLabel = "All" if ratios[index] == "All" else ratios[index]
        ui.message("1176 Ratio {ratio}".format(ratio=ratioLabel))

    @scriptHandler.script(description=_("Select the previous 1176 Ratio."), category=_("Audio Processor"))
    def script_previousAudioEQCompressorRatio(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        ratios = ["4:1", "8:1", "12:1", "20:1", "All"]
        try:
            index = ratios.index(str(settings.get("compressorRatio", "4:1")))
        except ValueError:
            index = 0
        index = (index - 1) % len(ratios)
        settings["compressorRatio"] = ratios[index]
        _updateSettings(settings)
        ratioLabel = "All" if ratios[index] == "All" else ratios[index]
        ui.message("1176 Ratio {ratio}".format(ratio=ratioLabel))

    @scriptHandler.script(description=_("Increase 1176 Input."), category=_("Audio Processor"))
    def script_increaseAudioEQCompressorInput(self, gesture):
        _changeNumericSetting("compressorInputDb", 1)

    @scriptHandler.script(description=_("Decrease 1176 Input."), category=_("Audio Processor"))
    def script_decreaseAudioEQCompressorInput(self, gesture):
        _changeNumericSetting("compressorInputDb", -1)

    @scriptHandler.script(description=_("Increase 1176 Output."), category=_("Audio Processor"))
    def script_increaseAudioEQCompressorOutput(self, gesture):
        _changeNumericSetting("compressorOutputDb", 1)

    @scriptHandler.script(description=_("Decrease 1176 Output."), category=_("Audio Processor"))
    def script_decreaseAudioEQCompressorOutput(self, gesture):
        _changeNumericSetting("compressorOutputDb", -1)

    @scriptHandler.script(description=_("Increase 1176 Attack."), category=_("Audio Processor"))
    def script_increaseAudioEQCompressorAttack(self, gesture):
        _changeNumericSetting("compressorAttackUs", 1)

    @scriptHandler.script(description=_("Decrease 1176 Attack."), category=_("Audio Processor"))
    def script_decreaseAudioEQCompressorAttack(self, gesture):
        _changeNumericSetting("compressorAttackUs", -1)

    @scriptHandler.script(description=_("Increase 1176 Release."), category=_("Audio Processor"))
    def script_increaseAudioEQCompressorRelease(self, gesture):
        _changeNumericSetting("compressorReleaseMs", 1)

    @scriptHandler.script(description=_("Decrease 1176 Release."), category=_("Audio Processor"))
    def script_decreaseAudioEQCompressorRelease(self, gesture):
        _changeNumericSetting("compressorReleaseMs", -1)

    @scriptHandler.script(description=_("Enable or disable Stereo Chorus."), category=_("Audio Processor"))
    def script_toggleAudioEQChorus(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["chorusEnabled"] = not bool(settings.get("chorusEnabled", False))
        _updateSettings(settings)
        ui.message(_("Stereo Chorus enabled") if settings["chorusEnabled"] else _("Stereo Chorus disabled"))

    @scriptHandler.script(description=_("Select the next Chorus Mode."), category=_("Audio Processor"))
    def script_nextAudioEQChorusMode(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        modes = ["I", "II", "I+II"]
        try:
            index = modes.index(str(settings.get("chorusMode", "I")))
        except ValueError:
            index = 0
        index = (index + 1) % len(modes)
        settings["chorusMode"] = modes[index]
        _updateSettings(settings)
        ui.message("Chorus Mode {mode}".format(mode=modes[index]))

    @scriptHandler.script(description=_("Select the previous Chorus Mode."), category=_("Audio Processor"))
    def script_previousAudioEQChorusMode(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        modes = ["I", "II", "I+II"]
        try:
            index = modes.index(str(settings.get("chorusMode", "I")))
        except ValueError:
            index = 0
        index = (index - 1) % len(modes)
        settings["chorusMode"] = modes[index]
        _updateSettings(settings)
        ui.message("Chorus Mode {mode}".format(mode=modes[index]))

    @scriptHandler.script(description=_("Increase Chorus Depth."), category=_("Audio Processor"))
    def script_increaseAudioEQChorusDepth(self, gesture):
        _changeNumericSetting("chorusDepth", 1)

    @scriptHandler.script(description=_("Decrease Chorus Depth."), category=_("Audio Processor"))
    def script_decreaseAudioEQChorusDepth(self, gesture):
        _changeNumericSetting("chorusDepth", -1)

    @scriptHandler.script(description=_("Increase Chorus Rate."), category=_("Audio Processor"))
    def script_increaseAudioEQChorusRate(self, gesture):
        _changeNumericSetting("chorusRate", 1)

    @scriptHandler.script(description=_("Decrease Chorus Rate."), category=_("Audio Processor"))
    def script_decreaseAudioEQChorusRate(self, gesture):
        _changeNumericSetting("chorusRate", -1)

    @scriptHandler.script(description=_("Increase Chorus Width."), category=_("Audio Processor"))
    def script_increaseAudioEQChorusWidth(self, gesture):
        _changeNumericSetting("chorusWidth", 1)

    @scriptHandler.script(description=_("Decrease Chorus Width."), category=_("Audio Processor"))
    def script_decreaseAudioEQChorusWidth(self, gesture):
        _changeNumericSetting("chorusWidth", -1)

    @scriptHandler.script(description=_("Increase Chorus Send."), category=_("Audio Processor"))
    def script_increaseAudioEQChorusMix(self, gesture):
        _changeNumericSetting("chorusMix", 1)

    @scriptHandler.script(description=_("Decrease Chorus Send."), category=_("Audio Processor"))
    def script_decreaseAudioEQChorusMix(self, gesture):
        _changeNumericSetting("chorusMix", -1)

    @scriptHandler.script(description=_("Enable or disable Stereo Delay."), category=_("Audio Processor"))
    def script_toggleAudioEQDelay(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["delayEnabled"] = not bool(settings.get("delayEnabled", False))
        _updateSettings(settings)
        ui.message(_("Stereo Delay enabled") if settings["delayEnabled"] else _("Stereo Delay disabled"))

    @scriptHandler.script(description=_("Enable or disable Delay tail."), category=_("Audio Processor"))
    def script_toggleAudioEQDelayTail(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["delayTailEnabled"] = not bool(settings.get("delayTailEnabled", False))
        _updateSettings(settings)
        ui.message(_("Delay tail enabled") if settings["delayTailEnabled"] else _("Delay tail disabled"))

    @scriptHandler.script(description=_("Increase Delay Time."), category=_("Audio Processor"))
    def script_increaseAudioEQDelayTime(self, gesture):
        _changeNumericSetting("delayTimeMs", 1)

    @scriptHandler.script(description=_("Decrease Delay Time."), category=_("Audio Processor"))
    def script_decreaseAudioEQDelayTime(self, gesture):
        _changeNumericSetting("delayTimeMs", -1)

    @scriptHandler.script(description=_("Increase Delay Feedback."), category=_("Audio Processor"))
    def script_increaseAudioEQDelayFeedback(self, gesture):
        _changeNumericSetting("delayFeedback", 1)

    @scriptHandler.script(description=_("Decrease Delay Feedback."), category=_("Audio Processor"))
    def script_decreaseAudioEQDelayFeedback(self, gesture):
        _changeNumericSetting("delayFeedback", -1)

    @scriptHandler.script(description=_("Increase Delay Send."), category=_("Audio Processor"))
    def script_increaseAudioEQDelayMix(self, gesture):
        _changeNumericSetting("delayMix", 1)

    @scriptHandler.script(description=_("Decrease Delay Send."), category=_("Audio Processor"))
    def script_decreaseAudioEQDelayMix(self, gesture):
        _changeNumericSetting("delayMix", -1)

    @scriptHandler.script(description=_("Increase Delay Ping-Pong."), category=_("Audio Processor"))
    def script_increaseAudioEQDelayPingPong(self, gesture):
        _changeNumericSetting("delayPingPong", 1)

    @scriptHandler.script(description=_("Decrease Delay Ping-Pong."), category=_("Audio Processor"))
    def script_decreaseAudioEQDelayPingPong(self, gesture):
        _changeNumericSetting("delayPingPong", -1)

    @scriptHandler.script(description=_("Enable or disable Stereo Reverb."), category=_("Audio Processor"))
    def script_toggleAudioEQReverb(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["reverbEnabled"] = not bool(settings.get("reverbEnabled", False))
        _updateSettings(settings)
        ui.message(_("Stereo Reverb enabled") if settings["reverbEnabled"] else _("Stereo Reverb disabled"))

    @scriptHandler.script(description=_("Enable or disable Reverb tail."), category=_("Audio Processor"))
    def script_toggleAudioEQReverbTail(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["reverbTailEnabled"] = not bool(settings.get("reverbTailEnabled", False))
        _updateSettings(settings)
        ui.message(_("Reverb tail enabled") if settings["reverbTailEnabled"] else _("Reverb tail disabled"))

    @scriptHandler.script(description=_("Increase Reverb Size."), category=_("Audio Processor"))
    def script_increaseAudioEQReverbSize(self, gesture):
        _changeNumericSetting("reverbSize", 1)

    @scriptHandler.script(description=_("Decrease Reverb Size."), category=_("Audio Processor"))
    def script_decreaseAudioEQReverbSize(self, gesture):
        _changeNumericSetting("reverbSize", -1)

    @scriptHandler.script(description=_("Increase Reverb Decay."), category=_("Audio Processor"))
    def script_increaseAudioEQReverbDecay(self, gesture):
        _changeNumericSetting("reverbDecaySec", 1)

    @scriptHandler.script(description=_("Decrease Reverb Decay."), category=_("Audio Processor"))
    def script_decreaseAudioEQReverbDecay(self, gesture):
        _changeNumericSetting("reverbDecaySec", -1)

    @scriptHandler.script(description=_("Increase Reverb Damping."), category=_("Audio Processor"))
    def script_increaseAudioEQReverbDamping(self, gesture):
        _changeNumericSetting("reverbDamping", 1)

    @scriptHandler.script(description=_("Decrease Reverb Damping."), category=_("Audio Processor"))
    def script_decreaseAudioEQReverbDamping(self, gesture):
        _changeNumericSetting("reverbDamping", -1)

    @scriptHandler.script(description=_("Increase Reverb Send."), category=_("Audio Processor"))
    def script_increaseAudioEQReverbMix(self, gesture):
        _changeNumericSetting("reverbMix", 1)

    @scriptHandler.script(description=_("Decrease Reverb Send."), category=_("Audio Processor"))
    def script_decreaseAudioEQReverbMix(self, gesture):
        _changeNumericSetting("reverbMix", -1)

    @scriptHandler.script(description=_("Enable or disable Limiter."), category=_("Audio Processor"))
    def script_toggleAudioEQLimiter(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        settings["limiterEnabled"] = not bool(settings.get("limiterEnabled", True))
        _updateSettings(settings)
        ui.message(_("Limiter enabled") if settings["limiterEnabled"] else _("Limiter disabled"))

    @scriptHandler.script(description=_("Increase Low Pass Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQLowPass(self, gesture):
        _changeNumericSetting("lowPassHz", 1)

    @scriptHandler.script(description=_("Decrease Low Pass Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQLowPass(self, gesture):
        _changeNumericSetting("lowPassHz", -1)

    @scriptHandler.script(description=_("Increase Low Pass Resonance."), category=_("Audio Processor"))
    def script_increaseAudioEQLowPassResonance(self, gesture):
        _changeNumericSetting("lowPassResonance", 1)

    @scriptHandler.script(description=_("Decrease Low Pass Resonance."), category=_("Audio Processor"))
    def script_decreaseAudioEQLowPassResonance(self, gesture):
        _changeNumericSetting("lowPassResonance", -1)

    @scriptHandler.script(description=_("Increase Band 1 Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQBand1Freq(self, gesture):
        _changeNumericSetting("band1Freq", 1)

    @scriptHandler.script(description=_("Decrease Band 1 Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand1Freq(self, gesture):
        _changeNumericSetting("band1Freq", -1)

    @scriptHandler.script(description=_("Increase Band 1 Gain."), category=_("Audio Processor"))
    def script_increaseAudioEQBand1Gain(self, gesture):
        _changeNumericSetting("band1Gain", 1)

    @scriptHandler.script(description=_("Decrease Band 1 Gain."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand1Gain(self, gesture):
        _changeNumericSetting("band1Gain", -1)

    @scriptHandler.script(description=_("Increase Band 1 Q."), category=_("Audio Processor"))
    def script_increaseAudioEQBand1Q(self, gesture):
        _changeNumericSetting("band1Q", 1)

    @scriptHandler.script(description=_("Decrease Band 1 Q."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand1Q(self, gesture):
        _changeNumericSetting("band1Q", -1)

    @scriptHandler.script(description=_("Increase Band 2 Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQBand2Freq(self, gesture):
        _changeNumericSetting("band2Freq", 1)

    @scriptHandler.script(description=_("Decrease Band 2 Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand2Freq(self, gesture):
        _changeNumericSetting("band2Freq", -1)

    @scriptHandler.script(description=_("Increase Band 2 Gain."), category=_("Audio Processor"))
    def script_increaseAudioEQBand2Gain(self, gesture):
        _changeNumericSetting("band2Gain", 1)

    @scriptHandler.script(description=_("Decrease Band 2 Gain."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand2Gain(self, gesture):
        _changeNumericSetting("band2Gain", -1)

    @scriptHandler.script(description=_("Increase Band 2 Q."), category=_("Audio Processor"))
    def script_increaseAudioEQBand2Q(self, gesture):
        _changeNumericSetting("band2Q", 1)

    @scriptHandler.script(description=_("Decrease Band 2 Q."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand2Q(self, gesture):
        _changeNumericSetting("band2Q", -1)

    @scriptHandler.script(description=_("Increase Band 3 Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQBand3Freq(self, gesture):
        _changeNumericSetting("band3Freq", 1)

    @scriptHandler.script(description=_("Decrease Band 3 Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand3Freq(self, gesture):
        _changeNumericSetting("band3Freq", -1)

    @scriptHandler.script(description=_("Increase Band 3 Gain."), category=_("Audio Processor"))
    def script_increaseAudioEQBand3Gain(self, gesture):
        _changeNumericSetting("band3Gain", 1)

    @scriptHandler.script(description=_("Decrease Band 3 Gain."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand3Gain(self, gesture):
        _changeNumericSetting("band3Gain", -1)

    @scriptHandler.script(description=_("Increase Band 3 Q."), category=_("Audio Processor"))
    def script_increaseAudioEQBand3Q(self, gesture):
        _changeNumericSetting("band3Q", 1)

    @scriptHandler.script(description=_("Decrease Band 3 Q."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand3Q(self, gesture):
        _changeNumericSetting("band3Q", -1)

    @scriptHandler.script(description=_("Increase Band 4 Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQBand4Freq(self, gesture):
        _changeNumericSetting("band4Freq", 1)

    @scriptHandler.script(description=_("Decrease Band 4 Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand4Freq(self, gesture):
        _changeNumericSetting("band4Freq", -1)

    @scriptHandler.script(description=_("Increase Band 4 Gain."), category=_("Audio Processor"))
    def script_increaseAudioEQBand4Gain(self, gesture):
        _changeNumericSetting("band4Gain", 1)

    @scriptHandler.script(description=_("Decrease Band 4 Gain."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand4Gain(self, gesture):
        _changeNumericSetting("band4Gain", -1)

    @scriptHandler.script(description=_("Increase Band 4 Q."), category=_("Audio Processor"))
    def script_increaseAudioEQBand4Q(self, gesture):
        _changeNumericSetting("band4Q", 1)

    @scriptHandler.script(description=_("Decrease Band 4 Q."), category=_("Audio Processor"))
    def script_decreaseAudioEQBand4Q(self, gesture):
        _changeNumericSetting("band4Q", -1)

    @scriptHandler.script(description=_("Increase High Pass Frequency."), category=_("Audio Processor"))
    def script_increaseAudioEQHighPass(self, gesture):
        _changeNumericSetting("highPassHz", 1)

    @scriptHandler.script(description=_("Decrease High Pass Frequency."), category=_("Audio Processor"))
    def script_decreaseAudioEQHighPass(self, gesture):
        _changeNumericSetting("highPassHz", -1)

    @scriptHandler.script(description=_("Increase High Pass Resonance."), category=_("Audio Processor"))
    def script_increaseAudioEQHighPassResonance(self, gesture):
        _changeNumericSetting("highPassResonance", 1)

    @scriptHandler.script(description=_("Decrease High Pass Resonance."), category=_("Audio Processor"))
    def script_decreaseAudioEQHighPassResonance(self, gesture):
        _changeNumericSetting("highPassResonance", -1)


    @scriptHandler.script(description=_("Increase Master Gain."), category=_("Audio Processor"))
    def script_increaseAudioEQMasterGain(self, gesture):
        _changeNumericSetting("masterGainDb", 1)

    @scriptHandler.script(description=_("Decrease Master Gain."), category=_("Audio Processor"))
    def script_decreaseAudioEQMasterGain(self, gesture):
        _changeNumericSetting("masterGainDb", -1)

    @scriptHandler.script(description=_("Move Pan right."), category=_("Audio Processor"))
    def script_increaseAudioEQPan(self, gesture):
        _changeNumericSetting("pan", 1)

    @scriptHandler.script(description=_("Move Pan left."), category=_("Audio Processor"))
    def script_decreaseAudioEQPan(self, gesture):
        _changeNumericSetting("pan", -1)

    @scriptHandler.script(description=_("Select the next LFO Waveform."), category=_("Audio Processor"))
    def script_nextAudioEQPanLfoWave(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        waves = ["Sine", "Tri", "Saw", "Square", "S&H"]
        current = str(settings.get("panLfoWave", "Sine"))
        try:
            index = waves.index(current)
        except ValueError:
            index = 0
        index = (index + 1) % len(waves)
        settings["panLfoWave"] = waves[index]
        _updateSettings(settings)
        waveLabels = {"Sine": "Sine", "Tri": "Triangle", "Saw": "Saw", "Square": "Square", "S&H": "S&H"}
        ui.message("LFO Waveform {wave}".format(wave=waveLabels[waves[index]]))

    @scriptHandler.script(description=_("Select the previous LFO Waveform."), category=_("Audio Processor"))
    def script_previousAudioEQPanLfoWave(self, gesture):
        settings, settingsRevision = _getSettingsSnapshot()
        waves = ["Sine", "Tri", "Saw", "Square", "S&H"]
        current = str(settings.get("panLfoWave", "Sine"))
        try:
            index = waves.index(current)
        except ValueError:
            index = 0
        index = (index - 1) % len(waves)
        settings["panLfoWave"] = waves[index]
        _updateSettings(settings)
        waveLabels = {"Sine": "Sine", "Tri": "Triangle", "Saw": "Saw", "Square": "Square", "S&H": "S&H"}
        ui.message("LFO Waveform {wave}".format(wave=waveLabels[waves[index]]))

    @scriptHandler.script(description=_("Increase LFO Rate."), category=_("Audio Processor"))
    def script_increaseAudioEQPanLfoRate(self, gesture):
        _changeNumericSetting("panLfoRateHz", 1)

    @scriptHandler.script(description=_("Decrease LFO Rate."), category=_("Audio Processor"))
    def script_decreaseAudioEQPanLfoRate(self, gesture):
        _changeNumericSetting("panLfoRateHz", -1)

    @scriptHandler.script(description=_("Increase LFO Smoothing."), category=_("Audio Processor"))
    def script_increaseAudioEQPanLfoSmooth(self, gesture):
        _changeNumericSetting("panLfoSmooth", 1)

    @scriptHandler.script(description=_("Decrease LFO Smoothing."), category=_("Audio Processor"))
    def script_decreaseAudioEQPanLfoSmooth(self, gesture):
        _changeNumericSetting("panLfoSmooth", -1)

    @scriptHandler.script(description=_("Increase Pan LFO Depth."), category=_("Audio Processor"))
    def script_increaseAudioEQPanLfoDepth(self, gesture):
        _changeNumericSetting("panLfoDepth", 1)

    @scriptHandler.script(description=_("Decrease Pan LFO Depth."), category=_("Audio Processor"))
    def script_decreaseAudioEQPanLfoDepth(self, gesture):
        _changeNumericSetting("panLfoDepth", -1)

    @scriptHandler.script(description=_("Increase Low Pass LFO Depth."), category=_("Audio Processor"))
    def script_increaseAudioEQLowPassLfoDepth(self, gesture):
        _changeNumericSetting("lowPassLfoDepth", 1)

    @scriptHandler.script(description=_("Decrease Low Pass LFO Depth."), category=_("Audio Processor"))
    def script_decreaseAudioEQLowPassLfoDepth(self, gesture):
        _changeNumericSetting("lowPassLfoDepth", -1)

    @scriptHandler.script(description=_("Increase High Pass LFO Depth."), category=_("Audio Processor"))
    def script_increaseAudioEQHighPassLfoDepth(self, gesture):
        _changeNumericSetting("highPassLfoDepth", 1)

    @scriptHandler.script(description=_("Decrease High Pass LFO Depth."), category=_("Audio Processor"))
    def script_decreaseAudioEQHighPassLfoDepth(self, gesture):
        _changeNumericSetting("highPassLfoDepth", -1)

    @scriptHandler.script(description=_("Increase Limiter Ceiling."), category=_("Audio Processor"))
    def script_increaseAudioEQLimiterCeiling(self, gesture):
        _changeNumericSetting("limiterCeilingDb", 1)

    @scriptHandler.script(description=_("Decrease Limiter Ceiling."), category=_("Audio Processor"))
    def script_decreaseAudioEQLimiterCeiling(self, gesture):
        _changeNumericSetting("limiterCeilingDb", -1)

    @scriptHandler.script(description=_("Increase Limiter Release."), category=_("Audio Processor"))
    def script_increaseAudioEQLimiterRelease(self, gesture):
        _changeNumericSetting("limiterReleaseMs", 1)

    @scriptHandler.script(description=_("Decrease Limiter Release."), category=_("Audio Processor"))
    def script_decreaseAudioEQLimiterRelease(self, gesture):
        _changeNumericSetting("limiterReleaseMs", -1)

    def terminate(self):
        try:
            _audioRuntime.uninstall()
            if self._menuItem is not None:
                try:
                    gui.mainFrame.sysTrayIcon.preferencesMenu.Remove(self._menuItem)
                except Exception:
                    pass
                self._menuItem = None
        finally:
            super().terminate()
