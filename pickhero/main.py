"""PickHero application entry point."""

import sys
from pathlib import Path

from pickhero.config import Config


def _resolve_songs_dir(config: Config) -> None:
    """Fix songs_dir for frozen exe: look next to the executable."""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path.cwd()

    songs_path = Path(config.songs_dir)
    if not songs_path.is_absolute():
        config.songs_dir = str(base_dir / songs_path)


def _adopt_system_language(config: Config) -> None:
    """On the very first start, speak the language the computer speaks.

    Only then. Once a settings file exists its language is somebody's
    choice -- or the German an older version wrote before there was one --
    and is not second-guessed. A language without a catalogue here gets
    English, which more people read than German. The choice is saved at
    once, so it is made exactly once and from then on belongs to the player.
    """
    from pickhero.i18n import available_languages, system_language
    codes = [code for code, _ in available_languages()]
    wanted = system_language()
    if wanted in codes:
        config.language = wanted
    elif "en" in codes:
        config.language = "en"
    config.save()


def _load_config() -> Config:
    from pickhero import config as config_module
    first_start = not config_module.CONFIG_FILE.exists()
    config = Config.load()
    if first_start:
        _adopt_system_language(config)
    return config


def main():
    if sys.argv[1:2] == ["--import-worker"]:
        from pickhero.import_worker import run
        raise SystemExit(run(sys.argv[2]))
    if sys.argv[1:2] == ["--self-test"]:
        from pickhero.selftest import run
        report = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("pickhero-selftest.json")
        raise SystemExit(0 if run(report) else 1)
    if "--console" in sys.argv:
        # Phase 1 console demo for audio testing
        from pickhero.audio.input import run_console_demo
        try:
            run_console_demo()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
    else:
        config = _load_config()
        _resolve_songs_dir(config)
        from pickhero.ui.app import App
        App(config=config, background=True).run()


if __name__ == "__main__":
    main()
