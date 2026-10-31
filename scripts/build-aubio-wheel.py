"""Build the pinned, unmodified aubio source with the active Windows toolchain.

No install, download or publication occurs. The output directory must be new.
Use the project's CPython 3.12 x64 environment and the documented MSVC tools.
PE timestamps and archive metadata may differ between builds; the report pins
the actual artifact, source, build tools and build command.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = 'df1244f6c4cf5bea382c8c2d35aa43bc31f4cf631fe325ae3992c219546a4202'
SOURCE_URL = 'https://files.pythonhosted.org/packages/cd/80/302d89240603e5347c7f8026c8b02c59f8dfaec66c91a743d82de7c86006/aubio-0.4.9.tar.gz'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_source(source, destination):
    if source.stat().st_size > 5*1024*1024 or sha(source) != SOURCE_SHA256:
        raise ValueError('The aubio source archive does not match the pinned release')
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        if (len(members) > 3000 or sum(m.size for m in members) > 20*1024*1024
                or any(not (m.isfile() or m.isdir()) for m in members)):
            raise ValueError('Unexpected source archive contents')
        archive.extractall(destination, members=members, filter='data')
    return destination/'aubio-0.4.9'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'wheels/sources/aubio-0.4.9.tar.gz')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if platform.system() != 'Windows' or sys.version_info[:2] != (3,12) or struct.calcsize('P') != 8:
        parser.error('This recipe supports Windows x64 and CPython 3.12')
    versions={name:metadata.version(name) for name in ('numpy','pip','setuptools')}
    if versions != {'numpy':'2.5.3','pip':'26.2.1','setuptools':'84.0.0'}:
        parser.error(f'Use the documented build dependencies; found {versions}')
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='pickhero-aubio-build-') as temporary:
        source=extract_source(args.source,Path(temporary))
        inputs={p.relative_to(source).as_posix():sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
        command=[sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation',
                 '--no-index','--no-cache-dir','--verbose','--wheel-dir',str(output),str(source)]
        with (output/'build.log').open('w',encoding='utf-8') as log:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=240,check=False)
        if result.returncode:
            raise RuntimeError(f'aubio build failed; inspect {output / "build.log"}')
        wheel, = output.glob('*.whl')
        assert wheel.name == 'aubio-0.4.9-cp312-cp312-win_amd64.whl'
        with zipfile.ZipFile(wheel) as archive:
            native={name:hashlib.sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist() if name.endswith('.pyd')}
            assert len(native) == 1 and any(name.endswith('/COPYING') for name in archive.namelist())
        report=dict(schema=1,source_url=SOURCE_URL,source_sha256=SOURCE_SHA256,
                    source_files_sha256=inputs,source_modifications=[],
                    python=platform.python_version(),platform=platform.platform(),dependencies=versions,
                    command=['python',*command[1:-1],'<unmodified extracted aubio-0.4.9>'],
                    wheel=wheel.name,wheel_sha256=sha(wheel),native_sha256=native,
                    build_log_sha256=sha(output/'build.log'),recipe_sha256=sha(Path(__file__)),
                    note='Exact source and recipe; PE timestamps need not reproduce byte-for-byte.')
        (output/'build.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({k:report[k] for k in ('wheel','wheel_sha256','native_sha256')}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
