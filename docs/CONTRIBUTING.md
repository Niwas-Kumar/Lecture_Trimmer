# Working on Lecture Trimmer

Use Python 3.11 or newer. Install `requirements.txt`, and install FFmpeg and
FFprobe. Run commands from the repository root.

```powershell
python app.py
python main.py --help
python -m lecture_trimmer --help
python -m unittest discover -s tests -v
```

The regression suite uses simulated provider responses and local synthetic
media. It must not submit a real lecture to a provider. Real-media tests require
FFmpeg; inspect skipped tests before treating a run as a release validation.

## Change placement

Follow the [source map](ARCHITECTURE.md). Put application code in
`lecture_trimmer/`, tests in `tests/`, release tooling in `tools/`, and operational
instructions in `docs/`. Keep package initializers free of work and use explicit
package imports. Do not add business logic to launchers or compatibility adapters.

Keep UI callbacks focused on input handling and presentation. Put processing
rules in the pipeline, classifier or planner that owns the behavior. Preserve
speech timestamps and complete academic activities when changing cut logic.
Use the shared cancellation helpers when submitting job work to a thread pool.

Patch the canonical module path in tests, for example
`lecture_trimmer.media.transcriber._groq_chunk`. Never call paid services in
regression tests. Cover behavior and failure recovery rather than duplicating
implementation details.

## Releases

Read [the desktop release guide](DESKTOP_RELEASE_GUIDE.txt). Add new application
files to the explicit allowlist in `tools/release_manifest.py`; `source_for()` maps
compatibility adapter locations to their legacy archive paths. The layout test
checks that source modules and assets are not accidentally omitted. Build a new
version rather than overwriting an already distributed signed version.

Before sharing, run the regression suite, verify the signed update archive and
run the installed runtime's isolated `--smoke-test`. This checks compatibility
without replacing a user's active version or interrupting a lecture. A passing
offline check does not replace a real sign-in and representative lecture test.

```powershell
python tools/build_desktop.py --version 1.0.4 --release-only
python tools/smoke_desktop.py --version 1.0.4
```

Use a new, unused version number for your release. The smoke command uses a
separate folder under `temp/` and leaves the active installation untouched.
