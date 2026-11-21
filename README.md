# yjp-dicom-downloader

医疗影像云（海纳医信 / HinaPACS）一键下载工具：把医院影像分享链接粘贴进来，自动下载该检查的**全部原始影像**，重建成标准 **DICOM** 文件并打包。

适用于北京友谊医院 `yjp.bfh.com.cn` 等 HinaPACS 平台；同类医院可通过 `--base8080 / --base8000` 复用。

## 特性

- 并发下载（默认 8 线程，`-j` 可调）
- 断点续传：中断后重跑同一命令，已下载文件不重复下载
- 原子写入：先写临时文件再改名，避免中断产生半截 `.dcm`
- 分享链接自动续 token，401 时自动重新登录
- 可选 ZIP 整包、预览图与影像汇总页
- 保留原始 CT 值、窗宽窗位、像素间距、层厚及患者/检查信息

## 快速开始

要求 Python 3.9+（Windows 安装时勾选 `Add python.exe to PATH`），依赖启动时自动安装，也可手动：

```bash
pip install requests pydicom numpy pillow
```

### Windows 双击

双击 **`一键下载.bat`** → 粘贴分享链接 → 回车，结果输出到 `下载结果\患者姓名_检查号_日期\`。

### 命令行（Windows / macOS / Linux）

```bash
python yjp_dl.py "https://yjp.bfh.com.cn:8080/Study/ViewShared/xxxx-xxxx"
```

macOS / Linux 也可双击 `一键下载.command`（首次需 `chmod +x 一键下载.command`）。

### 常用参数

| 参数 | 说明 | 示例 |
|---|---|---|
| `-o` | 输出目录（默认 `./下载结果`） | `-o D:\影像` |
| `--series` | 只下指定序列号，逗号分隔 | `--series 2,3` |
| `--max` | 只下前 N 幅（试跑用） | `--max 20` |
| `-j` | 并发数，默认 8 | `-j 12` |
| `--no-zip` | 不生成 ZIP | |
| `--no-preview` | 不生成预览图和汇总页 | |
| `--keep-cache` | 保留中间缓存（续传更快） | |

## 支持的链接

| 链接形式 | 说明 |
|---|---|
| `...:8080/Study/ViewShared/<guid>` | **分享链接，推荐**（自动续 token） |
| `...:8080/Study/ViewImage?studyId=<uid>` | 查看链接，需原浏览器会话有效 |
| `...:8000/entry/viewimage?token=<token>` | 一次性入口链接，只能用一次 |

> 只输入 studyId 无法匿名访问，必须使用医院提供的分享链接。

## 输出内容

```
下载结果\患者姓名_检查号_日期\
├─ Series_001_Scout\            各序列独立目录
├─ Series_002_Abdomen_1.25mm\   每幅一个 .dcm
├─ 预览图\                      各序列代表层 PNG
├─ 影像汇总.html                检查与序列明细
└─ (同级) xxx_DICOM.zip         整包压缩
```

浏览软件：**RadiAnt**（Windows，免费）、**MicroDicom**（Windows）、**Horos**（Mac）、**OSMIx**（Mac）。打开整个序列文件夹即可翻阅或做三维重建。

## 隐私提醒

影像属于个人敏感医疗数据，请仅在本人或授权范围内使用，妥善保管，**不要上传公共网盘或公开仓库**。本仓库的 `.gitignore` 已排除 `*.dcm`、`*.zip`、`.cache/`、`预览图/` 等下载产物。

更详细的安装、FAQ 与故障处理见 [使用说明.md](使用说明.md)。
