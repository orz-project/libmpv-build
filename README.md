# libmpv-build

构建锁定版本的 libmpv 运行库。构建系统是纯 Python 标准库实现的 `libmpv_build` 包，统一入口为：

```sh
python3 -m libmpv_build <build|package|verify|update|release-notes> [options]
```

## 交付目标

| 目标 | 运行库 | 最低条件 | 保留的系统后端 |
| --- | --- | --- | --- |
| `android-arm64-v8a` | `libmpv.so` | API 28，16 KiB ELF 对齐 | GLES、MediaCodec、AudioTrack |
| `android-x86_64` | `libmpv.so` | API 28，16 KiB ELF 对齐 | GLES、MediaCodec、AudioTrack |
| `windows-x64` | `libmpv-2.dll` | Windows x64 / UCRT | D3D11、OpenGL、WASAPI、DirectWrite |
| `linux-x64` | `libmpv.so.2` | glibc ≤ 2.35（Ubuntu 22.04 基线） | OpenGL/EGL、VAAPI、VDPAU、PulseAudio、ALSA |

每个目标输出一个确定性 ZIP：`dist/libmpv-<release>-<target>.zip`，内含单个已 strip 的运行库，并在 `dist/SHA256SUMS` 中记录摘要。

- 第三方库全部静态链接进运行库，仅依赖各平台的系统图形/音频接口；Windows 上 `libass` 使用 DirectWrite，不带 `fontconfig`/`libxml2`
- FFmpeg 以 `-O2` + ThinLTO 编译以保证软解吞吐，其余第三方库使用 `-Os` + ThinLTO
- 保留 stream callback、软件解码、ASS 字幕、HLS、`screenshot-raw` 及各平台渲染/音频后端
- 对外仅导出 `mpv_*` 符号（Android 额外导出 `av_jni_set_java_vm`），公开头文件见 `include/mpv/`

## 依赖锁定

`dependencies.json` 是唯一的版本来源，记录发行号（`release`）、工具链、构建工具，以及每个依赖的 tag、版本、完整 commit、上游日期与源码 SHA-256。

| 类别 | 内容 |
| --- | --- |
| 工具链 | Android NDK r29（`29.0.14206865`）、llvm-mingw UCRT、Ubuntu 22.04 镜像（clang/LLVM 14） |
| 构建工具 | Meson（按 hash 安装） |
| 核心依赖 | mpv、FFmpeg、libplacebo、libass、dav1d、freetype、fribidi、harfbuzz、libunibreak、zlib、Vulkan-Headers |
| 平台专属 | `fontconfig`/`libxml2`（Android、Linux）；`shaderc`/`spirv-cross`/`glslang`/`SPIRV-*`（Windows） |

各目标的 configure 选项在 `targets.json` 中声明；第三方许可证位于 `licenses/`。

## 构建

### 环境要求

- Linux 宿主（`build` 拒绝在其他系统运行）
- Docker：完整受控构建在锁定 digest 的 Ubuntu 22.04 镜像中进行，`linux-x64` 必须使用 `--container`
- 首次构建需联网下载源码与工具链，之后命中 `.cache/`

### 常用命令

```sh
# 构建全部四个目标
python3 -m libmpv_build build --all --container --jobs 4

# 单目标构建
python3 -m libmpv_build build --target linux-x64 --container --jobs 4
python3 -m libmpv_build build --target windows-x64 --container --jobs 4
python3 -m libmpv_build build --target android-x86_64 --container --jobs 4 --ndk /path/to/ndk/29.0.14206865
```

### `build` 参数

| 参数 | 说明 |
| --- | --- |
| `--target <t>` / `--all` | 二选一：单个目标或全部目标 |
| `--jobs N` | 并行度，默认 `min(CPU 数, 8)` |
| `--container` | 在锁定的 Ubuntu 22.04 容器内构建 |
| `--ndk DIR` | 使用本机 NDK，revision 必须为 `29.0.14206865`；容器内以只读方式挂载 |
| `--toolchain DIR` | 使用本机 llvm-mingw UCRT 工具链（须与锁定版本一致） |
| `--verify-cache` | 对源码/工具链缓存执行全量逐字节 SHA-256 复核 |
| `--no-clean-dist` | 跳过构建前的 `dist` 清理 |

未指定 `--ndk` 时依次尝试 `ANDROID_NDK_ROOT`、`ANDROID_NDK_HOME`、`$ANDROID_HOME/ndk/<revision>`、`$ANDROID_SDK_ROOT/ndk/<revision>`，都不可用时下载锁定的 NDK 压缩包。

### 增量缓存

- 每个依赖按 DAG 上游身份哈希独立缓存在 `build/<target>/stages/<dep>/<dep_id>`，通过 `DESTDIR` 隔离安装，再干净拼装出 `prefix/`；只修改 `mpv` 或 `ffmpeg` 时，前置库直接复用缓存
- 源码与工具链解包缓存默认使用原子解包后的元数据清单校验（毫秒级），`--verify-cache` 才做全量哈希

### `dist` 清理

每次实际构建开始前自动清理 `dist/`：删除旧版本产物和本次所选目标的上一份输出，并同步重写 `SHA256SUMS`。单目标构建会保留同版本其他目标的产物，以便多个目标并行构建；`--all` 持有全目标构建锁，只清理一次。

## 校验与打包

`build` 结束时会自动执行打包，打包前强制运行二进制校验门禁。两者也可单独调用：

```sh
# 校验运行库文件或交付 ZIP
python3 -m libmpv_build verify --target linux-x64 --path dist/libmpv-0.42.0-r6-linux-x64.zip --smoke

# 对已构建的 prefix 重新打包
python3 -m libmpv_build package --target linux-x64 --prefix build/linux-x64/prefix
```

`verify` 的检查项：

| 目标 | 检查 |
| --- | --- |
| 全部 | ELF/PE 架构正确；仅导出 `mpv_*` 公开符号，无第三方符号泄漏；可选 `--headers-dir` 校验 `include/mpv/*.h` 与仓库一致 |
| Android | `PT_LOAD` 16 KiB 对齐；导出 `av_jni_set_java_vm`；`DT_NEEDED` 仅限系统库白名单 |
| Linux | 所需 `GLIBC` 符号版本 ≤ 2.35；`DT_NEEDED` 白名单；`--smoke` 通过 `ctypes` 装载并初始化 |
| Windows | PE32+ AMD64 DLL；导入仅限系统 DLL 白名单；禁止转发导出和无名序号导出 |

`package` 的约束：

- 公开头文件一致性校验与 Linux 装载初始化冒烟强制执行，不提供跳过开关
- `build-info.json` 绑定编译输入身份、实际安装的库与头文件 SHA-256；库被替换、头文件变动或旧记录缺少摘要时必须重新构建
- 打包期间输入发生变化即中止，并保留上一次成功的产物；ZIP 时间戳取自 `SOURCE_DATE_EPOCH`（默认 `0`）
- 独立 `package` 与同目标 `build` 共用同一把构建锁

## 依赖更新与发布说明

```sh
python3 -m libmpv_build update                    # 仅检查上游稳定版本
python3 -m libmpv_build update --write --commit   # 写入 dependencies.json 并提交
python3 -m libmpv_build release-notes --output release-notes.md
```

`update --write --commit` 在上游有稳定更新时原子写入 `dependencies.json`，`mpv` 变更时同步更新 `include/mpv/*.h`，然后创建提交。`release-notes` 根据锁文件、`SHA256SUMS` 与基线版本生成 Markdown 发布说明。

## CI

`.github/workflows/native.yml`：

| 触发 | 行为 |
| --- | --- |
| `push`（`main`/`master`）、`pull_request` | 运行单元测试 |
| `schedule`（每日 UTC 20:00） | 检查上游更新；有更新时推送锁文件提交，四个目标全部构建并校验通过后发布不可变 GitHub Release |
| `workflow_dispatch` | 默认只做验证构建；勾选 `publish=true` 且新 tag 不存在时才发布 |

已存在的 tag 或 Release 永远不会被覆盖。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `libmpv_build/` | 构建系统：CLI、锁文件、源码与工具链、配方、DAG 构建、校验、打包、更新 |
| `dependencies.json` | 版本、commit、日期与摘要锁定 |
| `targets.json` | 各目标的平台参数与 configure 选项 |
| `patches/` | Android / 桌面端导出符号表 |
| `include/mpv/` | 与锁定 mpv 版本一致的公开头文件 |
| `licenses/` | 第三方许可证 |
| `tests/` | 纯标准库单元测试 |
| `Dockerfile` | 锁定的 Ubuntu 22.04 构建镜像 |

## 开发

脚本支持 Python 3.10 及以上版本，Ubuntu 22.04 的 CI 与构建镜像使用 Python 3.10。`ruff.toml` 将目标版本固定为 `py310`，自动修复时应保留此配置。

修改构建逻辑后运行：

```sh
python3 -m unittest discover -s tests -v
```

## 许可证

见 [LICENSE](LICENSE)，第三方组件许可证见 `licenses/`。
