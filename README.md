# window-cast-rtsp

在 Windows 10/11 上**后台捕获指定应用窗口画面**，实时编码为 H.264，并通过 **RTSP** 对外输出，供 VLC / ffplay 等播放器拉流观看。

- 采集与编码：**FFmpeg**（`gdigrab` 窗口捕获 + `libx264` 低延迟编码）
- RTSP 服务：**MediaMTX**
- 控制层：**Python**（窗口枚举、进程启停与监控、自动重启、配置管理、日志、可选 HTTP 接口）

---

## 1. 架构与目录结构

```
window-cast-rtsp/
├── main.py                # 入口 + CLI
├── config.py              # 配置加载与校验（YAML / JSON，含 dataclass）
├── window_enum.py         # 窗口枚举（ctypes 标准库实现，零依赖）
├── process_manager.py     # MediaMTX / FFmpeg 进程管理、重启、停止
├── logger.py              # 控制台 + 文件双通道日志
├── health.py              # RTSP 可拉流检测（ffprobe + TCP 回退）
├── http_api.py            # 可选 HTTP 控制接口（FastAPI）
├── config.yaml            # 示例配置
├── requirements.txt
├── README.md
└── tools/                 # 放置 ffmpeg.exe / ffprobe.exe / mediamtx.exe
```

数据流：

```
[目标窗口] --gdigrab--> [FFmpeg: libx264 编码] --RTSP推流--> [MediaMTX:8554]
                                                                   ^
                                              VLC / ffplay <-- rtsp://127.0.0.1:8554/window
```

---

## 2. 环境准备

### 2.1 Python

- 需要 **Python 3.10+**（建议 3.10~3.12，3.13 亦可）。
- 安装依赖（MVP 仅标准库即可运行，以下均为可选）：

```bash
pip install -r requirements.txt
```

> 说明：`PyYAML` 用于读取 YAML 配置；`pywin32` 为窗口枚举的可选增强；`fastapi`/`uvicorn` 仅启用 HTTP 接口时需要。**MVP 无任何第三方依赖也能跑**（配置会自动回退到 JSON 格式）。

### 2.2 FFmpeg / ffprobe

下载 Windows 版 FFmpeg（含 `ffmpeg.exe` 与 `ffprobe.exe`）：

- 官方：<https://www.gyan.dev/ffmpeg/builds/>（选 `ffmpeg-release-full.7z`，解压后取 `bin/` 下的 `ffmpeg.exe`、`ffprobe.exe`）
- 或 <https://github.com/BtbN/FFmpeg-Builds/releases>（选 `ffmpeg-master-latest-win64-gpl.zip`）

将 `ffmpeg.exe`、`ffprobe.exe` 放入本项目 **`tools/`** 目录，**或**加入系统 `PATH`。

### 2.3 MediaMTX

- 官方 Release：<https://github.com/bluenviron/mediamtx/releases>
- 下载 Windows 版本，如 `mediamtx_v1.x.x_windows_amd64.zip`，解压得到 `mediamtx.exe`。

将 `mediamtx.exe` 放入本项目 **`tools/`** 目录，**或**加入系统 `PATH`。

> 程序查找顺序：`config` 中指定的绝对路径 → 项目 `tools/` 目录 → 系统 `PATH`。

---

## 3. 配置

编辑 `config.yaml`，关键项：

```yaml
rtsp:
  host: "127.0.0.1"     # 对外拉流可改 0.0.0.0
  port: 8554
  stream:
    name: "window"
    capture:
      title: "记事本"    # 目标窗口标题，支持 * 通配，如 "*Chrome*"
      process: ""        # 或按进程名匹配，如 notepad.exe
      fps: 30
    encoder:
      codec: "libx264"
      preset: "ultrafast"    # 低延迟
      tune: "zerolatency"
      bitrate: "2000k"
```

> 若未安装 PyYAML，可复制为 `config.json` 并用 `--config config.json` 指定。

---

## 4. 运行

### 4.1 列出可用窗口

```bash
python main.py --list-windows
```

输出每个窗口的句柄 / 标题 / PID / 进程名，把目标窗口标题填入 `config.yaml` 的 `capture.title`。

### 4.2 启动采集与推流

```bash
python main.py --config config.yaml
```

命令行快捷覆盖（无需改配置文件）：

```bash
python main.py -c config.yaml --title "记事本"          # 按标题
python main.py -c config.yaml --process notepad.exe     # 按进程名
python main.py -c config.yaml --stream mywin --http     # 改流名 + 开 HTTP
```

启动成功后控制台会打印：

```
RTSP URL: rtsp://127.0.0.1:8554/window
```

### 4.3 验证拉流

VLC：

```bash
# 打开媒体 -> 网络串流，输入：
rtsp://127.0.0.1:8554/window
```

或命令行：

```bash
vlc rtsp://127.0.0.1:8554/window
```

ffplay：

```bash
ffplay -rtsp_transport tcp -fflags nobuffer rtsp://127.0.0.1:8554/window
```

`-fflags nobuffer` 可进一步降低播放端缓冲延迟。

---

## 5. HTTP 控制接口（可选）

配置 `http.enabled: true` 或加 `--http` 启动后：

| 方法 | 路径      | 说明                         |
|------|-----------|------------------------------|
| GET  | `/status` | 运行状态、子进程状态、RTSP URL |
| GET  | `/rtsp`   | 返回 RTSP URL                |
| POST | `/start`  | 启动                        |
| POST | `/stop`   | 停止                        |

```bash
curl http://127.0.0.1:8080/status
```

---

## 6. 关键实现说明与已知限制

1. **采集方式**：默认 `gdigrab`，命令等价于
   `-f gdigrab -framerate <fps> -i title=<窗口标题>`。
2. **低延迟推流参数**：`-c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g <fps*2> -f rtsp <url>`（TCP 传输）。
3. **窗口遮挡**：窗口被其他窗口**部分遮挡**通常仍可捕获（gdigrab 读取窗口 DC）；但窗口**完全最小化**时可能黑屏或停止刷新，**MVP 不承诺最小化采集**。若需最小化采集，需另行评估 Windows Graphics Capture / DXGI Desktop Duplication 方案，存在额外复杂度与兼容性风险。
4. **硬件编码**：启动时自动探测 `h264_nvenc` / `h264_qsv`，可用则优先，失败自动回退 `libx264`（`hw_fallback` 可关）。
5. **中文标题**：`title` 支持 `*` 通配模糊匹配，兼容中文（`fnmatch`）。
6. **子进程隐藏窗口**：所有子进程以 `CREATE_NO_WINDOW` 启动，无控制台弹窗。
7. **自动重启**：子进程异常退出自动重启，带指数退避，单进程最大 `max_restarts` 次。
8. **优雅停止**：Ctrl+C 或 `/stop` 先停 FFmpeg，再停 MediaMTX。

### 常见问题排查

| 现象 | 处理 |
|------|------|
| `找不到 ffmpeg 二进制` | 将 ffmpeg.exe 放入 `tools/` 或改 `process.ffmpeg_path` 为绝对路径 |
| `未找到匹配窗口` | 先 `--list-windows`，确认标题与进程名，窗口需可见 |
| `MediaMTX 启动失败` | 检查端口 8554 是否被占用（`netstat -ano | findstr 8554`） |
| FFmpeg 日志报错 | 查看 `logs/ffmpeg.log`；`logs/window-cast.log` 含错误摘要 |

---

## 7. 下一步可增强项

- [ ] 多窗口多路流（MediaMTX 多 path + 多 FFmpeg 实例）
- [ ] 最小化/离屏采集：Windows Graphics Capture / DXGI Desktop Duplication
- [ ] 更细的硬件编码参数与 NVENC 调优
- [ ] 帧率/码率动态调整与码控策略
- [ ] 鉴权（MediaMTX path 级 auth）、RTSP over TLS
- [ ] 打包为 Windows 服务（NSSM / pywin32 service）
- [ ] Web 控制台（列表流状态、预览缩略图）

---

## 8. 验收自测清单

1. `python main.py --list-windows` 能列出窗口 ✓
2. 配置标题后启动，MediaMTX 与 FFmpeg 正常启动 ✓
3. VLC 打开 `rtsp://127.0.0.1:8554/window` 看到画面 ✓
4. 手动结束 FFmpeg 进程，日志记录并自动重启 ✓
5. Ctrl+C 干净停止所有子进程 ✓
