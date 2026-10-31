"""Explicit file choices for the supported Windows x64 executable."""
from pathlib import Path, PurePosixPath


def supported_portaudio(name: str) -> bool:
    """Keep both x64 backends, including the existing optional ASIO backend."""
    path = PurePosixPath(name.replace('\\', '/').lower())
    if path.parent.as_posix() != '_sounddevice_data/portaudio-binaries' or path.suffix != '.dll':
        return True
    return path.name in {'libportaudio64bit.dll', 'libportaudio64bit-asio.dll'}


def apply_policy(binaries, datas, root):
    fallback = Path(root)/'pickhero/assets/fonts/Geist-Regular.ttf'
    if not fallback.is_file():
        raise FileNotFoundError(f'Bundled fallback font is missing: {fallback}')
    selected = [entry for entry in binaries if supported_portaudio(entry[0])]
    selected_data = []
    replaced = 0
    for name, source, kind in datas:
        if not supported_portaudio(name):
            continue
        if name.replace('\\', '/').lower() == 'pygame/freesansbold.ttf':
            source = str(fallback.resolve())
            replaced += 1
        selected_data.append((name, source, kind))
    if replaced != 1:
        raise ValueError('Expected exactly one pygame fallback font in the build')
    return selected, selected_data
