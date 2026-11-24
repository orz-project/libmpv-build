# libmpv-build: Agent Guide

This repository builds the libmpv runtime. The build system is the pure-stdlib `libmpv_build` package, invoked as `python3 -m libmpv_build <build|package|verify|update|release-notes>`. See `README.md` for user-facing usage.

## Layout

| Path | Owns |
| --- | --- |
| `libmpv_build/` | CLI, lock/config loading, sources, toolchains, recipes, DAG builder, verification, packaging, update, release notes |
| `dependencies.json` | Single source of truth for release, toolchains, build tools, and every dependency's tag, version, full commit, upstream date and source SHA-256 |
| `targets.json` | Per-target platform parameters and configure options |
| `patches/` | Android and desktop export maps |
| `include/mpv/` | Public headers matching the locked mpv version |
| `licenses/` | Third-party licenses |
| `tests/` | Pure-stdlib unit tests |
| `Dockerfile` | Pinned Ubuntu 22.04 build image |

Keep the package stdlib-only; do not add third-party Python dependencies.

## Product Invariants

- Targets are exactly `android-arm64-v8a`, `android-x86_64`, `windows-x64` and `linux-x64`
- Android: API 28, 16 KiB `PT_LOAD` alignment, `av_jni_set_java_vm` exported
- Linux: glibc requirement must stay at or below 2.35 (Ubuntu 22.04 container baseline)
- Statically link all third-party libraries into the shared runtime; depend only on system graphics/audio interfaces. Windows `libass` uses native DirectWrite without `fontconfig`/`libxml2`
- Build FFmpeg with `-O2` for software decoding throughput and other third-party libraries with `-Os`; both use ThinLTO. Strip unneeded symbols and do not emit debug symbol archives
- Preserve stream callbacks, software decoding, ASS subtitles, HLS, `screenshot-raw` and the platform rendering/audio backends
- Export only `mpv_*` symbols (plus `av_jni_set_java_vm` on Android)

## Build, Verify and Package Rules

- Change versions only through `dependencies.json` (preferably via `update`); keep full commits, dates and digests consistent, and sync `include/mpv/*.h` when mpv changes
- `dist` is cleaned before each build: remove old releases and the selected targets' previous output, keep only the current release's other targets so single-target builds can run in parallel, and clean once for `--all`
- Binary verification (`verify`) gates packaging and must not be weakened or made skippable: ELF/PE architecture, Android 16 KiB alignment and `av_jni_set_java_vm`, Linux `GLIBC <= 2.35` and `DT_NEEDED` allowlist, Windows PE system DLL allowlist, `mpv_*`-only exports, `include/mpv/*.h` parity, and the Linux `ctypes` smoke test
- Allowlists (`config.py`) change only with a concrete reason; never widen them to make a build pass
- `build-info.json` binds compiler inputs and installed library/header digests; stale or incomplete records require a rebuild rather than a bypass

## Workflow

1. Read the relevant module and its tests before changing build logic
2. Make the smallest change that keeps the invariants above
3. Update focused tests in `tests/` and `README.md` when behavior or usage changes
4. Run `python3 -m unittest discover -s tests -v`; run real builds (`build --target <t> --container`) when recipes, toolchains or packaging change, and report any skipped check with its reason

## Safety

- Never publish, tag, release or push unless the user has explicitly authorized it
- Do not run `git reset`, `git restore` or `git stash`, and never discard another contributor's work
