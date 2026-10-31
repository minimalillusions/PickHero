# Bundled typefaces

The three looks are three typefaces as much as three palettes. Until these
were here, all three rendered in Segoe UI, which is the single biggest reason
the app did not look like the drawings it was built from.

| Look   | Sans                              | Numerals / key hints |
|--------|-----------------------------------|----------------------|
| Studio | Geist Sans                        | JetBrains Mono       |
| Stage  | Chakra Petch                      | JetBrains Mono       |
| Hi-fi  | Hanken Grotesk (Light for display)| JetBrains Mono       |

All four are SIL Open Font Licence 1.1, which permits bundling and
redistribution with the application. The licence text for each sits beside
the files as `OFL-<family>.txt` and must be shipped with them.

- Geist — Vercel, in collaboration with basement.studio
- Chakra Petch — Cadson Demak
- Hanken Grotesk — Alfredo Marco Pradil
- JetBrains Mono — JetBrains

They are loaded by `design.font(face=...)`, which reads the current Look's
`face_sans` / `face_mono`. `design.BUNDLED` maps a face name to its regular
and bold file. A face that is missing falls through to the platform stack, so
a broken install renders plainly rather than not at all.
