# 新 Windows 接手、构建与发布指南（1.0.31）

更新：2026-10-31。适用：`nmrsz645-bit/gaixiaoshuoXpeiyin` / `main`，公开正式源码。本次只补文档，未改程序/版本/包/标签/Release/OSS/网站，也未操作另一台电脑。

先读 [README](../README.md)、[HANDOFF](../HANDOFF.md)、[AGENTS](../AGENTS.md)。本指南依次覆盖源码环境、数据迁移、版本与组包、真实更新机制、OSS、网站/catalog、验收回滚和凭据；命令按用途分段，不是可整份粘贴运行的脚本。生产上传、网站发布、真实任务恢复仍需各自明确授权。

## 1. 取得正确源码并建立环境

在新的 Windows 10/11 x64 开发目录操作。安装 [Git for Windows](https://git-scm.com/download/win)、[CPython 3.13 x64](https://www.python.org/downloads/windows/)（含 Tkinter/venv/pip）；可选 [GitHub CLI](https://cli.github.com/)。已验证本机为 **3.13.15**，既往 CI 为 **3.13.17**；工作流配置 `3.13` 会随补丁版本变化，不保证未来自动选择相同补丁。按 Python 官方发行包核对架构/补丁，不静默升级本项目依赖。

```powershell
git clone --branch main --single-branch https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin.git novel-source
Set-Location novel-source
git status --short --branch
git remote -v
git fetch origin
git pull --ff-only origin main
git log -1 --oneline
py -3.13 --version
py -3.13 -m venv .venv
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $py -m pip install -r requirements-windows-verified.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& $py -m pip check
if ($LASTEXITCODE -ne 0) { throw '依赖闭包不一致' }
cmd /c run_tests.bat
if ($LASTEXITCODE -ne 0) { throw '离线回归失败，禁止继续组包' }
```

状态不干净或 ff-only 失败时先核对修改，不能 reset/clean/强推处理。私有仓库需已有读取权限，使用浏览器交互登录，不把令牌放进 clone URL。

[`requirements.txt`](../requirements.txt) 是直接依赖，[`requirements-windows-verified.txt`](../requirements-windows-verified.txt) 是已验证的完整版本闭包，应安装后者。核心为 requests 2.32.4、edge-tts 7.2.5、imageio-ffmpeg 0.6.0、PyInstaller 6.14.2、alibabacloud-nls-python-sdk 1.0.2、oss2 2.19.1；正式与 beta 的 certifi 等闭包存在差异，不要跨分支混用。此前本分支离线回归 409 项通过；新电脑须实际运行再报告。

当前 `.github/workflows/offline-regression.yml` 在 push/PR 执行离线回归；`github-release.yml` 仅在 `v*` 标签推送时组包、整理并保存 artifact，不上传 OSS。应用仓库没有 Pages 部署配置。普通文档分支推送不切生产更新；打标签仍可能触发构建，不在本轮执行。

仅在没有真实任务/凭据的隔离副本验 GUI：

```powershell
& $py unified_app.py
```

源码运行也会创建本地配置，约300ms后尝试启动任务；不要指向旧机真实队列。正式包自带 Python/FFmpeg/证书，不要求客户装 Python，但 AI/TTS/Webhook 仍依赖网络。更新器另需系统 [.NET Framework 4.x](https://learn.microsoft.com/dotnet/framework/install/guide-for-developers)（最低具体补丁尚未独立确认）；不能把此包称为离线 AI/配音程序。

## 2. 新电脑配置与数据迁移

源码 clone、安装完整包和迁移业务数据是三件事。只从源码构建，不能从客户运行目录打包。

| 内容 | 实际位置/获取方式 | 接手规则 |
| --- | --- | --- |
| 配音配置 | 运行根的 `config.json` | GUI 保存；源码运行根为 checkout，打包后为 `app/` |
| 改写配置与规则 | 运行根的 `改写/config.json`、`检测.txt`、`ai_api_url.txt`、指定词规则 | 从旧机安全核对；改写输出与配音输入必须指向同一实际目录 |
| AI / 机器人凭据 | `改写/deepseek_api_key.txt`、`改写/wechat_webhook_url.txt` | 在本人供应商控制台/有权管理的企业微信群机器人处取得，通过 GUI 填写；不进 Git/ZIP/聊天 |
| 阿里云语音 | 本人智能语音项目 AppKey、已授权 AccessKey | GUI 保存；DPAPI 密文绑定 Windows 用户/机器，新机重新填写，不能假定复制后能解密 |
| 处理数据 | `待改小说`、`已改完成`、`完成`、失败目录、原文备份 | 包含自定义外部路径；必须连同配置中的真实路径逐项核对 |
| 事务和证据 | `.rewrite-processing`、`.voice-processing`、`.voice-receipts`、`.rewrite-queue-names`、统计、日志与发送回执 | 是恢复/防重依据，不是可清空的缓存 |

迁移前先在旧机停止任务、关闭无人值守并正常退出；另行取得迁移/恢复授权后做受控离线快照，保留原始文件、相对目录、字节数及 SHA-256。先读回清单和发送/配音回执，再决定哪些任务恢复；新旧机器不要同时领取同一份队列。配置中自定义的外部目录也要纳入快照。

只把必要配置和业务数据经安全通道交给获授权操作者，不随源码、Release 或公开证据分发。不要复制浏览器 Cookie/profile 或登录会话。原目录、备份、日志、回执、音频和处理进度均保留；同名 TXT/MP3 等待，不能覆盖或改名绕过。`已改完成` 中的 `.preparation.json` 是清理/备份映射，`.semantic-review.json` 是审改报告；默认配音只读 TXT，不能因 JSON 不计入等待数而删除。

安全配置字段见 [配置示例](../examples/configuration/README.md)；程序不读取 `.env`，示例也不能覆盖旧配置。GUI 的“测试全部接口”会真实调用 AI/TTS 并发送 Webhook，可能计费，必须另获授权。

## 3. 版本位置、构建与包布局

### 版本究竟改哪里

本轮文档不改版本。将来已批准新版本时，至少一起核对：

1. 根 [`version.json`](../version.json) 的 `version`；源码 `read_app_version()` 会读它。
2. [`unified_app.py`](../unified_app.py) 的 `DEFAULT_APP_VERSION`，是文件缺失/损坏时的显示回退。
3. [`test_pipeline.py`](../test_pipeline.py) 中 `PipelineTests.test_window_title_uses_runtime_version_file` 的当前版本、回退和窗口标题断言。
4. README/HANDOFF/发布说明，以及构建命令的 `--version`。Python Release 构建工具按参数写包内 `app/version.json`，不会替你确认根版本一致；旧 `构建桌面版.bat` 则复制根版本文件并写固定输出目录。

不要重打既有 `v1.0.30` / `v1.0.31-beta` 标签，也不要用新构建覆盖同版本旧包。源码维护提交与既有程序包可能不同；具体关联见 [源码版本核验](releases/2026-10-31-source-version-metadata.md)。

### 本地 Release 工具（不上传）

实际参数已用 `--help` 核对：

```powershell
& $py tools\build_github_release.py --help
& $py tools\prepare_domestic_release.py --help
```

`build_github_release.py --version <版本> --output <不存在的目录> [--notes <说明>]` 调用 PyInstaller。目录必须全新；工具失败会删除它自己创建的输出，因此不能指向客户目录或已有备份。`prepare_domestic_release.py --release <构建目录> --output <不存在的目录>` 校验/复制 ZIP 和清单，**不连接 OSS**。仓库没有已验证的小说产品上传脚本；第 5 节提供单独的 SDK 命令示例。


正式源码仅在已获批构建时执行；这里只在全新本地目录复现当前30，不上传、不替换既有30包：

```powershell
$version = (Get-Content -LiteralPath .\version.json -Raw | ConvertFrom-Json).version
if ($version -ne '1.0.31') { throw '先核对当前正式分支版本' }
$buildRoot = Join-Path 'C:\NPC-Releases' ('local-' + $version + '-' + [guid]::NewGuid().ToString('N'))
& $py tools\build_github_release.py --version $version --output $buildRoot --notes '隔离本地构建，不替换既有发布'
if ($LASTEXITCODE -ne 0) { throw '构建失败' }
Get-FileHash -Path (Join-Path $buildRoot '*.zip') -Algorithm SHA256
```

未来新版本获批后先完成上面的版本/测试/说明同步，再用对应新版本参数和新输出目录；不通过改命令给旧源码冒充新程序。本正式分支没有 `tools/build_installer.py`，NSIS 安装器属于独立测试分支，不要从其他分支混拷脚本。


| 产物 | 用途与真实布局 |
| --- | --- |
| `novel-processing-center-<版本>-windows-x64.zip` | 新用户完整包：根 `Start-App.cmd` 加 `app/` 整个程序；必须完整解压 |
| `novel-processing-center-<版本>-update.zip` | 更新用的完整 app 载荷；ZIP 根直接是 EXE、`version.json`、`_internal/` 等，无外层 `app/`。它不是字节差分包 |
| `latest.json` | 客户端候选清单：`version`、`notes`、`url`、`sha256`、`files`、`fullPackageUrl`、`fullPackageSha256` |
| 整理后的 `latest.js` | 网站对象 `window.NOVEL_PROCESSING_CENTER_LATEST`：`version`、`fullPackageUrl`、`fullPackageSha256` |

`sha256` 是更新 ZIP 哈希，`fullPackageSha256` 是完整 ZIP 哈希；`files` 是 app 内相对路径到逐文件 SHA-256 的映射，当前键使用 Windows 反斜线。不能拿完整 ZIP 的哈希填 `sha256`，也不能手工随意删掉 `files`。

正式 1.0.30 原始完整包 SHA-256 为 `99b205a1184a7e4898448bd91d1a8160c8edd62822244646baea87d2c00dfc6d`，更新包为 `757eea0f5e93fffe0127486837bab09d251d30271576264bbc310e0ba87f0a46`。这些只用于核对既有字节；相同版本重新打包未必得到相同哈希，不能据此替换旧包。


### 将来已获批的 GitHub 新版本发布

以下只是人工接手命令，当前不执行。正式程序发布目标是公开仓库；私有镜像并不另建同名正式程序 Release。`$buildRoot` 必须是已验收的新正式版本输出；30既有附件不重传。先人工读 `gh release view` / `git ls-remote --tags`，确认没有同版本 Release/标签，且目标提交属于已审查源码，再创建新版本。禁止 `--clobber`、重打标签或默认设置 Latest。

```powershell
$releaseRepo = 'nmrsz645-bit/gaixiaoshuoXpeiyin'
$version = (Get-Content -LiteralPath (Join-Path $buildRoot 'latest.json') -Raw | ConvertFrom-Json).version
if ($version -notmatch '^\d+\.\d+\.\d+$' -or $version -eq '1.0.30') { throw '只适用于获批的新正式版本' }
$approvedCommit = Read-Host '已审查的完整40位源码提交'
if ($approvedCommit -notmatch '^[0-9a-f]{40}$') { throw '提交格式不正确' }
$notesFile = Read-Host '已审查发布说明文件的绝对路径'
gh release create ('v' + $version) (Join-Path $buildRoot ('novel-processing-center-' + $version + '-windows-x64.zip')) (Join-Path $buildRoot ('novel-processing-center-' + $version + '-update.zip')) --repo $releaseRepo --target $approvedCommit --title ('小说处理中心 v' + $version) --notes-file $notesFile --latest=false
if ($LASTEXITCODE -ne 0) { throw 'GitHub发布失败，先核对远端状态，不盲目重试' }
gh release view ('v' + $version) --repo $releaseRepo --json tagName,isPrerelease,assets,url
gh api repos/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/latest --jq '.tag_name'
```

最后一项必须仍是 `v1.0.18`。GitHub附件还需下载回验；不代表 OSS、网站或客户已经更新。私有 beta 将来新增预发布时使用私有仓库、已审查的 beta 提交、便携包/安装器，并明确添加 **`--prerelease --latest=false`**；不要复制本段正式包命令上传 beta。

## 4. 客户端真正如何更新

稳定版由 `unified_app.py` 在 GUI 启动约 **30 秒**后检查，之后约每 **10 分钟**检查。`launch_update_check()` 仅在运行目录名为 `app` 时，把资源中的 `updater-patch/` 固定文件复制到相邻 `updater/`，启动：

```text
UpdateAgent.exe --silent --check <updater-config.json的绝对路径>
```

以上为实现说明，不是本轮应执行的命令。根 `Start-App.cmd` 当前用 `start` 启动 GUI，**没有同步等待启动器先更新**；现有稳定版可能静默检查并停止/重启应用，不能承诺“用户确认下载、下次启动安装”。新电脑复现必须先隔离活动任务。

[`updater-patch/updater-config.json`](../updater-patch/updater-config.json) 的实际字段：

| 字段 | 当前值/意义 |
| --- | --- |
| `appId` | `xiao-shuo-chu-li-zhong-xin` |
| `manifestUrl` | `https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/updates/novel/latest.json` |
| `appRoot` / `versionFile` | `..\app` / `version.json`，读安装 app 的版本 |
| `restartExecutable` | `小说处理中心.exe` |
| `excludePaths` / `preservePaths` | `["version.json"]` / `["*"]`；保持现有配置，不能据字段名推断所有异常都能恢复 |
| `requiredFiles` | `version.json`、EXE、`Start-App.cmd`、`_internal\python313.dll` 及两个 certifi 证书路径，以配置原文件为准 |

**1.0.31-beta 带 `-`，`check_for_updates()` 直接返回，不启动更新器，也不替代稳定通道。** 页面数字版本校验同样不接受 `-beta`。不要仅删除后缀来“转正式”。

旧 v1.0.18 的更新器固定读取 GitHub `releases/latest/download/latest.json`，所以公开仓库 **Latest 必须保留 v1.0.18**。正式程序 1.0.31 使用[版本专属 Release](https://github.com/nmrsz645-bit/gaixiaoshuoXpeiyin/releases/tag/v1.0.31)。新 Release 使用 `--latest=false`，测试版另加 `--prerelease`。见 [旧通道兼容约束](legacy-github-update-compatibility.md)。

更新器是外部 **.NET Framework 4.x** 固定二进制：26112 字节，SHA-256 `59e3fd575350c274e2134168d7f61c3caca8ac61377570af92e5a559f54d1241`。应用打包只复用它，**尚未找回对应原始源码/工程/构建参数**，不能宣称全部组件可重建，也不能用其他产品同名 EXE 代替。详见 [更新器依赖缺口](updater-dependency.md)。

## 5. OSS 的实际对象、整理、上传和回读

本节只供下一次单独获批的**正式生产发布**使用；本轮未上传，也未切清单。beta 停在本地/私有预发布，不执行生产上传。

北京地域 endpoint：`https://oss-cn-beijing.aliyuncs.com`；bucket：`luotuoqiluotuozhaoma-download`；公开根：`https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com`。

| Object key（不带起始 `/`） | 本地来源/发布职责 |
| --- | --- |
| `updates/novel/releases/<版本>/novel-processing-center-<版本>-windows-x64.zip` | 完整包，不可覆盖旧版本 |
| `updates/novel/releases/<版本>/novel-processing-center-<版本>-update.zip` | app 更新包，不可覆盖旧版本 |
| `updates/novel/latest.js` | 网站运行时元数据 |
| `updates/novel/latest.json` | 客户端生产清单，最后切换 |

### 整理正式候选，并备份旧公开清单

下列代码从已批准的正式构建输出开始；`$py` 是第 1 节 venv Python。必须用含候选 `latest.json` 的目录，不能填客户安装根。

```powershell
$releaseRoot = Read-Host '已获批准的正式构建输出绝对路径'
$candidate = Get-Content -LiteralPath (Join-Path $releaseRoot 'latest.json') -Raw | ConvertFrom-Json
$version = [string]$candidate.version
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw '生产只接收已验收的正式版本' }
if ($version -eq '1.0.30') { throw '既有30版本包不可覆盖；复核旧包请只读下载' }
$stageRoot = Join-Path 'C:\NPC-Releases' ('oss-' + $version + '-' + [guid]::NewGuid().ToString('N'))
& $py tools\prepare_domestic_release.py --release $releaseRoot --output $stageRoot
if ($LASTEXITCODE -ne 0) { throw '整理失败' }
$productStage = Join-Path $stageRoot 'updates\novel'
$candidate = Get-Content -LiteralPath (Join-Path $productStage 'latest.json') -Raw | ConvertFrom-Json
$publicRoot = 'https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com'
$snapshot = Join-Path 'C:\NPC-ReleaseEvidence' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $snapshot | Out-Null
foreach ($name in @('latest.js','latest.json')) {
    $uri = $publicRoot + '/updates/novel/' + $name + '?snapshot=' + [guid]::NewGuid().ToString('N')
    Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile (Join-Path $snapshot $name)
}
Get-FileHash -LiteralPath (Join-Path $snapshot 'latest.js'),(Join-Path $snapshot 'latest.json') -Algorithm SHA256
```

候选版本必须与已审查的新源码/包一致。现存路径先 HEAD/读取核对，不能因为本机目录叫“新”就覆盖 OSS 已存在对象。候选应经过 ZIP CRC、包内版本、逐文件哈希、无凭据/业务数据检查以及隔离验收。

### 可执行的 SDK 上传示例（不是仓库已有发布脚本）

依赖锁已含 `oss2==2.19.1`。以下 PowerShell 函数在当前会话定义后才可使用；只有调用时才上传。它只允许本产品前缀，以及第 6 节单独授权的历史 catalog 对象。只从环境读取短时 STS，凭据获取见第 8 节。

```powershell
function Send-NovelObject {
    param(
        [Parameter(Mandatory=$true)][string]$BucketName,
        [Parameter(Mandatory=$true)][string]$Key,
        [Parameter(Mandatory=$true)][string]$File,
        [Parameter(Mandatory=$true)][string]$ContentType,
        [switch]$Immutable
    )
    $immutableFlag = if ($Immutable) { 'true' } else { 'false' }
    @'
import os, sys
from pathlib import Path
import oss2
bucket_name, key, filename, content_type, immutable = sys.argv[1:]
allowed = (bucket_name == "luotuoqiluotuozhaoma-download" and key.startswith("updates/novel/")) or (bucket_name == "luotuoruanjiangengx" and key == "downloads/catalog.json")
if not allowed or ".." in key.split("/"):
    raise SystemExit("target outside approved product scope")
path = Path(filename)
if not path.is_file() or path.stat().st_size >= 5 * 1024**3:
    raise SystemExit("expected an existing file smaller than 5 GiB")
auth = oss2.StsAuth(os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"], os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"], os.environ["ALIBABA_CLOUD_SECURITY_TOKEN"])
bucket = oss2.Bucket(auth, "https://oss-cn-beijing.aliyuncs.com", bucket_name)
headers = {"Content-Type": content_type}
if immutable == "true":
    if bucket.object_exists(key):
        raise SystemExit("immutable target exists; do not overwrite")
    headers["x-oss-forbid-overwrite"] = "true"
else:
    headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
result = bucket.put_object_from_file(key, str(path), headers=headers)
print("uploaded", bucket_name, key, result.status, result.etag)
'@ | & $py - $BucketName $Key $File $ContentType $immutableFlag
    if ($LASTEXITCODE -ne 0) { throw '上传失败；停止后续清单切换' }
}
```

`put_object_from_file(..., headers=...)` 和 `StsAuth` 签名已在本地锁定 SDK 核对；防覆盖头参见 [阿里云官方说明](https://www.alibabacloud.com/help/en/oss/developer-reference/prevent-objects-from-being-overwritten-by-objects-that-have-the-same-names-2)。**若 bucket 开启/暂停版本控制，防覆盖头不生效**（见 [PutObject API](https://www.alibabacloud.com/help/en/oss/developer-reference/putobject)）；由 OSS 管理者先确认当前策略并串行发布，本示例的 HEAD 检查不是并发原子锁，也不授权修改 bucket 策略。生产凭据仅授予指定 prefix 的必要读写，不能包含删除/权限管理。

先上传两个不可变 ZIP：

```powershell
$bucketName = 'luotuoqiluotuozhaoma-download'
$releaseKey = 'updates/novel/releases/' + $version
$fullName = 'novel-processing-center-' + $version + '-windows-x64.zip'
$updateName = 'novel-processing-center-' + $version + '-update.zip'
$localRelease = Join-Path $productStage ('releases\' + $version)
Send-NovelObject -BucketName $bucketName -Key ($releaseKey + '/' + $fullName) -File (Join-Path $localRelease $fullName) -ContentType 'application/zip' -Immutable
Send-NovelObject -BucketName $bucketName -Key ($releaseKey + '/' + $updateName) -File (Join-Path $localRelease $updateName) -ContentType 'application/zip' -Immutable
```

再从公网完整下载，不以 PUT=200/ETag 代替 SHA-256：

```powershell
$checks = @(
    @{ Url=$candidate.fullPackageUrl; Hash=$candidate.fullPackageSha256; Name=$fullName },
    @{ Url=$candidate.url; Hash=$candidate.sha256; Name=$updateName }
)
foreach ($item in $checks) {
    $downloaded = Join-Path $snapshot $item.Name
    Invoke-WebRequest -UseBasicParsing -Uri ($item.Url + '?verify=' + [guid]::NewGuid().ToString('N')) -OutFile $downloaded
    if ((Get-FileHash -LiteralPath $downloaded -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Hash) { throw '公网包哈希不符；禁止切换清单' }
    if ((Get-Item -LiteralPath $downloaded).Length -ne (Get-Item -LiteralPath (Join-Path $localRelease $item.Name)).Length) { throw '包字节数不符' }
}
```

只有包、布局、签名/隐私检查和隔离验收均通过，才按 **ZIP → 公网哈希回读 → latest.js → latest.json 最后** 切换：

```powershell
Send-NovelObject -BucketName $bucketName -Key 'updates/novel/latest.js' -File (Join-Path $productStage 'latest.js') -ContentType 'application/javascript; charset=utf-8'
Send-NovelObject -BucketName $bucketName -Key 'updates/novel/latest.json' -File (Join-Path $productStage 'latest.json') -ContentType 'application/json; charset=utf-8'
foreach ($name in @('latest.js','latest.json')) {
    $readback = Join-Path $snapshot ('published-' + $name)
    Invoke-WebRequest -UseBasicParsing -Uri ($publicRoot + '/updates/novel/' + $name + '?verify=' + [guid]::NewGuid().ToString('N')) -OutFile $readback
    if ((Get-FileHash -LiteralPath $readback -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath (Join-Path $productStage $name) -Algorithm SHA256).Hash) { throw '清单回读不一致' }
}
```

公开读回失败时停止后续步骤，保留请求时刻、状态、候选和旧清单；缓存未刷新不能当作成功。不要重新上传/删除旧包来“清缓存”。

## 6. 网站卡片与历史 catalog：各改哪一个

网站源码是 [software-download-center](https://github.com/nmrsz645-bit/software-download-center)，当前 Pages 为 `main` 根目录，域名 `https://download.luotuoqiluotuozhaoma.com/`。**网站修改由“更新”维护会话单独执行；本应用指南不取得网站写入权。** 共用说明在 `docs-cross-pc-handoff` 文档分支，不是 Pages 来源。

| 本产品位置 | 实际字段/行为 |
| --- | --- |
| `index.html` 静态卡片 | `id="novel-version"` 的版本文字；`id="novel-download"` 的 `href` 和按钮文字 |
| 网页运行时 | 加载国内 bucket `updates/novel/latest.js`（加时间查询参数），读取 `window.NOVEL_PROCESSING_CENTER_LATEST` |
| `applyDownload()` | 使用 `version`、`fullPackageUrl`；数字版本与 HTTPS URL 合法才刷新；`fullPackageSha256` 在 JS 中保留，但该页面函数不校验包哈希 |
| 客户端 | 读取 `updates/novel/latest.json`，不读取网页卡片或 catalog |
| 标识边界 | 客户端 appId 为 `xiao-shuo-chu-li-zhong-xin`；网页只有上述两个 DOM id，不能把它们当成统一 catalog productId |

2026-10-31 核对：静态兜底仍为 **1.0.25**，运行时 JS 与客户端清单为 **1.0.30**。脚本加载失败会留下旧兜底，不能只看动态页面就宣称静态入口同步。此差异已记录，本轮不改网站。

下一次网站维护者获得本产品网页发布授权后，按 [共用网站操作串](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#website) 获取最新 main，仅编辑小说卡片上述三个值：

```powershell
# 仅供网站维护会话执行；当前应用文档任务不执行这些命令。
git clone https://github.com/nmrsz645-bit/software-download-center.git
Set-Location software-download-center
git switch main
git pull --ff-only origin main
git status --short
notepad .\index.html
git diff --check
git diff -- index.html
# 确认只含已授权的小说卡片变更后：
git add -- index.html
git diff --cached --name-only
git commit -m 'docs: sync approved novel download card'
git push origin main
gh api repos/nmrsz645-bit/software-download-center/pages/builds/latest --jq '{status,commit}'
```

main 推送会触发 Pages；发布后用真实浏览器检查加载后的 `novel-version` 文本、`novel-download.href`，再完整下载这个 href 验 SHA-256。只读 HTML 不会执行 JS，不能代替页面运行时验收。

### 历史 catalog 的真实对象和条件操作

当前 `index.html` **不加载 catalog**，仓库也没有生效的统一 `catalog.json` 或 catalog 上传脚本。历史 bucket 为 `luotuoruanjiangengx`（北京地域）：

- `catalog.json`：根字段 `siteTitle`、`apps`；本次无 `id=xiao-shuo-chu-li-zhong-xin` 的记录，不要盲目插入或改同名近似项目。
- `downloads/catalog.json`：同样是 `siteTitle`、`apps`；唯一对应记录 `id=xiao-shuo-chu-li-zhong-xin`，现有字段为 `id/name/description/version/visible/downloadUrl`，本次版本仍为 **1.0.15**，旧 `downloadUrl=packages/xiao-shuo-chu-li-zhong-xin-1.0.15.zip`。

此历史记录与当前网页无联动。没有核实其他历史消费者是否接受绝对 URL，因此以下命令**仅展示精确候选编辑方式**；上传还必须有该历史对象的独立维护授权及消费者兼容证据，不属于普通小说发布的必做步骤。不得为“全站同步”批量修改其他条目。

```powershell
$catalogOriginal = Join-Path $snapshot 'legacy-catalog.original.json'
$catalogCandidate = Join-Path $snapshot 'legacy-catalog.candidate.json'
Invoke-WebRequest -UseBasicParsing -Uri ('https://luotuoruanjiangengx.oss-cn-beijing.aliyuncs.com/downloads/catalog.json?read=' + [guid]::NewGuid().ToString('N')) -OutFile $catalogOriginal
@'
import json, sys
from pathlib import Path
original, output, manifest_path = map(Path, sys.argv[1:])
catalog = json.loads(original.read_text(encoding="utf-8-sig"))
manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
matches = [x for x in catalog["apps"] if x.get("id") == "xiao-shuo-chu-li-zhong-xin"]
if len(matches) != 1:
    raise SystemExit("expected exactly one approved catalog entry")
matches[0]["version"] = manifest["version"]
matches[0]["downloadUrl"] = manifest["fullPackageUrl"]
output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
'@ | & $py - $catalogOriginal $catalogCandidate (Join-Path $productStage 'latest.json')
if ($LASTEXITCODE -ne 0) { throw 'catalog候选生成失败' }
git diff --no-index -- $catalogOriginal $catalogCandidate
# diff返回1表示存在差异；人工确认只有目标条目的两个字段发生语义变化。
# 只有维护授权/绝对URL消费者兼容/旧对象备份均齐备时，才执行：
Send-NovelObject -BucketName 'luotuoruanjiangengx' -Key 'downloads/catalog.json' -File $catalogCandidate -ContentType 'application/json; charset=utf-8'
$catalogReadback = Join-Path $snapshot 'legacy-catalog.readback.json'
Invoke-WebRequest -UseBasicParsing -Uri ('https://luotuoruanjiangengx.oss-cn-beijing.aliyuncs.com/downloads/catalog.json?verify=' + [guid]::NewGuid().ToString('N')) -OutFile $catalogReadback
if ((Get-FileHash -LiteralPath $catalogReadback -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $catalogCandidate -Algorithm SHA256).Hash) { throw '历史catalog回读不一致' }
```

候选序列化会改变缩进，复核应比较解析后的对象：其他 apps、条目顺序及该记录除 `version/downloadUrl` 外的字段都必须一致。若旧消费者只接受相对路径，上述绝对 URL 候选不能上传；先由维护者确认映射和真实可下载对象，而非猜一个旧 bucket 相对路径。

## 7. 验收、隔离升级与回滚

一次发布分别记录：源码提交/版本、离线回归、完整/更新 ZIP 的名称/字节数/SHA-256/CRC/包内版本、逐文件哈希、隐私扫描、公网下载回验、JS/JSON 原始读回、浏览器卡片实际 href，以及隔离旧版升级/失败回滚/受保护数据证据。每一项独立下结论。

新电脑只验构建时不触发生产更新。将来获批升级测试，用全新 VM/独立 Windows 用户与合成任务，先确认系统没有真实同名应用和守护进程；仅改安装目录不能隔离全局单实例、同名进程、快捷方式和 HKCU 影响。保留旧完整包、旧配置/数据哈希及测试前快照。完整 ZIP 解压后从根 `Start-App.cmd` 启动，稳定版须观察约30秒后的真实更新器过程、版本变化、程序重新可用和数据完整性。窗口显示版本/EXE能开/HTTP200均不足以证明升级成功。

测试版安装器依赖 NSIS 3，安装/覆盖/卸载会涉及快捷方式、HKCU 及同名进程检测，不能在真实客户 Windows 用户环境试跑。beta 自动更新关闭，无 beta 升级成功结论。

服务器侧回退必须先读回并验证旧清单引用的包仍可下载且哈希一致，确认没有其他发布者切换清单；在独立回滚授权下才把第5节备份的原始对象恢复。**先 JS，JSON 最后**，用同一回读循环与备份哈希比较：

```powershell
# $snapshot必须指向已确认的该次发布前快照，不是任意旧目录。
Send-NovelObject -BucketName 'luotuoqiluotuozhaoma-download' -Key 'updates/novel/latest.js' -File (Join-Path $snapshot 'latest.js') -ContentType 'application/javascript; charset=utf-8'
Send-NovelObject -BucketName 'luotuoqiluotuozhaoma-download' -Key 'updates/novel/latest.json' -File (Join-Path $snapshot 'latest.json') -ContentType 'application/json; charset=utf-8'
```

这只恢复服务端入口，**不保证已升级客户端自动降级**。网页静态卡片另由网站维护者正常 `git revert <网站提交>` / push 并等 Pages 验证；历史 catalog 若曾独立改动，也单独恢复其备份，不碰其他产品。

客户端故障时先停止新增任务并保全当前 `app`、若存在的 `app.previous`、外部数据、更新证据和日志；旧目录可能早于升级后产生的数据，不能直接覆盖回来。没有已确认的本更新器原始源码/回滚 CLI，**不要编造 `--rollback` 或盲目重命名/清空目录**。先核对事务与回执，再使用已批准的已验证旧完整包隔离恢复；只有数据/凭据/外部发送状态对齐后才能恢复真实业务。

## 8. 凭据名称、授权边界与真实缺口

Git 用 Git Credential Manager / `gh auth login` 浏览器交互授权；不导出 Token、SSH 私钥或 Cookie。只需要当前仓库正常推送/读取权限，不改仓库可见性、不强推。

第5节 SDK 示例只读取当前进程的三个环境变量：

- `ALIBABA_CLOUD_ACCESS_KEY_ID`
- `ALIBABA_CLOUD_ACCESS_KEY_SECRET`
- `ALIBABA_CLOUD_SECURITY_TOKEN`

从本人受控的 RAM/STS/OIDC 或密码管理器取得短时凭据，限定北京 bucket 的小说前缀读写；历史 catalog 需单独授予该 key，不能借用其他产品权限。不要打印值、写入 `.env`/命令历史/脚本/Git/ZIP/聊天。不具备凭据就是生产上传阻碍，不绕过、不扩大权限。

若已授权使用交互方式注入临时 STS，以下输入均不把秘密字面量写进命令历史；优先使用既有安全注入机制：

```powershell
$stsCredential = Get-Credential -Message '临时STS：用户名填AccessKey ID，密码填AccessKey Secret'
if ($null -eq $stsCredential) { throw '未提供临时凭据' }
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = $stsCredential.UserName
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = $stsCredential.GetNetworkCredential().Password
$stsToken = Read-Host '临时STS SecurityToken' -AsSecureString
$env:ALIBABA_CLOUD_SECURITY_TOKEN = [System.Net.NetworkCredential]::new('', $stsToken).Password
```

已批准的上传及回读结束后，另执行清理当前进程变量；不删除任何本地数据文件：

```powershell
Remove-Item Env:ALIBABA_CLOUD_ACCESS_KEY_ID,Env:ALIBABA_CLOUD_ACCESS_KEY_SECRET,Env:ALIBABA_CLOUD_SECURITY_TOKEN
$stsCredential = $null
$stsToken = $null
```

清理块只在上传/回读结束后执行。示例不提供真实凭据；配置、队列、日志、回执、小说/音频、客户数据及内部对话均不进入发布物。

本指南的验证范围：本地 Git/脚本源码、参数 `--help`、SDK 签名、示例语法/链接、远端对象只读核对；文档补齐不重新打包、不调用计费接口、不执行上传、部署、客户升级或真实队列恢复。历史离线回归/包核验见 [GitHub 核验记录](releases/2026-10-31-github-verification.md)，不当作本轮重跑。

仍需后续工作：匹配 UpdateAgent 原始源码及其可复现构建；新Windows/NSIS/.NET环境实际验收；已获批发布的公网包/清单/页面/隔离升级回滚证据；网站静态25兜底同步，以及历史 catalog 消费者是否仍有用途。不能从指南或现有 Release 推断所有客户已升级。

共用网站说明只引用稳定入口，不在本指南复制跨项目索引：

- [网站](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#website)
- [卡片与 catalog](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#catalog)
- [客户端发布](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#client-update)
- [验证与回滚](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#verify-rollback)
- [凭据](https://github.com/nmrsz645-bit/software-download-center/blob/docs-cross-pc-handoff/docs/PUBLISHING.md#credentials)
