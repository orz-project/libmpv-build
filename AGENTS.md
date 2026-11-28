# libmpv-build: Agent Guide

This repository builds the libmpv runtime. The build system is the pure-stdlib `libmpv_build` package, invoked as `python3 -m libmpv_build <build|package|verify|update|release-notes>`. See `README.md` for user-facing usage.

## Layout

| Path | Owns |
| --- | --- |
| `libmpv_build/` | CLI, lock/config loading, sources, toolchains, recipes, DAG builder, verification, packaging, update, release notes |
| `build-config.json` | Single source of truth for compiler/linker flags, optimization tiers, recipe options, FFmpeg whitelists, patch anchors, update pins, and verification allowlists |
| `dependencies.json` | Single source of truth for release, toolchains, build tools, and every dependency's tag, version, full commit/tarball URL, upstream date and source SHA-256 |
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
- Statically link all third-party libraries into the shared runtime; depend only on system graphics/audio interfaces. Windows `libass` uses native DirectWrite without `fontconfig`/`libxml2` (while `libxml2` is linked everywhere for FFmpeg MPEG-DASH)
- Build `zlib-ng` and `dav1d` with `-O3`, `ffmpeg`/`libass`/`libplacebo`/`mpv` with `-O2`, `freetype`/`harfbuzz`/`shaderc`/`spirv-cross` with `-Os`, and all other third-party libraries with `-Oz`, using Full LTO (`-flto=full`, `-Wl,--lto-O2`), `-march=x86-64-v2` (x64) / `-march=armv8-a` (arm64), `-fno-math-errno`, `-fvisibility=hidden` (omitted on Android FFmpeg to keep `av_jni_set_java_vm` exportable), Linux Full RELRO (`-Wl,-z,relro,-z,now`), and Android RELR relocation packing; disable C++ exceptions/RTTI in `uchardet`/`libplacebo` and skip `fribidi`'s unused CLI binary. Strip unneeded symbols and do not emit debug symbol archives
- Preserve stream callbacks, software decoding (curated mainstream+classic FFmpeg whitelist), ASS subtitles + `uchardet`/`iconv`, HLS + MPEG-DASH, NVDEC/CUVID (desktop), AAudio (Android), `screenshot-raw` and the platform rendering/audio backends (Linux includes Wayland/EGL-Wayland/dmabuf-Wayland/VAAPI-Wayland compiled against `wayland 1.26.0` + `wayland-protocols 1.50` while linking Ubuntu 22.04's system `libwayland-*` 1.20.0 via compat patches for `wl_display_create_queue` and disabling optional `wl_fixes_interface`)
- Export only `mpv_*` symbols (plus `av_jni_set_java_vm` on Android)
- Keep `vulkan-headers` as a build-only dependency for libplacebo's disabled Vulkan stubs; keep the Vulkan runtime backend disabled

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
