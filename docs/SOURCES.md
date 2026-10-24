# 来源与许可证核实

> 核实日期：2026-10-24 ｜ 核实方式：GitHub REST API + 上游仓库原文文件 + 本机二进制 `-buildconf`

## 1. 上游

| 项 | 值 |
| --- | --- |
| 项目 | FFmpeg |
| 仓库 | https://github.com/FFmpeg/FFmpeg |
| Stars | 65,624（2026-10-24 通过 `api.github.com/repos/FFmpeg/FFmpeg` 读取） |
| 官方许可声明 | `LICENSE.md`（https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md） |
| 许可证原文 | `COPYING.LGPLv2.1`（502 行）、`COPYING.GPLv2`、`COPYING.LGPLv3`、`COPYING.GPLv3` |
| GitHub API 的 license 字段 | `NOASSERTION` / `Other` —— **这不是"无许可证"**，而是 GitHub 的识别器无法把 FFmpeg 的多层许可映射成单一 SPDX 标识。真实条款以 `LICENSE.md` 为准 |

### 1.1 上游默认许可（原文摘录）

`LICENSE.md` 第 3–6 行：

> Most files in FFmpeg are under the GNU Lesser General Public License version 2.1
> or later (LGPL v2.1+). Read the file `COPYING.LGPLv2.1` for details. Some other
> files have MIT/X11/BSD-style licenses. In combination the LGPL v2.1+ applies to
> FFmpeg.

### 1.2 启用 GPL 组件后的变化（原文摘录）

`LICENSE.md` 第 8–11 行：

> Some optional parts of FFmpeg are licensed under the GNU General Public License
> version 2 or later (GPL v2+). See the file `COPYING.GPLv2` for details. None of
> these parts are used by default, you have to explicitly pass `--enable-gpl` to
> configure to activate them. **In this case, FFmpeg's license changes to GPL v2+.**

`LICENSE.md` 第 84–97 行还列明：`libx264`、`libx265`、`libxvid` 等库本身是 GPL v2，
与它们组合时 FFmpeg 也必须是 GPL。

## 2. 本机实际使用的构建

```
ffmpeg version 7.1-essentials_build-www.gyan.dev
built with gcc 14.2.0 (Rev1, Built by MSYS2 project)
configuration: --enable-gpl --enable-version3 ... --enable-libx264 --enable-libx265 ...
```

核实命令：`ffmpeg -hide_banner -buildconf`

结论：

| 层 | 许可 |
| --- | --- |
| 上游源码的默认状态 | LGPL v2.1+ |
| **本机这个二进制**（`--enable-gpl` + 链接 `libx264`/`libx265`） | **GPL v2+**（可用 v3 条款，因带 `--enable-version3`） |

## 3. 本项目与 FFmpeg 的关系

- 本项目**不链接** FFmpeg 的库（不使用 libav* 的 C API），**不分发** FFmpeg 二进制。
- 仅通过 `subprocess` 调用已安装在用户机器上的 `ffmpeg` 命令行程序。
- 因此本项目自身代码采用 MIT（见根目录 `LICENSE`），与 FFmpeg 的 GPL 义务相互独立。
- **但**：若你要把 ffmpeg 二进制与本项目一起分发，那个二进制仍受 GPL v2+ 约束，
  须一并提供对应源码或书面许可，MIT 覆盖不到它。

## 4. 本项目实际调用到的 FFmpeg 功能

全部经本机实测确认存在（`ffmpeg -filters`、`ffmpeg -muxers`、`ffmpeg -encoders`）：

| 类别 | 名称 | 用途 |
| --- | --- | --- |
| 滤镜 | `scale` | 统一分辨率 |
| 滤镜 | `fps` | 统一帧率 |
| 滤镜 | `format` | 统一像素格式 |
| 滤镜 | `concat` | 拼接多段视频 |
| 滤镜 | `amix` | 混音（`normalize` 选项是第 3 步教学核心） |
| 滤镜 | `apad` | 音频尾部补静音 |
| 滤镜 | `loudnorm` | EBU R128 响度归一化 |
| 滤镜 | `ebur128` | 综合响度测量 |
| 滤镜 | `volumedetect` | 平均/峰值电平测量 |
| 滤镜 | `showinfo` | 帧级时间戳诊断（仅排查用） |
| 源 | `testsrc2` / `sine` / `color` | 合成确定性素材 |
| 编码器 | `libx264`（GPL）、`aac`（LGPL） | 视频/音频编码 |
| 封装 | `mp4`、`wav`、`null`、`framemd5`、`lavfi` | 输出与测量 |

## 5. 未使用的能力（避免误解）

- 未使用 `ffprobe`（本机不存在，见 README §6.1）。
- 未使用硬件加速（`cuda`/`qsv`/`amf` 虽在构建里，但断言依赖确定性，软编码更稳）。
- 未使用 `-filter_complex_script`、`sendcmd` 等外部脚本注入面。

## 6. 本仓库自身

| 项 | 值 |
| --- | --- |
| 代码许可 | MIT（`LICENSE`） |
| 第三方声明 | `NOTICE` |
| 是否含上游源码 | 否，零拷贝 |
| 运行时依赖 | Python 3.9+ 标准库、系统 `ffmpeg`；界面端零第三方 JS |
