"""Build the self-contained listening room and a composition-only Claude brief.

Uses no PickHero runtime state, recordings, audio devices, or network services.
The browser renders both authors through the same deterministic synthesizer.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/music/vergleich"
SOURCE = ROOT / "scripts/composition-listening"


def task(key, style, name, bpm, swing, chords, brief):
    return dict(id=key, style=style, name=name, bpm=bpm, swing=swing,
                bars=12, chords=chords.split(), brief=brief)


TASKS = [
    task("ruhig-1", "Ruhig", "Platz zum Atmen", 80, 0,
         "Em7 Em7 Cmaj7 Cmaj7 Am7 Am7 Em7 Em7 Cmaj7 B7 Em7 Em7",
         "Ein ruhiges Zweitaktmotiv mit hörbaren Pausen. Höchstens vier Anschläge pro Takt."),
    task("ruhig-2", "Ruhig", "Eine kleine Antwort", 76, 0,
         "Am7 Am7 Fmaj7 Fmaj7 Cmaj7 G6 Am7 Am7 Fmaj7 Em7 Am7 Am7",
         "Eine kleine gesungene Frage und Antwort. Höchstens vier Anschläge pro Takt."),
    task("blues-1", "Blues", "Der kleine Shuffle", 84, 2 / 3,
         "E7 E7 E7 E7 A7 A7 E7 E7 B7 A7 E7 E7",
         "Ein entspannter Zwölftaktblues mit geshuffelten Achteln. Höchstens sechs Anschläge pro Takt."),
    task("blues-2", "Blues", "Kurz und frech", 88, 0,
         "A7 A7 A7 A7 D7 D7 A7 A7 E7 D7 A7 A7",
         "Ein gerader Blues mit kurzen Antworten und Luft zwischen den Phrasen. Höchstens sechs Anschläge pro Takt."),
    task("rock-1", "Rock", "Ein Riff, eine Lücke", 96, 0,
         "E5 E5 G5 A5 E5 E5 G5 A5 B5 A5 E5 E5",
         "Ein prägnantes Riff mit einer wiedererkennbaren rhythmischen Lücke. Höchstens sechs Anschläge pro Takt."),
    task("rock-2", "Rock", "Weite Kurven", 92, 0,
         "A5 A5 C5 G5 A5 A5 D5 E5 C5 G5 A5 A5",
         "Ein melodischer Rockgroove mit langen Zieltönen und kurzen Anläufen. Höchstens sechs Anschläge pro Takt."),
]


# Each bar is composed explicitly. Tokens are string/fret, onset, length in
# quarter-note beats, optionally velocity (0..1). Spaces between notes are rests.
# A repeated motif is intentional; the third phrase changes or resolves it.
MUSIC = [
    ("ruhig-1", "Fenster auf", [
        "E0,0,1.4 E3,2,0.65 A2,3,0.65",
        "A0,0,0.65 E3,1,0.65 E0,2,1.4",
        "A3,0,1.4 E3,2,0.65 A2,3,0.65",
        "A3,0,1.4 A2,2,0.65 E3,3,0.65",
        "A0,0,1.4 A3,2,0.65 E3,3,0.65",
        "A0,0,0.65 E3,1,0.65 E0,2,1.4",
        "E0,0,1.4 E3,2,0.65 A2,3,0.65",
        "A0,0,0.65 E3,1,0.65 E0,2,1.4",
        "A3,0,1.4 A2,2,0.65 A0,3,0.65",
        "A2,0,1.4 E2,2,0.65 A2,3,0.65",
        "E0,0,1.4 E3,2,0.65 E2,3,0.65",
        "E0,0,3.2",
    ]),
    ("ruhig-2", "Papierboot", [
        "A0,0,1.3 A3,2,0.65 D2,3,0.65",
        "D0,0,1.3 A3,2,0.65 A0,3,0.65",
        "E1,0,1.3 A0,2,0.65 A3,3,0.65",
        "D0,0,0.65 A3,1,0.65 A0,2,1.3",
        "A3,0,1.3 D2,2,0.65 D0,3,0.65",
        "E3,0,1.3 A2,2,0.65 D0,3,0.65",
        "A0,0,1.3 A3,2,0.65 D2,3,0.65",
        "D0,0,1.3 A3,2,0.65 A0,3,0.65",
        "E1,0,1.3 A0,2,0.65 A3,3,0.65",
        "E0,0,1.3 A2,2,0.65 E3,3,0.65",
        "A0,0,1.3 A3,2,0.65 A2,3,0.65",
        "A0,0,3.2",
    ]),
    ("blues-1", "Schlendrian", [
        "E0,0,0.7 E0,1,0.36 E3,1.5,0.36 A0,2,0.7 E3,3,0.7",
        "E0,0,1.35 A2,2,0.7 E3,3,0.36 E0,3.5,0.36",
        "E0,0,0.7 E0,1,0.36 E3,1.5,0.36 A0,2,0.7 E3,3,0.7",
        "A2,0,0.7 A0,1,0.7 E3,2,0.7 E0,3,0.7",
        "A0,0,0.7 A0,1,0.36 A3,1.5,0.36 D0,2,0.7 A3,3,0.7",
        "A0,0,1.35 D2,2,0.7 A3,3,0.36 A0,3.5,0.36",
        "E0,0,0.7 E0,1,0.36 E3,1.5,0.36 A0,2,0.7 E3,3,0.7",
        "E0,0,1.35 E3,2,0.7 A1,3,0.36 A2,3.5,0.36",
        "A2,0,0.7 E2,1,0.7 A0,2,0.7 E2,3,0.7",
        "A0,0,0.7 A3,1,0.7 D0,2,0.7 A3,3,0.7",
        "E0,0,0.7 E3,1,0.36 A0,1.5,0.36 A1,2,0.36 A2,2.5,0.36 E3,3,0.7",
        "E0,0,3.2",
    ]),
    ("blues-2", "Noch einen Kaffee", [
        "A0,0,0.7 A3,1.5,0.35 D0,2,0.6 A0,3,0.4",
        "A0,0,1.1 D2,2,0.4 A3,2.5,0.35 A0,3,0.6",
        "A0,0,0.7 A3,1.5,0.35 D0,2,0.6 A0,3,0.4",
        "D2,0,0.6 D0,1,0.6 A3,2,0.6 A0,3,0.4",
        "D0,0,0.7 D3,1.5,0.35 D2,2,0.6 D0,3,0.4",
        "D0,0,1.1 A0,2,0.4 A3,2.5,0.35 D0,3,0.6",
        "A0,0,0.7 A3,1.5,0.35 D0,2,0.6 A0,3,0.4",
        "A0,0,1.1 A3,2,0.4 D0,2.5,0.35 D2,3,0.6",
        "E0,0,0.7 E3,1.5,0.35 A2,2,0.6 E0,3,0.4",
        "D0,0,0.7 D3,1.5,0.35 D2,2,0.6 A3,3,0.4",
        "A0,0,0.7 A3,1.5,0.35 D0,2,0.4 A3,2.5,0.35 A0,3,0.6",
        "A0,0,3.2",
    ]),
    ("rock-1", "Kante", [
        "E0,0,0.36 E0,0.5,0.36 E3,1.5,0.36 E0,2,0.65 A0,3,0.6",
        "E0,0,0.36 E0,0.5,0.36 E3,1.5,0.36 A2,2,0.65 E3,3,0.6",
        "E3,0,0.36 E3,0.5,0.36 A2,1.5,0.36 E3,2,0.65 D0,3,0.6",
        "A0,0,0.36 A0,0.5,0.36 A3,1.5,0.36 A0,2,0.65 E3,3,0.6",
        "E0,0,0.36 E0,0.5,0.36 E3,1.5,0.36 E0,2,0.65 A0,3,0.6",
        "E0,0,0.36 E0,0.5,0.36 E3,1.5,0.36 A2,2,0.65 E3,3,0.6",
        "E3,0,0.36 E3,0.5,0.36 A2,1.5,0.36 E3,2,0.65 D0,3,0.6",
        "A0,0,0.36 A0,0.5,0.36 A3,1.5,0.36 A0,2,0.65 A1,3,0.35 A2,3.5,0.35",
        "A2,0,0.36 A2,0.5,0.36 E2,1.5,0.36 A2,2,0.65 A0,3,0.6",
        "A0,0,0.36 A0,0.5,0.36 A3,1.5,0.36 A0,2,0.65 E3,3,0.6",
        "E0,0,0.36 E0,0.5,0.36 E3,1.5,0.36 A0,2,0.6 E3,3,0.6",
        "E0,0,3.2",
    ]),
    ("rock-2", "Richtung Meer", [
        "A0,0,1.4 A3,2,0.4 D0,2.5,0.4 D2,3,0.6",
        "D2,0,0.7 D0,1,0.4 A3,1.5,0.4 A0,2,1.4",
        "A3,0,1.4 D2,2,0.4 D0,2.5,0.4 A3,3,0.6",
        "E3,0,1.4 A2,2,0.4 A0,2.5,0.4 E3,3,0.6",
        "A0,0,1.4 A3,2,0.4 D0,2.5,0.4 D2,3,0.6",
        "D2,0,0.7 D0,1,0.4 A3,1.5,0.4 A0,2,1.4",
        "D0,0,1.4 D3,2,0.4 D2,2.5,0.4 D0,3,0.6",
        "E0,0,1.4 E3,2,0.4 A0,2.5,0.4 A2,3,0.6",
        "A3,0,1.4 D2,2,0.4 D0,2.5,0.4 A3,3,0.6",
        "E3,0,1.4 A2,2,0.4 A0,2.5,0.4 E3,3,0.6",
        "A0,0,0.7 A3,1,0.4 D0,1.5,0.4 D2,2,0.6 A3,3,0.6",
        "A0,0,3.2",
    ]),
]


def scores():
    result = []
    for task_id, title, bars in MUSIC:
        assert len(bars) == 12
        notes = []
        for bar, pattern in enumerate(bars):
            for token in pattern.split():
                position, beat, duration, *velocity = token.split(",")
                beat = float(beat)
                # Accents follow the motif; offbeats remain a little lighter.
                level = float(velocity[0]) if velocity else (.88 if beat == 0 else .74 if beat % 1 else .81)
                notes.append(dict(beat=bar * 4 + beat, duration=float(duration),
                                  string=position[0], fret=int(position[1:]), velocity=level))
        result.append(dict(task=task_id, title=title, notes=notes))
    return dict(schema="pickhero-composition-1", author="Codex", pieces=result)


PROMPT = """Komponiere sechs kurze, eigenständige Basslinien für eine Bass-Ukulele.

Ziel: Musik, die ein Anfänger gern spielen und jemandem vorspielen möchte.
Keine Tonleiterübungen, keine Diagnosesequenzen und kein bloßes Durchzählen
der Akkordgrundtöne. Einprägsame Motive, Pausen, Frage und Antwort sowie
gezielte Variationen sind wichtiger als viele Noten.

Ich hänge die Datei claude-vorlage.html an. Sie enthält die sechs Aufgaben,
eine fertige Hörseite und eine festgelegte Begleitung mit Klangerzeugung.
Öffne sie als Quelltext. Fülle ausschließlich das JSON im Script-Element
mit id="score-data" mit deinen sechs Kompositionen. Lasse Aufgaben,
Synthesizer, Begleitung, Lautstärke-Normalisierung und Oberfläche unverändert.
Falls du keine Dateien bearbeiten kannst, liefere wenigstens das vollständige
JSON als herunterladbare Datei. Keine erfundenen WAV-Dateien oder Hörtests.

Gemeinsame Vorgaben
- Bass-Ukulele E1–A1–D2–G2 (MIDI 28, 33, 38, 43).
- Verwende nur die E-, A- und D-Saite, Bünde 0 bis 3.
- Einstimmig: keine überlappenden Bassnoten, keine Akkorde, keine Effekte.
- Alle Stücke: 12 Takte, 4/4. Notenbeginn auf ganzen oder halben Beats.
- beat zählt ab 0 über das gesamte Stück; Takt 2 beginnt bei beat 4.
- duration wird in Viertel-Beats gemessen. Mindestens 0.25 Beats lang;
  die nächste Note darf nicht vor dem Ende der vorherigen beginnen.
- Höchstens vier Anschläge pro Takt bei Ruhig, sechs bei Blues und Rock.
- Jeder Entwurf soll ein erkennbares Motiv und einen bewussten Abschluss haben.
- Höchstens ein benachbarter Saitenwechsel ist wünschenswert; große Sprünge
  und dauernde Achtelketten vermeiden. Keine Kopie eines bekannten Riffs.
- velocity zwischen 0.60 und 0.95. Verwende Dynamik musikalisch.
- Bei Shuffle bleiben die Noten auf dem notierten Achtelraster. Die Hörseite
  verschiebt die zweite Achtel automatisch auf zwei Drittel des Beats.
- Nutze die vorgegebenen Akkorde und Tempi aus den sechs Aufgaben unten.
- Die Begleitung ist absichtlich für beide Komponisten identisch. Komponiere
  daher ausschließlich die Bassstimme; liefere keine eigenen Klang-Samples.

JSON-Vertrag
{
  "schema": "pickhero-composition-1",
  "author": "Claude",
  "pieces": [
    {
      "task": "ruhig-1",
      "title": "Dein eigener Titel",
      "notes": [
        {"beat": 0, "duration": 1.5, "string": "E", "fret": 0, "velocity": 0.85}
      ]
    }
  ]
}
Das Beispiel zeigt nur das Format, keine fertige Komposition.
pieces muss alle sechs task-IDs jeweils genau einmal enthalten. Noten
aufsteigend nach beat sortieren. Alle Noten müssen vor oder bei beat 48 enden.
Jeder Takt braucht mindestens einen Anschlag, Pausen innerhalb der Takte
sind ausdrücklich erwünscht. Keine zusätzlichen ausführbaren Inhalte.

Die sechs Aufgaben
__TASK_TEXT__

Liefere
1. claude-stuecke.json mit den sechs vollständigen Basslinien.
2. claude-hoerprobe.html: die ausgefüllte, vollständig eigenständige Vorlage.
   Die Seite muss sich per Doppelklick ohne Server, Installation oder Internet
   öffnen lassen. Es darf erst nach einem bewussten Klick Ton erklingen.
3. Pro Stück einen Satz zu Motiv und musikalischer Entwicklung.

Prüfe die tatsächlichen Ergebnisse auf JSON-Format, Tonumfang, Saiten/Bünde,
Taktgrenzen, Überlappungen, Notendichte und funktionsfähige Wiedergabe.
Behaupte keinen Hörtest, wenn du die Ausgabe nicht wirklich hören konntest.
Bewerte dich nicht selbst als Sieger. Ich entscheide anhand der Hörproben.
"""


def embed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    package = scores()
    task_text = "\n\n".join(
        f"{i}. {t['id']} / {t['name']}\n"
        f"   {t['bpm']} BPM, {'Shuffle 2:1' if t['swing'] else 'gerade Achtel'}, "
        f"12 Takte ({2880 / t['bpm']:.1f} Sekunden).\n"
        f"   {t['brief']}\n   Akkorde, ein Eintrag pro Takt: {' | '.join(t['chords'])}"
        for i, t in enumerate(TASKS, 1)
    )
    prompt = PROMPT.replace("__TASK_TEXT__", task_text)
    template = (SOURCE / "page.html").read_text(encoding="utf-8")
    engine = (SOURCE / "player.js").read_text(encoding="utf-8")
    for destination, data in (
        (ROOT / "docs/kompositionsvergleich.html", package),
        (OUT / "claude-vorlage.html", dict(schema="pickhero-composition-1", author="Claude", pieces=[])),
    ):
        page = (template.replace("__TASK_DATA__", embed(TASKS))
                .replace("__SCORE_DATA__", embed(data))
                .replace("__PROMPT_DATA__", embed(prompt)).replace("__PLAYER_JS__", engine))
        destination.write_text(page, encoding="utf-8", newline="\n")
    (OUT / "claude-auftrag.txt").write_text(prompt, encoding="utf-8", newline="\n")
    (OUT / "codex-stuecke.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "aufgaben.json").write_text(json.dumps(TASKS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Built six compositions, self-contained listening page, Claude template and prompt.")


if __name__ == "__main__":
    main()
