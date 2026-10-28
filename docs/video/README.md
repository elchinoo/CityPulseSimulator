# Tutorial video: build and review cut

`docs/VIDEO-SCRIPT.md` is the script, `record.spec.ts` the recorder (real Chrome, 1920x1080, Playwright video), `build.py` turns both into the
finished file with narration and captions.

```bash
cd docs/video
npm install                                   # the project's own devDependencies (@playwright/test); needs Google Chrome
python3 build.py --deliver ../../../videos/review   # script + recorder -> out/cpsim.mp4 and .srt, copied to the review folder
```

Needs: the local Kokoro text-to-speech tool `../../../videos/tts` (`narrate.py` and its `.venv`; override with `TTS_DIR`), `ffmpeg`/`ffprobe` on PATH
(Homebrew is fine), Node, a PostgreSQL with `initdb` and `postgres` (`PG_BIN`, default `pg_config --bindir`), and `../../.venv` with the simulator installed
(`pip install -e ".[dev]"`; `CPSIM_BIN` to use another). Nothing is downloaded; no live service or real credential is used. The optional live-fleet scene 7b
of the script is not part of this cut (it needs a deployed fleet).

What `build.py` does:

1. **Narration.** `narration.txt` is the spoken text, written for listening (short sentences, one `## <scene id> <voice>` section per scene of
   VIDEO-SCRIPT.md). Blank line = pause, a line `...` = short beat, `[pause 1.5]` = deliberate long pause over something happening on screen (for
   example the Modbus registers, which stay zero for 10 to 15 s after Start city). `videos/tts/narrate.py scenes` speaks it with Kokoro (local
   neural voices, US accent; nothing leaves the machine) and writes `out/audio/scene-<id>.wav` plus `narration.json` (durations and per-sentence cues).
   The voice changes with the topic, alternating female and male, never inside a scene:
   scenes 1-2 `af_sarah` (intro, first start), 3-4 `am_michael` (the city, running it), 5-6 `af_heart` (bursts, workloads),
   7-8 `am_eric` (companies, settings and monitoring), 9 `af_bella` (recap). Pronunciation respellings live in the shared `videos/tts/pronounce.json`
   and the project's `pronounce.json` (spoken text only; captions keep the written form). Every build rewrites the NARRATION column of VIDEO-SCRIPT.md
   from `narration.txt`, so script and voice never diverge: edit `narration.txt`, not the table.
2. **Recording.** `npx playwright test` runs with `SCENE_INFO` (cue start offsets) and `SCENE_DURATIONS` (audio + 0.7 s per scene). The recorder starts each
   on-screen action when the narration sentence it illustrates begins (`at(k)` = the k-th sentence of the scene, counted from 0 across paragraphs) and
   never ends a scene before its audio plus 0.7 s. A fresh throwaway Postgres (new cluster, new database) and `cpsim serve` are created for every take on
   ports 41600-41699 (`CPSIM_PORT_RANGE`). The terminal scenes are an overlay with the real output of the real commands. The recorder's browser context
   runs with `bypassCSP` only so the overlays can be drawn; the app is unchanged.
3. **Assembly.** The recorder writes `out/timings.json` (scene start times); the build finds the title card in the webm, trims the lead-in, places each scene's
   audio at its start time and encodes H.264 yuv420p + AAC + a soft subtitle track (mov_text) with `+faststart` to `out/cpsim.mp4`. It also refreshes the time column of the script table.
4. **Captions.** `out/cpsim.srt` is timed from the measured cue times (long sentences are split in two cues).

Options: `--skip-record` re-assembles from the last recording in `out/pw`, `--narration-only` only synthesises audio and prints durations, `--reuse-audio` records again with the audio of the last build, `--name` changes the output name.
Environment: `BUILD_OUT`, `BUILD_TMP` (scratch for the Postgres cluster and the ffmpeg shim), `PLAYWRIGHT_BROWSERS_PATH`, `PG_BIN`, `CPSIM_BIN`, `CPSIM_PORT_RANGE`, `TTS_DIR`.

If Playwright's own ffmpeg build is not installed, `build.py` creates a private browsers directory under `BUILD_TMP` that links the existing browsers and
points Playwright's ffmpeg slot at the system ffmpeg. If a scene gains, loses or reorders sentences, adjust the `at(k)` anchors in `record.spec.ts` (the
recorder fails loudly when it asks for a sentence that does not exist).

`out/` is git-ignored; do not commit media.
