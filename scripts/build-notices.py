"""Build offline-readable notices from installed runtime metadata, never import audio.

Run with the build interpreter before PyInstaller. No EXE is required. --exe adds
an inventory of that archive; it is NOT evidence about a future build. --verify-exe
checks every local notice byte inside a freshly built archive without executing it.
--fetch refreshes pinned upstream license copies; normal builds are offline.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import ssl
import struct
import sys
import urllib.request
import zlib
import pyexpat

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "licenses"
RUNTIME = {
    "sounddevice": ("MIT", "https://github.com/spatialaudio/python-sounddevice"),
    "aubio": ("GPL-3.0-or-later", "https://github.com/aubio/aubio/tree/0.4.9"),
    "numpy": ("BSD-3-Clause; weitere Lizenzen im Wheel", "https://github.com/numpy/numpy"),
    "PyGuitarPro": ("LGPL-3.0-only", "https://github.com/Perlence/PyGuitarPro"),
    "pygame": ("LGPL-2.1", "https://github.com/pygame/pygame/tree/2.6.1"),
    "defusedxml": ("PSF-2.0", "https://github.com/tiran/defusedxml"),
    "attrs": ("MIT", "https://github.com/python-attrs/attrs"),
    "cffi": ("MIT-0", "https://github.com/python-cffi/cffi"),
}
PYGAME_NOTICES = (
    "FLAC fluidsynth freetype jpeg modplug mpg123 numpy ogg-vorbis opus "
    "opusfile png portmidi sdl2 sdl2_image sdl2_mixer sdl_gfx sse2neon-h tiff webp zlib"
).split()
UPSTREAM = {
    f"pygame-native/LICENSE.{name}.txt":
    f"https://raw.githubusercontent.com/pygame/pygame/2.6.1/docs/licenses/LICENSE.{name}.txt"
    for name in PYGAME_NOTICES
}
UPSTREAM.update({
    "sdl-ttf/LICENSE.txt": "https://raw.githubusercontent.com/libsdl-org/SDL_ttf/release-2.20.1/LICENSE.txt",
    "portaudio/LICENSE.txt": "https://raw.githubusercontent.com/PortAudio/portaudio/v19.7.0/LICENSE.txt",
    "openssl/LICENSE.txt": "https://raw.githubusercontent.com/openssl/openssl/openssl-3.5.9/LICENSE.txt",
    "expat/COPYING.txt": "https://raw.githubusercontent.com/libexpat/libexpat/R_2_8_3/expat/COPYING",
    "libffi/LICENSE-reference.txt": "https://raw.githubusercontent.com/libffi/libffi/v3.4.4/LICENSE",
    "zlib-python/LICENSE.txt": "https://raw.githubusercontent.com/madler/zlib/v1.3.2/LICENSE",
    "freefont/GPL-2.0.txt": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt",
})
FONT_SOURCES = {
    "Geist": "https://github.com/vercel/geist-font",
    "ChakraPetch": "https://github.com/cadsondemak/Chakra-Petch",
    "HankenGrotesk": "https://github.com/HankenDesignCo/Hanken-Grotesk",
    "JetBrainsMono": "https://github.com/JetBrains/JetBrainsMono",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def put(relative: str, data: bytes, source: str, records: list) -> str:
    path = OUT / relative
    if not path.resolve().is_relative_to(OUT.resolve()):
        raise ValueError("notice destination escapes licenses/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    records.append({"path": relative, "sha256": digest(data), "bytes": len(data), "source": source})
    return relative


def package_notices(name: str, records: list) -> list[str]:
    dist = metadata.distribution(name)
    paths = []
    for item in sorted(dist.files or [], key=str):
        # Metadata license files are authoritative for this installed wheel.
        # NumPy also keeps authentic notices in package subdirectories.
        parts = item.parts
        is_notice = any(part.lower() in {"licenses", "license", "copying"} for part in parts)
        is_notice |= item.name.lower().startswith(("license", "copying", "authors"))
        if not is_notice or ".." in parts or item.suffix.lower() in {".py", ".pyc"}:
            continue
        source = Path(dist.locate_file(item))
        if source.is_file():
            dest = f"python/{name.lower()}/{item.as_posix()}"
            paths.append(put(dest, source.read_bytes(), f"installed:{name}=={dist.version}/{item}", records))
    if name == "pygame":
        source = Path(dist.locate_file("pygame/docs/generated/LGPL.txt"))
        paths.append(put("python/pygame/LGPL.txt", source.read_bytes(), "installed:pygame/docs/generated/LGPL.txt", records))
    if not paths:
        raise RuntimeError(f"No authentic license text found for {name}")
    return paths


def font_names(data: bytes) -> list[dict]:
    result = []
    for index in range(struct.unpack_from(">H", data, 4)[0]):
        tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if tag != b"name":
            continue
        _, count, base = struct.unpack_from(">HHH", data, offset)
        for index in range(count):
            plat, _, _, field, length, pos = struct.unpack_from(">HHHHHH", data, offset + 6 + index * 12)
            if field in (0, 1, 3, 5, 13, 14):
                value = data[offset + base + pos:offset + base + pos + length].decode(
                    "utf-16-be" if plat in (0, 3) else "latin1", errors="replace")
                row = {"name_id": field, "value": value}
                if row not in result:
                    result.append(row)
    return result


def owner(name: str) -> str:
    low = name.lower().replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    if "portaudio" in low:
        return "PortAudio / sounddevice"
    if low.startswith("numpy"):
        return "NumPy (OpenBLAS/LAPACK/GCC runtime notices included)"
    if low.startswith("aubio"):
        return "aubio"
    if base.startswith("_cffi"):
        return "cffi"
    for prefix, label in (("sdl2", "SDL2 family"), ("freetype", "FreeType"),
                          ("libjpeg", "IJG JPEG"), ("libmodplug", "libmodplug"),
                          ("libogg", "Ogg"), ("libopusfile", "Opusfile"),
                          ("libopus", "Opus"), ("libpng", "libpng"),
                          ("libtiff", "libtiff"), ("libwebp", "WebP"),
                          ("portmidi", "PortMidi"), ("zlib1", "zlib (pygame)"),
                          ("libcrypto", "OpenSSL"), ("libssl", "OpenSSL"),
                          ("libffi", "libffi"), ("vcruntime", "Microsoft VC runtime"),
                          ("msvcp", "Microsoft VC runtime")):
        if base.startswith(prefix):
            return label
    if low.startswith("pygame/"):
        return "pygame"
    return "CPython runtime"


def binary_record(name: str, data: bytes) -> dict:
    record = {"path": name.replace("\\", "/"), "owner": owner(name), "bytes": len(data), "sha256": digest(data)}
    # PE metadata is read, never loaded/executed. pefile is optional build tooling.
    try:
        import pefile
        pe = pefile.PE(data=data, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        versions = {}
        for info in getattr(pe, "FileInfo", []):
            for table in info:
                for strings in getattr(table, "StringTable", []):
                    for key, value in strings.entries.items():
                        if key in (b"FileVersion", b"ProductVersion", b"LegalCopyright"):
                            versions[key.decode()] = value.decode("utf-8", errors="replace")
        pe.close()
        if versions:
            record["pe_metadata"] = versions
    except (ImportError, ValueError):
        pass
    return record


def inventory(exe: Path | None) -> dict:
    if exe:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(exe))
        binaries = [binary_record(name, archive.extract(name)) for name in sorted(archive.toc)
                    if name.lower().endswith((".dll", ".pyd"))]
        pyz = archive.open_embedded_archive("PYZ.pyz")
        roots = sorted({name.split(".")[0] for name in pyz.toc})
        return {"basis": "existing-exe-archive", "artifact": exe.name,
                "note": "Nur dieses Archiv; kein Nachweis für einen späteren Build. EXE wird nicht ausgeführt.",
                "python_module_roots": roots, "binaries": binaries}
    binaries = []
    for name in RUNTIME:
        dist = metadata.distribution(name)
        for item in sorted(dist.files or [], key=str):
            if item.suffix.lower() in {".dll", ".pyd"} and ".." not in item.parts:
                source = Path(dist.locate_file(item))
                binaries.append(binary_record(item.as_posix(), source.read_bytes()))
    return {"basis": "installed-runtime-candidates", "artifact": None,
            "note": "Vor Build: installierte Laufzeitkandidaten, kein Beleg, dass jede Datei gebündelt wird. Betriebssystem/Python-DLLs ergänzt erst --exe.",
            "binaries": sorted(binaries, key=lambda row: row["path"])}


def verify_exe(exe: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    lookup = {name.replace("\\", "/"): name for name in archive.toc}
    failed = []
    files = sorted(path for path in OUT.rglob("*") if path.is_file())
    for path in files:
        key = "licenses/" + path.relative_to(OUT).as_posix()
        if key not in lookup or archive.extract(lookup[key]) != path.read_bytes():
            failed.append(key)
    report = {"artifact": exe.name, "sha256": digest(exe.read_bytes()),
              "notice_files": len(files), "mismatches": failed, "passed": not failed}
    print(json.dumps(report, indent=2))
    if failed:
        raise SystemExit(1)
    return report


def validate_pinned_sources() -> None:
    """Refuse a misleading notice set before writing even the first file."""
    expected = {"OpenSSL": "3.5.9", "Expat": "2.8.3", "Python zlib": "1.3.2", "pygame": "2.6.1"}
    actual = {"OpenSSL": ssl.OPENSSL_VERSION.split()[1],
              "Expat": pyexpat.EXPAT_VERSION.removeprefix("expat_"),
              "Python zlib": zlib.ZLIB_RUNTIME_VERSION, "pygame": metadata.version("pygame")}
    mismatches = [f"{name}: installed {actual[name]}, licensed source pinned to {version}"
                  for name, version in expected.items() if actual[name] != version]
    if mismatches:
        raise RuntimeError("Review and update pinned upstream license sources before building: "
                           + "; ".join(mismatches))


def build(fetch: bool, exe: Path | None) -> dict:
    validate_pinned_sources()
    records = []
    components = []
    for name, (license_name, url) in RUNTIME.items():
        dist = metadata.distribution(name)
        components.append({"name": name, "version": dist.version, "license": license_name,
                           "source": url, "license_files": package_notices(name, records),
                           "license_metadata": dist.metadata.get("License-Expression") or dist.metadata.get("License", "")})
    # PyInstaller itself is build tooling. Its embedded bootloader and runtime
    # hooks are distributed, so retain the original exception and their notices.
    try:
        version = metadata.version("pyinstaller")
        components.append({"name": "PyInstaller bootloader / runtime hooks", "version": version,
                           "license": "GPL-2.0-or-later with bootloader exception; runtime hooks Apache-2.0",
                           "source": "https://pyinstaller.org/en/stable/license.html",
                           "license_files": package_notices("pyinstaller", records)})
    except metadata.PackageNotFoundError:
        existing = list((OUT / "python/pyinstaller").rglob("COPYING*"))
        if not existing:
            raise RuntimeError("Install build requirements or preserve checked-in PyInstaller notice")
        for path in existing:
            records.append({"path": path.relative_to(OUT).as_posix(), "sha256": digest(path.read_bytes()),
                            "bytes": path.stat().st_size, "source": "checked-in build-tool notice; PyInstaller not installed"})
    for relative, url in UPSTREAM.items():
        path = OUT / "upstream" / relative
        if fetch:
            request = urllib.request.Request(url, headers={"User-Agent": "PickHero-license-inventory/1"})
            with urllib.request.urlopen(request, timeout=25) as response:
                data = response.read(1_000_001)
                if len(data) > 1_000_000 or not data or b"<!doctype html" in data[:100].lower():
                    raise ValueError(f"Unexpected license response: {url}")
        else:
            data = path.read_bytes()
        put("upstream/" + relative, data, url, records)
    python_license = next((path for path in (
        Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE",
        Path(sys.base_prefix) / "share/licenses/python/LICENSE") if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("Build interpreter LICENSE not found; do not invent a replacement")
    py_license = put("python-runtime/LICENSE.txt", python_license.read_bytes(),
                     f"build-interpreter:CPython-{platform.python_version()}/LICENSE", records)
    components.append({"name": "CPython and bundled Windows runtime", "version": platform.python_version(),
                       "license": "PSF and historical/embedded notices; Microsoft redistributable conditions",
                       "source": f"https://github.com/python/cpython/tree/v{platform.python_version()}",
                       "license_files": [py_license]})
    port_dist = metadata.distribution("sounddevice")
    for tail in ("README.md", ".github/workflows/build-libs.yml"):
        source = Path(port_dist.locate_file("_sounddevice_data/portaudio-binaries/" + tail))
        if source.is_file():
            put("portaudio/" + tail, source.read_bytes(), "installed:sounddevice/portaudio-binaries/" + tail, records)
    fonts = []
    for family, url in FONT_SOURCES.items():
        source = ROOT / f"pickhero/assets/fonts/OFL-{family}.txt"
        notice = put("fonts/" + source.name, source.read_bytes(), source.relative_to(ROOT).as_posix(), records)
        fontfiles = sorted((ROOT / "pickhero/assets/fonts").glob(f"{family}*.ttf"))
        fonts.append({"family": family, "license": "OFL-1.1", "source": url, "license_file": notice,
                      "files": [{"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path.read_bytes())} for path in fontfiles]})
    fallback = Path(metadata.distribution("pygame").locate_file("pygame/freesansbold.ttf"))
    fallback_names = font_names(fallback.read_bytes())
    put("fonts/FreeSans-embedded-metadata.json", (json.dumps(fallback_names, indent=2, ensure_ascii=False) + "\n").encode(),
        "installed:pygame/freesansbold.ttf:name-table", records)
    fonts.append({"family": "pygame FreeSans (installed source environment; replaced in EXE)", "license": "GNU GPL per embedded metadata; exact exception/version not established",
                  "source": "https://www.gnu.org/software/freefont/license.html",
                  "license_file": "upstream/freefont/GPL-2.0.txt",
                  "files": [{"path": "pygame/freesansbold.ttf", "sha256": digest(fallback.read_bytes())}]})
    packaged_fallback = ROOT / "pickhero/assets/fonts/Geist-Regular.ttf"
    fonts.append({"family": "Geist (EXE fallback at pygame/freesansbold.ttf)", "license": "OFL-1.1",
                  "source": FONT_SOURCES["Geist"], "license_file": "fonts/OFL-Geist.txt",
                  "files": [{"path": "pygame/freesansbold.ttf", "sha256": digest(packaged_fallback.read_bytes())}]})
    aubio_build = ROOT / "wheels/aubio-build.json"
    aubio_provenance = False
    if aubio_build.is_file():
        provenance = json.loads(aubio_build.read_text(encoding="utf-8"))
        source = ROOT / "wheels/sources/aubio-0.4.9.tar.gz"
        wheel = ROOT / "wheels" / provenance["wheel"]
        recipe = ROOT / "scripts/build-aubio-wheel.py"
        build_log = ROOT / "wheels/aubio-build.log"
        installed = metadata.distribution("aubio")
        if (digest(source.read_bytes()) != provenance["source_sha256"]
                or digest(wheel.read_bytes()) != provenance["wheel_sha256"]
                or digest(recipe.read_bytes()) != provenance["recipe_sha256"]
                or digest(build_log.read_bytes()) != provenance["build_log_sha256"]
                or any(digest(Path(installed.locate_file(name)).read_bytes()) != checksum
                       for name, checksum in provenance["native_sha256"].items())):
            raise ValueError("aubio build provenance does not match the installed runtime")
        put("aubio/BUILD.json", aubio_build.read_bytes(), "local:verified aubio build", records)
        put("aubio/build-aubio-wheel.py", recipe.read_bytes(), "local:verified build recipe", records)
        put("aubio/BUILD.log", build_log.read_bytes(), "local:verified compiler commands", records)
        put("aubio/aubio-0.4.9.tar.gz", source.read_bytes(), provenance["source_url"], records)
        aubio_provenance = True
    origin = ("PickHero Herkunft / provenance\n\n"
              "Upstream: https://github.com/Artemarius/PickHero\n"
              "Fork: https://github.com/minimalillusions/PickHero\n"
              "Erster lokaler Git-Commit: 7af9c2526bdb3287500bf2403c54d30aa5505ffa\n"
              "Autor laut Git-Historie: Artem Shamsuarov\n"
              "Projektlizenz: MIT. LICENSE im Repository-Root nennt beide Urheber,\n"
              "Artem Shamsuarov und minimalillusions.\n")
    put("project/ORIGIN.txt", origin.encode(), "repository:README.md and read-only git history/remotes", records)
    snapshot = inventory(exe)
    issues = [
        "LGPL-Bibliotheken (pygame, PyGuitarPro): Quellbereitstellung, Austauschbarkeit und passende Build-Unterlagen sind vor externer Veröffentlichung zu vervollständigen; Notices allein sind keine Freigabe.",
        "Einige native pygame-Unterbibliotheken haben keine verifizierte genaue Build-Version. Die authentischen Abhängigkeitsnotices stammen aus pygame 2.6.1; statisch eingebettete Komponenten sind nicht vollständig zugeordnet.",
        "libffi-8.dll: genaue Build-Version unbekannt; mitgelieferter MIT-Text von 3.4.4 ist ausdrücklich nur eine Upstream-Referenz.",
        "OpenSSL-/Expat-/Python-zlib-Texte sind auf 3.5.9 / 2.8.3 / 1.3.2 festgelegt. Bei anderem Build-Interpreter müssen diese Quelltexte/Zuordnungen überprüft werden.",
    ]
    if not aubio_provenance:
        issues.append("Für das aubio-Wheel fehlen an die tatsächlich installierte Binärdatei gebundene Quellen und Build-Nachweise.")
    if any("asio" in row["path"].lower() for row in snapshot["binaries"]):
        issues.append("ASIO: Exakte SDK-Version und zugehörige Bedingungen sind für die vorhandene x64-Binärdatei weiter offen. Die x64-Unterstützung bleibt erhalten. Nicht benötigte 32-bit- und ARM64-DLLs werden durch die Build-Regel ausgeschlossen; der Archivprüfer muss das bestätigen.")
    manifest = {"schema": 1, "scope": "runtime notices; no application relicensing or publication clearance",
                "inventory": snapshot, "components": components, "fonts": fonts,
                "interpreter_libraries": {"openssl": ssl.OPENSSL_VERSION, "zlib": zlib.ZLIB_RUNTIME_VERSION, "expat": pyexpat.EXPAT_VERSION},
                "notice_files": sorted(records, key=lambda row: row["path"]), "open_items": issues,
                "bundle_policy": {"fallback_font": "Unmodified Geist-Regular.ttf replaces pygame/freesansbold.ttf in the EXE; OFL notice included.",
                                  "portaudio": "Windows x64 standard and ASIO backends retained; x86 and ARM64 excluded.",
                                  "aubio_source_and_recipe_verified": aubio_provenance},
                "reachability": {
                    "pygame_media": "Bundled fonts / trusted assets; no user GP/MIDI import path through SDL_image/SDL_mixer found in current app.",
                    "imports": "User GP ZIP imports use Python zipfile and Python zlib; MIDI is parsed by application code. Does not use pygame zlib1.dll.",
                    "limits": "Old DLL versions alone do not prove an exploitable application vulnerability. No hardware or active exploit test performed."}}
    write_json(OUT / "manifest.json", manifest)
    render(manifest, OUT / "THIRD_PARTY_NOTICES.html", "")
    render(manifest, ROOT / "docs/third-party-notices.html", "../licenses/")
    return manifest


def render(manifest: dict, destination: Path, prefix: str) -> None:
    esc = html.escape
    links = lambda paths: " · ".join(f'<a href="{esc(prefix + path, quote=True)}">{esc(Path(path).name)}</a>' for path in paths)
    rows = "".join(f'<tr><td>{esc(c["name"])}<br><small>{esc(c["version"])}</small></td><td>{esc(c["license"])}</td><td>{links(c["license_files"])}<br><a href="{esc(c["source"], quote=True)}">Originalquelle</a></td></tr>' for c in manifest["components"])
    fontrows = "".join(f'<tr><td>{esc(c["family"])}</td><td>{esc(c["license"])}</td><td>{links([c["license_file"]])}<br><a href="{esc(c["source"], quote=True)}">Originalquelle</a></td></tr>' for c in manifest["fonts"])
    issues = "".join(f"<li>{esc(item)}</li>" for item in manifest["open_items"])
    native = "".join(f'<tr><td><code>{esc(row["path"])}</code></td><td>{esc(row["owner"])}</td><td>{esc(row.get("pe_metadata", {}).get("FileVersion", "nicht verifiziert"))}</td></tr>' for row in manifest["inventory"]["binaries"])
    alltexts = "".join(f'<li>{links([record["path"]])} <small>{esc(record["source"])}</small></li>' for record in manifest["notice_files"])
    page = f'''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PickHero · Drittanbieter und Laufzeitbestand</title><style>
:root{{color-scheme:light;--ink:#142d3a;--muted:#536571;--line:#d7e2e7}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f7f6;color:var(--ink);font:17px/1.6 system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:40px 24px 72px}}h1{{font-size:clamp(30px,5vw,44px);line-height:1.15}}h2{{margin-top:36px}}a{{color:#075e85;text-underline-offset:3px}}small,.meta{{font-size:13px;color:var(--muted)}}article{{background:white;border:1px solid var(--line);border-left:4px solid #096b62;padding:20px;margin:20px 0;border-radius:8px}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#e4eeec}}code{{font-size:.8em;overflow-wrap:anywhere}}p,li{{max-width:60ch}}li{{margin:10px 0}}.scroll{{overflow:auto}}small{{overflow-wrap:anywhere}}</style>
<main><p class="meta">Lokale Bestandsaufnahme · automatisch aus dem Build-Interpreter erzeugt</p>
<h1>Drittanbieter, Schriften und native Bibliotheken</h1>
<p>Authentische Lizenztexte und Herkunftsnachweise für PickHeros Laufzeit. Die ursprüngliche MIT-Angabe bleibt erhalten. Diese Sammlung ändert keine Projektlizenz und bestätigt keine abschließende Freigabe zur Veröffentlichung.</p>
<article><strong>Inventarbasis: {esc(manifest['inventory']['basis'])}</strong><p>{esc(manifest['inventory']['note'])}</p><p>{links(['manifest.json', 'project/ORIGIN.txt'])}. SHA-256-Prüfsummen sämtlicher erfasster nativer Dateien, Schriften und Texte stehen im Manifest. Entwicklertools werden nicht als App-Laufzeit ausgewiesen; PyInstallers tatsächlich eingebetteter Bootloader und Runtime-Hooks sind separat erfasst.</p></article>
<h2>Python-Komponenten</h2><div class="scroll"><table><tr><th>Komponente</th><th>Lizenzangabe</th><th>Authentische Texte</th></tr>{rows}</table></div>
<p>cffi ist durch den nativen <code>_cffi_backend</code> relevant. pycparser, pytest, Ruff, pip und andere reine Entwicklungs-/Installationswerkzeuge gehören nicht zu diesem Runtime-Inventar. NumPys Originaltext enthält auch OpenBLAS, LAPACK und GCC-Runtime-Ausnahmen.</p>
<h2>Schriften</h2><div class="scroll"><table><tr><th>Familie</th><th>Lizenzangabe</th><th>Text / Herkunft</th></tr>{fontrows}</table></div>
<h2>Native Dateien</h2><p>Die Windows-DLLs werden als Daten gelesen, nicht geladen. Fehlende Versionsmetadaten werden ausdrücklich nicht erraten. PortAudio stammt laut mitgeliefertem Build-Workflow aus v19.7.0. Die FreType-Notices und der Hinweis gelten: Portions of this software are copyright © 2021 The FreeType Project (www.freetype.org). All rights reserved.</p>
<p>Interpreter: {esc(json.dumps(manifest['interpreter_libraries'], ensure_ascii=False))}.</p>
<details><summary>{len(manifest['inventory']['binaries'])} native Dateien und Zuordnungen</summary><div class="scroll"><table><tr><th>Datei</th><th>Herkunft</th><th>PE-Dateiversion</th></tr>{native}</table></div></details>
<h2>Erreichbarkeit und Aktualisierung</h2><p>Die Bild-, Audio-Datei- und Schriftdecoder aus pygame werden im aktuellen Anwendungscode nicht für GP-/MIDI-Nutzerimporte verwendet. Fonts kommen aus gebündelten Assets beziehungsweise lokalen Systemschriften. GP-ZIP-Importe laufen über Python <code>zipfile</code> und dessen zlib. Eine ältere zusätzliche DLL-Version allein belegt deshalb keine ausnutzbare Schwachstelle in diesem Importweg.</p>
<p>Die offiziellen <a href="https://github.com/pygame-community/pygame-ce/releases/tag/2.5.8">pygame-ce-2.5.8-Releases</a> und das <a href="https://pypi.org/project/pygame-ce/2.5.8/#files">CPython-3.12-Windows-x64-Wheel</a> bieten eine getrennt zu testende Aktualisierungsoption. Es wurde nur das ZIP/PE-Inventar gelesen, nichts installiert oder ausgeführt: SDL 2.32.10, image 2.8.12, mixer 2.8.2, ttf 2.24.0; FreeType bleibt laut DLL-Metadaten 2.11.1. Keine gemischten DLL-Sätze verwenden. Diese Notiz behauptet keine vollständige Schwachstellenfreiheit.</p>
<h2>Offene Herkunfts- und Veröffentlichungspunkte</h2><ul>{issues}</ul>
<h2>Alle Originaltexte</h2><p>Texte werden unverändert aus installierten Distributionsdateien oder angegebenen offiziellen Quellen kopiert. pygame-Notices umfassen auch mögliche statische Abhängigkeiten; ihre Beilage behauptet nicht, dass jede davon als eigene DLL im Archiv liegt.</p><details><summary>{len(manifest['notice_files'])} Texte / Nachweise öffnen</summary><ul>{alltexts}</ul></details>
<h2>Reproduzieren und bündeln</h2><p><code>python scripts/build-notices.py</code> funktioniert ohne EXE, offline mit eingecheckten Upstream-Texten. <code>--fetch</code> aktualisiert die festgelegten Quellen. Optional <code>--exe dist/PickHero.exe</code> erfasst genau dieses bestehende Archiv. Der vollständige Ordner <code>licenses/</code> gehört in das App-Bundle; Einstieg: <code>licenses/THIRD_PARTY_NOTICES.html</code>. Nach dem Build prüft <code>python scripts/build-notices.py --verify-exe dist/PickHero.exe</code> alle Notice-Dateien bytegenau ohne App-Start.</p>
</main></html>'''
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--exe", type=Path, help="Optional existing EXE inventory; never executed")
    parser.add_argument("--verify-exe", type=Path, help="Only verify embedded notice bytes; do not regenerate")
    args = parser.parse_args()
    if args.verify_exe:
        verify_exe(args.verify_exe)
        return
    if args.exe and not args.exe.is_file():
        parser.error("--exe must name an existing archive; omit it for a clean checkout")
    manifest = build(args.fetch, args.exe)
    print(json.dumps({"components": len(manifest["components"]), "notice_files": len(manifest["notice_files"]),
                      "native_files": len(manifest["inventory"]["binaries"]), "basis": manifest["inventory"]["basis"]}))


if __name__ == "__main__":
    main()
