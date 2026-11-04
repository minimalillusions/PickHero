# PickHero

**Üben mit dem echten Instrument.** PickHero lässt die Tabulatur laufen, hört
über dein Mikrofon oder Audio-Interface zu und zeigt dir, welche Töne du
gefunden hast und ob sie im Takt waren — getrennt voneinander, weil es zwei
verschiedene Fähigkeiten sind.

Kostenlos, offline, für Windows. [English](README.md)

![PickHero spielt „In der Halle des Bergkönigs"](assets/screenshots/play-hifi.png)

## Was es kann

- **Hört dein Spiel** in Echtzeit und markiert jeden Ton als gefunden, knapp oder verfehlt.
- **Timing für sich:** ein zu früh gespielter Ton zählt trotzdem als gefunden; das Timing steht daneben, es wird nicht abgezogen.
- **Gitarre, Bass-Gitarre und Bass-Ukulele**, dazu jede Stimmung, die eine Tab-Datei mitbringt.
- **Ein Lernpfad** aus zehn kurzen Lektionen und acht Stücke zum Spielen — darunter
  *Pachelbels Kanon*, *When the Saints Go Marching In* und *In der Halle des Bergkönigs* —
  jeweils in einer leichten und einer ausgebauten Fassung.
- **Eigene Tabs:** Guitar Pro 3, 4, 5, 7 und 8 sowie MIDI, mit Hörprobe vor dem Spielen.
- **Übungshilfen:** bis auf 25 % verlangsamen, Abschnitte wiederholen, auf den richtigen Ton warten,
  Metronom und Einzähler.
- **Ein Einrichtungs-Leitfaden** beim ersten Start: Mikrofon, Pegel, Stimmung.
- **Drei Looks** — Studio, Stage, Hi-fi — auf Deutsch oder Englisch.

## Privat von Grund auf

Kein Konto, kein Netzwerkzugriff, keine Telemetrie. Tabs, Fortschritt und
Übungsaufnahmen bleiben auf deinem Rechner. Fremde Dateien werden in einem
eigenen, eingeschränkten Prozess gelesen.

## Loslegen

Ein fertiger Download ist in Vorbereitung. Bis dahin drei Schritte unter
Windows 10 oder 11 — Python brauchst du nicht, PickHero bringt sein eigenes mit:

1. **uv installieren**, einmalig. PowerShell öffnen (Startmenü, *PowerShell* tippen) und einfügen:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   uv schon da? Mit `uv self update` aktualisieren.
2. **PickHero holen:** oben auf dieser Seite der grüne Knopf **Code** →
   **Download ZIP**. Dann Rechtsklick auf die ZIP-Datei → **Alle extrahieren**.
3. **Doppelklick auf `START.bat`** im entpackten Ordner. Der erste Start
   richtet alles ein, das dauert ein paar Minuten und braucht einmal Internet.
   Danach startet es sofort. Meldet Windows, es habe den Computer geschützt:
   PickHero ist nicht signiert — **Weitere Informationen**, dann **Trotzdem ausführen**.

Schließ ein USB-Mikrofon an oder ein Instrumentenkabel an ein Audio-Interface.
Beim ersten Start hilft dir ein kurzer Leitfaden, es auszuwählen und den
Pegel einzustellen.

## Herkunft und Lizenz

PickHero wurde von [Artem Shamsuarov](https://github.com/Artemarius/PickHero)
begonnen. Diese Version führt es fort.

Der Code steht unter der MIT-Lizenz — siehe [LICENSE](LICENSE). Der
Windows-Build enthält Fremdkomponenten unter eigenen Lizenzen, darunter aubio
(GPL-3.0, Quellcode liegt bei) und die Schriften (SIL Open Font License); alle
Hinweise stehen in [licenses/](licenses/).
