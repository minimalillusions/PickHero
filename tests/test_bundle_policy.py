"""The Windows bundle must retain working backends and licensed fallback data."""
from pathlib import Path
import pytest
from build_support.bundle_policy import apply_policy, supported_portaudio


@pytest.mark.parametrize('name', ['libportaudio32bit.dll','libportaudio32bit-asio.dll',
                                 'libportaudioarm64.dll','libportaudioarm64-asio.dll'])
def test_other_architectures_are_not_shipped(name):
    assert not supported_portaudio('_sounddevice_data/portaudio-binaries/'+name)


@pytest.mark.parametrize('name', ['libportaudio64bit.dll','libportaudio64bit-asio.dll'])
def test_existing_x64_audio_backends_remain_available(name):
    assert supported_portaudio('_sounddevice_data\\portaudio-binaries\\'+name)


def test_fallback_substitution_keeps_original_font_bytes_and_other_data(tmp_path):
    font=tmp_path/'pickhero/assets/fonts/Geist-Regular.ttf'
    font.parent.mkdir(parents=True);font.write_bytes(b'original licensed font')
    original=[('pygame/freesansbold.ttf','old.ttf','DATA'),('licenses/example','notice','DATA')]
    binaries=[('_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll','asio','BINARY'),
              ('_sounddevice_data/portaudio-binaries/libportaudioarm64.dll','arm','BINARY'),
              ('another.dll','other','BINARY')]
    selected,data=apply_policy(binaries,original,tmp_path)
    assert selected==[binaries[0],binaries[2]]
    assert Path(data[0][1]).read_bytes()==b'original licensed font'
    assert data[1]==original[1] and original[0][1]=='old.ttf'


def test_missing_or_duplicate_fallback_fails_the_build(tmp_path):
    with pytest.raises(FileNotFoundError):apply_policy([],[],tmp_path)
    font=tmp_path/'pickhero/assets/fonts/Geist-Regular.ttf'
    font.parent.mkdir(parents=True);font.write_bytes(b'font')
    for data in ([],[('pygame/freesansbold.ttf','old','DATA')]*2):
        with pytest.raises(ValueError):apply_policy([],data,tmp_path)
