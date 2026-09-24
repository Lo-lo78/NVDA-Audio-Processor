from __future__ import annotations

import json
import os
import re
import tempfile

import addonHandler
import globalVars
from logHandler import log

addonHandler.initTranslation()


def localizedAddonFolderName():
    """Return a readable, locale-specific folder name for user data."""
    name = str(_("Audio Processor")).strip() or "Audio Processor"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return name or "Audio Processor"


def presetDirectory():
    """User preset folder inside the NVDA user configuration."""
    return os.path.join(globalVars.appArgs.configPath, localizedAddonFolderName(), "presets")


def migrateLegacyPresetDirectory():
    """Move legacy user presets from audioEQ/presets to the localized add-on folder."""
    legacyRoot = os.path.join(globalVars.appArgs.configPath, "audioEQ")
    legacyDirectory = os.path.join(legacyRoot, "presets")
    targetDirectory = presetDirectory()
    if os.path.normcase(os.path.abspath(legacyDirectory)) == os.path.normcase(os.path.abspath(targetDirectory)):
        return
    if not os.path.isdir(legacyDirectory):
        return
    os.makedirs(targetDirectory, exist_ok=True)
    for fileName in os.listdir(legacyDirectory):
        if not fileName.lower().endswith(".json"):
            continue
        sourcePath = os.path.join(legacyDirectory, fileName)
        targetPath = os.path.join(targetDirectory, fileName)
        if not os.path.isfile(sourcePath):
            continue
        if os.path.exists(targetPath):
            base, extension = os.path.splitext(fileName)
            index = 2
            while os.path.exists(targetPath):
                targetPath = os.path.join(targetDirectory, f"{base} {index}{extension}")
                index += 1
        os.replace(sourcePath, targetPath)
    try:
        os.rmdir(legacyDirectory)
    except OSError:
        pass
    try:
        os.rmdir(legacyRoot)
    except OSError:
        pass


def factoryPresetPath():
    return os.path.join(os.path.dirname(__file__), "factoryPresets.json")


def normalizePresetSettings(values, defaults):
    merged = dict(defaults)
    if isinstance(values, dict):
        merged.update({k: v for k, v in values.items() if k in merged})
    return merged


def loadFactoryPresets(defaults):
    presets = []
    try:
        with open(factoryPresetPath(), "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("presets", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            presets.append({
                "kind": "factory",
                "name": name,
                "settings": normalizePresetSettings(item.get("settings", {}), defaults),
            })
    except Exception:
        log.debugWarning("Audio Processor: unable to load factory presets", exc_info=True)
    return presets


def loadUserPresets(defaults):
    presets = []
    directory = presetDirectory()
    try:
        names = sorted(os.listdir(directory), key=str.casefold)
    except FileNotFoundError:
        return presets
    except Exception:
        log.debugWarning("Audio Processor: unable to list user presets", exc_info=True)
        return presets
    for fileName in names:
        if not fileName.lower().endswith(".json"):
            continue
        path = os.path.join(directory, fileName)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            name = str(data.get("name", "")).strip()
            settings = data.get("settings")
            if not name or not isinstance(settings, dict):
                continue
            presets.append({
                "kind": "user",
                "name": name,
                "settings": normalizePresetSettings(settings, defaults),
                "path": path,
            })
        except Exception:
            log.debugWarning("Audio Processor: unable to read user preset %s", path, exc_info=True)
    presets.sort(key=lambda item: item["name"].casefold())
    return presets


def safePresetFileStem(name):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(".")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or "Preset"


def findUserPresetPathByName(name, defaults):
    folded = str(name).casefold()
    for item in loadUserPresets(defaults):
        if item["name"].casefold() == folded:
            return item.get("path")
    return None


def saveUserPreset(name, values, defaults):
    """Atomically save one complete processor state as a named user preset."""
    directory = presetDirectory()
    os.makedirs(directory, exist_ok=True)
    existingPath = findUserPresetPathByName(name, defaults)
    finalPath = existingPath or os.path.join(directory, safePresetFileStem(name) + ".json")
    if existingPath is None and os.path.exists(finalPath):
        base = safePresetFileStem(name)
        index = 2
        while os.path.exists(finalPath):
            finalPath = os.path.join(directory, f"{base} {index}.json")
            index += 1
    payload = {
        "format": 1,
        "name": str(name),
        "settings": normalizePresetSettings(values, defaults),
    }
    tempPath = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=os.path.basename(finalPath) + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            tempPath = f.name
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tempPath, finalPath)
        tempPath = None
        return finalPath
    finally:
        if tempPath:
            try:
                os.remove(tempPath)
            except Exception:
                pass


def deleteUserPreset(item):
    """Delete one user preset file."""
    path = item.get("path") if isinstance(item, dict) else None
    if not path:
        raise ValueError("Missing user preset path")
    os.remove(path)


def renameUserPreset(item, newName, defaults, replaceItem=None):
    """Rename one user preset, preserving its complete processor state."""
    if not isinstance(item, dict) or item.get("kind") != "user":
        raise ValueError("Not a user preset")
    oldPath = item.get("path")
    if not oldPath:
        raise ValueError("Missing user preset path")
    directory = presetDirectory()
    os.makedirs(directory, exist_ok=True)

    replacePath = None
    if isinstance(replaceItem, dict):
        replacePath = replaceItem.get("path")

    if replacePath and os.path.normcase(os.path.abspath(replacePath)) != os.path.normcase(os.path.abspath(oldPath)):
        finalPath = replacePath
    else:
        candidate = os.path.join(directory, safePresetFileStem(newName) + ".json")
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(os.path.abspath(oldPath)):
            finalPath = oldPath
        else:
            finalPath = candidate
            if os.path.exists(finalPath):
                base = safePresetFileStem(newName)
                index = 2
                while os.path.exists(finalPath):
                    finalPath = os.path.join(directory, f"{base} {index}.json")
                    index += 1

    payload = {
        "format": 1,
        "name": str(newName),
        "settings": normalizePresetSettings(item.get("settings", {}), defaults),
    }
    tempPath = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=os.path.basename(finalPath) + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            tempPath = f.name
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tempPath, finalPath)
        tempPath = None
        if os.path.normcase(os.path.abspath(oldPath)) != os.path.normcase(os.path.abspath(finalPath)):
            try:
                os.remove(oldPath)
            except FileNotFoundError:
                pass
        return finalPath
    finally:
        if tempPath:
            try:
                os.remove(tempPath)
            except Exception:
                pass


def configPath(configFile):
    return os.path.join(globalVars.appArgs.configPath, configFile)


def loadSettings(defaults, configFile):
    data = {}
    try:
        with open(configPath(configFile), "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        pass
    except Exception:
        log.debugWarning("Audio Processor: unable to read configuration", exc_info=True)
    merged = dict(defaults)
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def saveSettings(data, configFile):
    tempPath = None
    try:
        path = configPath(configFile)
        configDir = os.path.dirname(path)
        os.makedirs(configDir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=configDir,
            prefix=configFile + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            tempPath = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tempPath, path)
        tempPath = None
    except Exception:
        log.debugWarning("Audio Processor: unable to save configuration", exc_info=True)
    finally:
        if tempPath:
            try:
                os.remove(tempPath)
            except Exception:
                pass
