from __future__ import annotations

import py_compile
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

PACKAGE_TOP_LEVEL = (
    "manifest.ini",
    "LOCALIZATION_GUIDE.txt",
    "globalPlugins",
    "locale",
    "doc",
)


def read_version() -> str:
    # NVDA manifest.ini uses top-level key/value pairs rather than sections.
    for line in (ROOT / "manifest.ini").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("Unable to read version from manifest.ini")


def validate_python() -> None:
    for path in sorted((ROOT / "globalPlugins").rglob("*.py")):
        py_compile.compile(str(path), doraise=True)


def should_include(path: Path) -> bool:
    parts = path.parts
    if "__pycache__" in parts:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return True


def iter_package_files():
    for item in PACKAGE_TOP_LEVEL:
        path = ROOT / item
        if not path.exists():
            raise FileNotFoundError(f"Required package item is missing: {item}")
        if path.is_file():
            yield path
        else:
            for child in sorted(path.rglob("*")):
                if child.is_file() and should_include(child.relative_to(ROOT)):
                    yield child


def build() -> Path:
    validate_python()
    version = read_version()
    DIST.mkdir(exist_ok=True)
    output = DIST / f"AudioProcessor_{version}.nvda-addon"
    if output.exists():
        output.unlink()

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in iter_package_files():
            archive.write(path, path.relative_to(ROOT).as_posix())

    return output


if __name__ == "__main__":
    try:
        result = build()
    except Exception as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(result)
