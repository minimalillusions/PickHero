# aubio for Windows x64 / CPython 3.12

The bundled wheel was built on 2026-10-15 from the unchanged official aubio
0.4.9 source archive. Users do not need a C compiler to install it:

    pip install --find-links wheels -c constraints.txt -r requirements.txt

Wheel SHA-256:
`624b508cfb51c34abf682ea3eb64e8a12cdbb03ff214b688683ff82328052dd5`.
Both `scripts/setup.ps1` and CI check this value before installation. The
supported build uses CPython 3.12.14, Windows x64 and NumPy 2.5.3; this wheel
does not support a different Python minor version or architecture.

## Source and build evidence

- [Official source archive](sources/aubio-0.4.9.tar.gz), SHA-256
  `df1244f6c4cf5bea382c8c2d35aa43bc31f4cf631fe325ae3992c219546a4202`.
- [Build report](aubio-build.json): original source URL, hashes of source
  files, recipe, wheel and native module; exact Python/dependency versions.
- [Compiler log](aubio-build.log): MSVC 14.44.35207, compiler 19.44.35228,
  Windows SDK 10.0.26100.0, all compilation/link commands.
- [Build recipe](../scripts/build-aubio-wheel.py): validates the archive and
  local build dependencies, extracts into a fresh temporary directory and
  builds offline. It does not install or replace the resulting wheel.

With the recorded x64 MSVC/SDK toolchain installed, prepare a separate
CPython 3.12.14 environment containing NumPy 2.5.3, pip 26.2.1 and
setuptools 84.0.0, then run:

    python scripts/build-aubio-wheel.py --output scratchpad/aubio-new-build

The output directory must not exist. Compiler timestamps and temporary
paths can change binary hashes; this is a documented source build, not a
claim of bit-for-bit reproducibility. Before adopting another build, rerun
the audio regressions and tests, update the wheel/report together, then
update both installation checksum pins. Keep the source archive unchanged.

`scripts/build-notices.py` checks the installed native module against this
report and bundles the original source archive, report and recipe with the
aubio license. `scripts/check-bundle.py` verifies the actual EXE's native
module and those embedded sources. aubio's GPL terms still apply; these
build records do not settle the complete application's release licensing.

## Role in detection

Bass ukulele primarily uses the NumPy harmonic detector. aubio supplies
onset evidence and corroborating YINfast pitch observations for narrowly
defined ambiguous cases. Guitar and bass guitar also use aubio pitch
detection. The 2026-10-15 rebuild keeps aubio at 0.4.9 and changes no DSP
settings or instrument profiles.
