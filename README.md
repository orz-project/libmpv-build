# libmpv-build

构建锁定版本的 libmpv 运行库。构建系统是纯 Python 标准库实现的 `libmpv_build` 包，统一入口为：

```sh
python3 -m libmpv_build <build|package|verify|update|release-notes> [options]
```

## 交付目标

| 目标 | 运行库 | 最低条件 | 保留的系统后端 |
| --- | --- | --- | --- |
| `android-arm64-v8a` | `libmpv.so` | API 28，ARMv8-A，16 KiB ELF 对齐，RELR 重定位压缩 | GLES、MediaCodec、AAudio、AudioTrack |
| `android-x86_64` | `libmpv.so` | API 28，x86-64-v2，16 KiB ELF 对齐，RELR 重定位压缩 | GLES、MediaCodec、AAudio、AudioTrack |
| `windows-x64` | `libmpv-2.dll` | Windows 10 x64 / UCRT，x86-64-v2 | D3D11、OpenGL、NVDEC/CUDA、WASAPI、DirectWrite |
| `linux-x64` | `libmpv.so.2` | glibc ≤ 2.35（Ubuntu 22.04 基线），x86-64-v2 | OpenGL/EGL、X11、Wayland（`egl-wayland`、`dmabuf-wayland`）、VAAPI、VDPAU、NVDEC/CUDA、PulseAudio、ALSA |

每个目标输出一个确定性 ZIP：`dist/libmpv-<release>-<target>.zip`，内含单个已 strip 的运行库，并在 `dist/SHA256SUMS` 中记录摘要。

- 第三方库全部静态链接进运行库，仅依赖各平台的系统图形/音频接口；Windows 上 `libass` 使用 DirectWrite（不带 `fontconfig`），`libxml2` 全平台仅启用精简解析供 FFmpeg MPEG-DASH 与 Android/Linux `fontconfig` 使用
- 分层优化 + Full LTO：`zlib-ng`、`dav1d` 使用 `-O3`，`ffmpeg`、`libass`、`libplacebo`、`mpv` 使用 `-O2`，`freetype`、`harfbuzz`、`shaderc`、`spirv-cross` 使用 `-Os`，其余第三方库使用 `-Oz`，统一启用 Full LTO（`-flto=full` 与 `-Wl,--lto-O2`）、`-fno-math-errno`、`-fvisibility=hidden`（Android FFmpeg 为保留 `av_jni_set_java_vm` 导出除外）、`-fno-semantic-interposition` 与 `--icf=safe`；Linux 启用 Full RELRO（`-Wl,-z,relro,-z,now`），`uchardet`/`libplacebo` 关闭 C++ 异常与 RTTI，`fribidi` 跳过未使用的 CLI 工具，且 x86_64 专属参数（`dav1d` `stack_alignment=16`、`uchardet` `CHECK_SSE2`）仅对 `x86_64` 生效
- 发布策略由 `build-config.json` 统一控制：通用 `-DNDEBUG` 配合 Meson 的 `debug=false`、`b_ndebug=true` 关闭调试信息、普通开发断言和 mpv TA 内存调试，保留独立的 `mp_assert` 检查。CMake 将所选优化等级传入通用 C/C++ flags，避免 zlib-ng 将显式 `-O3` 降为 `-O2`；配方附加的 C++ 参数在平台初始化参数之后应用，确保 Android 上 `uchardet` 的异常与 RTTI 禁用设置生效，同时保留平台加固参数
- 保留 stream callback、主流与经典全覆盖的精简 FFmpeg 解码/解复用白名单、ASS 字幕 + `uchardet` 自动编码检测（Windows 用 `win-iconv`，Android 静态合入 GNU `libiconv`）、HLS + MPEG-DASH、`screenshot-raw` 及各平台渲染/音频后端；Linux 通过前缀构建 `wayland 1.26.0`（仅保留 `wayland-scanner` 与头文件）和 `wayland-protocols 1.50` 启用完整 Wayland/EGL/dmabuf/VAAPI 支持，同时修补兼容队列创建调用并禁用可选的 `wl_fixes_interface`，以保持直接链接系统 `libwayland-*` 1.20.0（Ubuntu 22.04 基线）
- 对外仅导出 `mpv_*` 符号（Android 额外导出 `av_jni_set_java_vm`），公开头文件见 `include/mpv/`

Vulkan 后端保持关闭，但锁定版 libplacebo 的占位实现仍引用 Vulkan 公共类型，因此四平台保留 `vulkan-headers` 作为编译期依赖；它不增加运行库动态依赖

## 依赖锁定

`dependencies.json` 是唯一的版本来源，记录发行号（`release`）、工具链、构建工具，以及每个依赖的 tag、版本、完整 commit/tarball URL、上游日期与源码 SHA-256。`repository` 仅用于各 Git 依赖的源码仓库地址，顶层不需要该字段。Dockerfile 从同一锁文件读取 LLVM 版本及 Meson 版本和安装摘要，锁文件变化会更新容器镜像身份。

| 类别 | 内容 |
| --- | --- |
| 工具链 | Android NDK r30（`30.0.16248370`）、llvm-mingw UCRT、Ubuntu 22.04 镜像（Clang/LLD 23，保持 glibc 2.35 基线） |
| 构建工具 | Meson、Ninja（按 hash 安装）、CMake 4.5.1（按 SHA-256 安装） |
| 核心依赖 | mpv、FFmpeg、libplacebo、libass、dav1d、freetype（裁剪古董驱动）、fribidi、harfbuzz、libunibreak、libxml2、zlib-ng（compat 模式）、uchardet |
| 平台专属 | `fontconfig`、`libiconv`（Android）；`fontconfig`、`nv-codec-headers`、`wayland`（scanner+头文件）、`wayland-protocols`（Linux）；`shaderc`（关闭 HLSL）、`spirv-cross`、`glslang`、`SPIRV-*`、`win-iconv`、`nv-codec-headers`（Windows） |
| 编译期头文件 | `vulkan-headers`（四平台，供 libplacebo 占位实现编译，不启用 Vulkan 后端） |

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
python3 -m libmpv_build build --target android-x86_64 --container --jobs 4
```

本地可以预先封装一次与当前 `dependencies.json`、`Dockerfile` 和 Ubuntu digest 匹配的镜像，后续构建直接复用：

```sh
python3 -m libmpv_build container-image
python3 -m libmpv_build build --target linux-x64 --container --reuse-container-image --jobs 4
```

`--reuse-container-image` 只接受带有当前锁定身份的本地镜像；镜像不存在或锁文件、Dockerfile 发生变化时会立即失败，并提示重新执行 `container-image`。

### `build` 参数

| 参数 | 说明 |
| --- | --- |
| `--target <t>` / `--all` | 二选一：单个目标或全部目标 |
| `--jobs N` | 并行度，默认 `min(CPU 数, 8)` |
| `--container` | 在锁定的 Ubuntu 22.04 容器内构建 |
| `--reuse-container-image` | 仅本地使用已准备且与当前锁定身份匹配的镜像；必须同时指定 `--container` |
| `--ndk DIR` | 使用本机 NDK，revision 必须为 `30.0.16248370`；容器内以只读方式挂载 |
| `--toolchain DIR` | 使用本机 llvm-mingw UCRT 工具链（须与锁定版本一致） |
| `--verify-cache` | 对源码/工具链缓存执行全量逐字节 SHA-256 复核 |
| `--no-clean-dist` | 跳过构建前的 `dist` 清理 |

未指定 `--ndk` 时依次尝试 `ANDROID_NDK_ROOT`、`ANDROID_NDK_HOME`、`$ANDROID_HOME/ndk/<revision>`、`$ANDROID_SDK_ROOT/ndk/<revision>`，只使用与 `dependencies.json` 锁定 revision 一致的本地 NDK；缺失、元数据不可读或版本不符的候选会跳过。使用 `--container` 构建 Android 目标（包括 `--all`）时，也会在宿主机自动查找，并将找到的 NDK 只读挂载到容器，无需手动指定 `--ndk`。本地没有匹配版本时，继续使用工具链缓存或下载锁定的 NDK 压缩包；显式 `--ndk` 路径或版本不正确时仍会报错。

例如，SDK 安装在 `~/tool/Android-SDK` 且已安装 `ndk/30.0.16248370` 时：

```sh
export ANDROID_HOME="$HOME/tool/Android-SDK"
python3 -m libmpv_build build --target android-arm64-v8a --container --jobs 4
```

### 增量缓存

- 每个依赖按 DAG 上游身份哈希独立缓存在 `build/<target>/stages/<dep>/<dep_id>`，通过 `DESTDIR` 隔离安装，再干净拼装出 `prefix/`；只修改 `mpv` 或 `ffmpeg` 时，前置库直接复用缓存
- 源码与工具链解包缓存默认使用原子解包后的元数据清单校验（毫秒级），`--verify-cache` 才做全量哈希

### `dist` 清理

每次实际构建开始前自动清理 `dist/`：删除旧版本产物和本次所选目标的上一份输出，并同步重写 `SHA256SUMS`。单目标构建会保留同版本其他目标的产物，以便多个目标并行构建；`--all` 持有全目标构建锁，只清理一次。

## 校验与打包

`build` 结束时会自动执行打包，打包前强制运行二进制校验门禁。两者也可单独调用：

```sh
# 校验运行库文件或交付 ZIP
python3 -m libmpv_build verify --target linux-x64 --path dist/libmpv-0.42.0-r1-linux-x64.zip --smoke

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

`build-config.json` 可增加必须导出的公共 API，但不能删掉应用依赖的 stream callback、render callback、命令结果等最低运行契约。

`package` 的约束：

- 公开头文件一致性校验与 Linux 装载初始化冒烟强制执行，不提供跳过开关
- `build-info.json` 绑定编译输入身份、实际安装的库与头文件 SHA-256；库被替换、头文件变动或旧记录缺少摘要时必须重新构建
- 打包期间输入发生变化即中止，并保留上一次成功的产物；ZIP 时间戳取自 `SOURCE_DATE_EPOCH`（默认 `0`）
- 独立 `package` 与同目标 `build` 共用同一把构建锁
- 构建或打包进程加载配置后若 `build-config.json` 变化，立即拒绝操作，需启动新进程重新加载配置

## 依赖更新与发布说明

```sh
python3 -m libmpv_build update                    # 仅检查上游稳定版本
python3 -m libmpv_build update --write --commit   # 写入 dependencies.json 并提交
python3 -m libmpv_build release-notes --output release-notes.md
```

`update --write --commit` 在上游有稳定更新时原子写入 `dependencies.json`，`mpv` 变更时同步更新 `include/mpv/*.h`，然后创建提交。`release-notes` 根据锁文件、`SHA256SUMS` 与基线版本生成 Markdown 发布说明。

Wayland 自动更新仅在 `1.x` 系列内选择稳定版（锁定主版本 `1`，排除补丁号为 90 及以上的开发版、候选版以及 `2.x` 跨主版本升级）；构建时保留 `wayland-scanner` 与头文件，运行库继续链接 Ubuntu 22.04 的系统 `libwayland-*`，升级后仍需通过兼容性验证。

为兼容系统 Wayland 1.20.0，mpv 使用旧版队列创建 API，并关闭依赖新版 `wl_fixes_interface` 的可选全局对象移除确认；Wayland/EGL/dmabuf/VAAPI 后端保持启用。

## CI

`.github/workflows/native.yml`：

| 触发 | 行为 |
| --- | --- |
| `pull_request` | 运行单元测试 |
| `schedule`（每日 UTC 20:30，北京时间次日 04:30） | 先检查依赖，有更新时提交并推送锁文件；随后始终构建四个目标，有更新且构建校验全部通过时发布 GitHub Release |
| `workflow_dispatch` | 通过 `mode` 选择下表中的模式，默认 `check-build` |

| 手动模式 | 行为 |
| --- | --- |
| `check-only`（只更新并提交） | 检查上游稳定版本，有更新时写入锁文件、同步 mpv 头文件并提交推送，不编译或发布；没有更新时不创建提交 |
| `build-only`（只编译） | 不检查上游更新，使用所选 Git ref 中的锁定依赖构建四个目标 |
| `check-build`（先检查后编译） | 检查上游更新，有更新时提交并推送锁文件，再使用该提交构建四个目标；没有更新也会编译 |

所有模式都先运行单元测试。依赖更新提交推送到触发运行的分支，手动运行时为所选分支。手动构建时，只有勾选 `publish=true` 才发布 GitHub Release；`check-only` 忽略该选项。

已存在的 Release 会替换四个平台的同名 ZIP 和 `SHA256SUMS`，并更新发布说明；已存在的 tag 保留原提交指向。tag 已存在但尚无 Release 时，会基于该 tag 创建 Release。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `libmpv_build/` | 构建系统：CLI、锁文件、源码与工具链、配方、DAG 构建、校验、打包、更新 |
| `build-config.json` | 编译/链接参数、分层优化等级、各依赖配方选项、FFmpeg 裁剪白名单、源码补丁与校验白名单配置 |
| `dependencies.json` | 版本、commit/tarball URL、日期与摘要锁定 |
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
