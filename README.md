# PDF Recipient Watermark

Windows 本地工具：为 70 位收件人生成带完整邮箱水印的 PDF，保留原文件名，再用 QQ 邮箱逐人发送。生成、检查、预演和溯源可离线完成；真实发送需要 QQ SMTP 授权码。

只有三个业务模块，没有 Web 服务、插件体系或额外构建工具：

| 文件 | 职责 |
|---|---|
| `batch.py` | 批次、CSV 字段、邮箱、文件清单及哈希 |
| `watermark.py` | PDF 生成、校验和溯源 |
| `delivery.py` | 邮件预演、QQ SMTP、日志和恢复 |
| `selfcheck.py` | 使用合成资料和模拟 SMTP 的完整自检 |

运行依赖仅为免费 BSD 许可的 pypdf 与 ReportLab，其余使用 Python 标准库。真实 QQ 授权、实际投递和个人账号限额尚未验证；自检不联系邮箱服务器。

## 安装

安装 [Python 3.11+](https://www.python.org/downloads/windows/)，在项目目录打开 PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force local | Out-Null
Copy-Item recipients.example.csv local\recipients.csv
Copy-Item subject.example.txt local\subject.txt
Copy-Item body.example.txt local\body.txt
```

把真实 PDF、名单和邮件内容放在被 Git 忽略的 `local/` 内。不要提交真实资料或授权码。仓库仅保留一对同名合成 PDF，见 `examples/original/` 与 `examples/recipient/`；批次和邮件预演按需生成。

## 生成与校验

修改 `local/recipients.csv`，只保留 `email` 一列，填入 **70 个不重复的个人邮箱**；示例 `.invalid` 地址必须替换。支持普通 ASCII 邮箱及长邮箱，不接受多个地址写在同一单元格、姓名加尖括号或通讯组。地址格式检查不能判断共享邮箱或自动转发规则，须核实名单。

把未加逐人水印的 PDF 放在 `local/`，将下面输入路径换成实际文件：

```powershell
.\.venv\Scripts\python.exe watermark.py generate --source "local\培训 资料.PDF" --recipients local\recipients.csv --out local\batch_001
.\.venv\Scripts\python.exe watermark.py verify --batch local\batch_001
```

默认严格要求 70 人。输出目录必须是新的，下一批用 `batch_002`；中途失败不得分发，解决问题后在新目录重做。原文件不修改，文件名、空格、中文及扩展名大小写完全保留：

```text
batch_001/
  pdfs/001/培训 资料.PDF
  pdfs/002/培训 资料.PDF
  ...
  pdfs/070/培训 资料.PDF
  mapping.csv
  send_log.csv
  run.json
```

每页只有本份收件人的完整邮箱：两处 30°浅灰斜向水印（正常页面 12pt，默认不透明度 18%）、一处页脚（通常 8pt），以及渲染模式 3 的不可见文本、页面和文档 `/RecipientEmail` 属性。长地址会分行或缩小，完整地址仍可校验。可用 `--opacity 0.30` 调深斜向水印。

`verify` 检查清单、原文件名、哈希、页数及所有邮箱标记。正式使用前仍须在阅读器抽查实际长邮箱、横页、扫描页与首中末页。不要在登记哈希后编辑、压缩或另存成品。

支持常见文字、图片扫描、裁剪和 90°倍数旋转页面；拒绝加密、检测到的表单/签名、内嵌文件及作品集。脚本不清除原文已有的个人信息或旧水印。

## 邮件预演与 QQ 发送

编辑 `local/subject.txt` 的一行主题和 `local/body.txt` 的纯文本正文，不包含收件人名单。将所有命令中的 `你的QQ邮箱@qq.com` 替换为你自己的完整 ASCII QQ 邮箱地址：

```powershell
.\.venv\Scripts\python.exe delivery.py preview --batch local\batch_001 --from "你的QQ邮箱@qq.com" --subject-file local\subject.txt --body-file local\body.txt --out local\preview_001
```

预演不联网，输出每人一封 EML 和精简配对清单 `preview.csv`，不产生发送状态。检查每封一个 To、一个原文件名附件，Cc/Bcc 为空。EML 外层文件名使用内部编号，PDF 附件名称不变；预演文件也不能打包发给收件人。

先建立只含你自有测试邮箱的一行 `local/recipients.test.csv`，单独生成测试批次：

```powershell
.\.venv\Scripts\python.exe watermark.py generate --source "local\培训 资料.PDF" --recipients local\recipients.test.csv --expected 1 --out local\test_001
.\.venv\Scripts\python.exe watermark.py verify --batch local\test_001
```

在 QQ 网页端开启第三方 SMTP 服务并生成授权码，方法见 [QQ 官方说明](https://help.mail.qq.com/detail/106/985)。程序使用 `smtp.qq.com:465`、TLS 和证书校验，运行时隐藏输入授权码，不保存密码，不读取 Outlook 凭据。

**下面命令会真实发送。先对 `local\test_001` 运行并确认收件箱附件，再改为正式批次：**

```powershell
.\.venv\Scripts\python.exe delivery.py send-qq --batch local\test_001 --from "你的QQ邮箱@qq.com" --subject-file local\subject.txt --body-file local\body.txt
```

发送从映射表取配对，整批预检一次；每封提交前再读取并核验附件，用同一份已核验字节构造 MIME，检查 To、附件数量、原文件名和字节内容。禁止按目录顺序重新配对。

默认串行间隔 10 秒，可用 `--interval 20` 放慢，最小 3 秒；明确拒绝、限流或结果不明时停止，不自动重试。单封完整 MIME 默认限制 20 MiB，同时服从服务端 SIZE 上限；可用 `--max-message-mb` 调整工具限制。这些设置不代表 QQ 的额度保证，编码后邮件体积会增大。

## 日志与恢复

`mapping.csv` 保存批次、内部编号、邮箱、相对路径、原文件名、源/成品 SHA-256、页数、生成时间及结果。`send_log.csv` 逐条追加并落盘，保存这些配对字段，以及尝试编号、Message-ID、内容/邮件哈希、状态、错误与时间。日志不应删除或重置；进程锁阻止同批并发发送。

| 状态 | 处理 |
|---|---|
| `NOT_SENT` | 无提交记录，可发送 |
| `IN_PROGRESS` | 尝试已落盘，提交中；中断后转 `UNKNOWN` |
| `ACCEPTED` | SMTP 接受，自动跳过；仅记录 `submitted_at` |
| `SENT` | 人工核实服务端发信记录，自动跳过；不表示送达 |
| `FAILED` | 明确拒绝，处理原因后再人工决定重试 |
| `UNKNOWN` | 结果不明，先查 QQ 记录，禁止盲目重发 |

时间使用带时区的 ISO 8601。自动时间为 UTC，转北京时间加八小时。生成时间和邮件 Date 头都不是发送证明；SMTP 接受时间 `submitted_at` 也不是送达时间。`sent_at` 仅在另行核实服务端记录后填写。没有已读回执、追踪像素或 IMAP“已发送”副本保存功能。

查看状态或将中断项转为待核查（都不发邮件）：

```powershell
.\.venv\Scripts\python.exe delivery.py status --batch local\batch_001 --out local\status_001.csv
.\.venv\Scripts\python.exe delivery.py recover --batch local\batch_001
```

有 `FAILED` 或 `UNKNOWN` 时，批次暂停发送。人工核查确实未提交，才允许重试；原因必须写实际依据：

```powershell
.\.venv\Scripts\python.exe delivery.py resolve --batch local\batch_001 --id 001 --outcome NOT_SENT --reason "已核实本次未提交，填写实际核查依据"
```

确认已发送并取得服务端时间后，可用 `--outcome SENT --sent-at "2026-10-21T10:30:00+08:00"` 记录，不能照抄示例时间。不明则保留 `UNKNOWN`。处理完后重跑相同发送命令，已接受或已确认发送项会跳过；发件人、主题或正文改变时拒绝直接续发。找不到“已发送”副本不能推定未发送，网络故障下不承诺绝对一次发送。

## 溯源与验证

保全可疑文件原件，对副本分析；输入可为文件或目录：

```powershell
.\.venv\Scripts\python.exe watermark.py trace local\suspects --mapping local\batch_001\mapping.csv --out local\trace_001.csv
.\.venv\Scripts\python.exe selfcheck.py
```

溯源不会修改 PDF，报告须为新文件：`EXACT_FILE` 表示字节一致且无标记冲突；`MARKER_CANDIDATE` 为邮箱标记候选；`TEXT_CANDIDATE` 只有文字证据；`CONFLICT_REVIEW` 需要人工分析；`NO_MATCH` 不能排除来源。多批分别使用各自映射表；当前格式为邮箱批次 version 2，不能混用旧数字批次。

隐藏标记不抗截图、扫描或重打印，也可被删除或伪造；本工具不做 OCR。哈希和候选邮箱不能单独证明谁实施泄露，也不能阻止转发。保管好批次与日志的受控备份，文件和映射同时被改动时普通哈希不能认证真实性。

自检覆盖 70 份×6页、最长邮箱、中文/空格/大写扩展名、旋转/裁剪/扫描页、可见及隐藏标记、错配拦截、批量溯源、70封隔离 MIME、QQ TLS 模拟和中断恢复。自检全部使用合成资料和模拟传输，不发送真实邮件。
