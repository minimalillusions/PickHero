"""Verify the actual Windows EXE's runtime provenance without executing it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(exe: Path) -> dict:
    archive = CArchiveReader(str(exe))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    failures = []
    checked = []

    def check_bytes(name: str, expected: bytes) -> None:
        checked.append(name)
        if name not in names or archive.extract(names[name]) != expected:
            failures.append("Missing or changed: " + name)

    font = ROOT / "pickhero/assets/fonts/Geist-Regular.ttf"
    check_bytes("pygame/freesansbold.ttf", font.read_bytes())
    check_bytes("licenses/fonts/OFL-Geist.txt",
                (ROOT / "pickhero/assets/fonts/OFL-Geist.txt").read_bytes())
    portaudio = sorted(name for name in names
                       if name.startswith("_sounddevice_data/portaudio-binaries/")
                       and name.lower().endswith(".dll"))
    expected_portaudio = {
        "_sounddevice_data/portaudio-binaries/libportaudio64bit.dll",
        "_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll",
    }
    if set(portaudio) != expected_portaudio:
        failures.append("Unexpected PortAudio architecture/backend set: " + repr(portaudio))
    provenance = json.loads((ROOT / "wheels/aubio-build.json").read_text(encoding="utf-8"))
    for archive_name, source, key in (
        ("licenses/aubio/aubio-0.4.9.tar.gz", ROOT / "wheels/sources/aubio-0.4.9.tar.gz", "source_sha256"),
        ("licenses/aubio/build-aubio-wheel.py", ROOT / "scripts/build-aubio-wheel.py", "recipe_sha256"),
        ("licenses/aubio/BUILD.log", ROOT / "wheels/aubio-build.log", "build_log_sha256"),
    ):
        data = source.read_bytes()
        if digest(data) != provenance[key]:
            failures.append("Local provenance mismatch: " + str(source))
        check_bytes(archive_name, data)
    check_bytes("licenses/aubio/BUILD.json", (ROOT / "wheels/aubio-build.json").read_bytes())
    native = {name for name in names if name.startswith("aubio/") and name.endswith(".pyd")}
    if native != set(provenance["native_sha256"]) or not native:
        failures.append("aubio native module set does not match build report")
    for name in sorted(native):
        checked.append(name)
        if digest(archive.extract(names[name])) != provenance["native_sha256"].get(name):
            failures.append("aubio native module checksum mismatch: " + name)
    wheel = ROOT / "wheels" / provenance["wheel"]
    if digest(wheel.read_bytes()) != provenance["wheel_sha256"]:
        failures.append("Local aubio wheel no longer matches build report")
    return {"passed": not failures, "exe_sha256": digest(exe.read_bytes()),
            "checked": checked, "portaudio": portaudio, "failures": failures,
            "scope": "Artifact integrity and packaging; not publication clearance or a hardware test."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.exe)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
