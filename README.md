# 影视双语字幕制作与校对

`bilingual-subtitles` 是供 Codex 使用的字幕 skill，当前发布版本 **1.02**。制作英文在上、简体中文在下的双语字幕，支持单片、批量剧集、既有字幕修复与断点继续。制作前分轮展示字号、字体和位置的实际字幕效果图；翻译前建立独立来源台账，复查对白和画面文字的整条遗漏。

## 功能

- 以实际视频对白为依据，保留英文措辞、口语与人物语气；中文结合情境处理俚语、粗口、讽刺和幽默。
- 分别记录原文听辨、译义、时间轴和画面文字的核实状态，保留来源、证据与待确认时间点。
- 独立收集原片对白、歌词和画面文字候选，冻结来源 ID；逐项保存字幕映射、排除理由或待核状态，检查候选丢失、未扫描段及内容复核失效。
- 检查整体偏移、渐进漂移和剪辑差异；参考字幕使用前核对剧集与剪辑版本。
- 分别选择英文、中文和画面文字字体，检查字形覆盖；ASS 默认白字、细黑边，按视频分辨率调整。
- 通过 FFmpeg/libass 生成 5 档字号、最多 6 组实际字体、九宫格及底部抬高共 10 种位置的效果图；按组分轮选择，保存综合预览和完整样式指纹，也支持自定参数。
- ASS 对白/歌词按语言独立固定定位，同期对白保持各自槽位，避免换行或碰撞避让造成位置跳动。
- 翻译剧情相关书信、短信、文件、墙面文字等，保留完整可辨读内容及必要的全文附录。
- 跨剧集积累俚语与习语，按具体义项和语境检索，区分候选与已核条目，保留修订和引用历史。
- 使用稳定条目 ID、修订号和冲突检测继续任务，按变更范围重做相关阶段。
- 从同一字幕记录生成 UTF-8 BOM、CRLF 的 SRT 与 ASS，附来源、待确认说明和漏项检查报告；正式导出要求覆盖复核就绪，未完成可显式导出带 `.草稿` 标记的文件，再按白名单校验并打包 ZIP。

这些功能由 skill 工作流与辅助脚本配合完成。脚本处理任务记录、字体、知识库和导出；ASR、OCR、听辨、自动校时和视频渲染依赖当前环境中的可用工具。它不是安装后即可一键全自动精听、翻译和验收的软件。

## 安装

将仓库克隆到 Codex 的个人 skills 目录。目标目录已有同名 skill 时，先保留旧版，再按需要更新；不要覆盖任务数据或俚语库。

macOS / Linux：

```bash
git clone https://github.com/Zorkxxx/bilingual-subtitles.git "${CODEX_HOME:-$HOME/.codex}/skills/bilingual-subtitles"
```

Windows PowerShell（默认 Codex 目录）：

```powershell
git clone https://github.com/Zorkxxx/bilingual-subtitles.git "$env:USERPROFILE\.codex\skills\bilingual-subtitles"
```

若自行设置了 `CODEX_HOME`，使用其 `skills/bilingual-subtitles` 子目录。也可下载 Release 压缩包，将包含 `SKILL.md` 的目录放到该位置。

辅助脚本要求 **Python 3.10+**，只依赖 Python 标准库。FFmpeg、媒体识别、OCR 或字体渲染工具按实际任务配置，不随 skill 自动安装。字体能否使用须在目标环境检查。

## 使用

在 Codex 中调用 `$bilingual-subtitles`，提供可访问的视频路径和任务要求，例如：

```text
$bilingual-subtitles
请为这些视频制作英文在上、简体中文在下的字幕，交付同名 SRT、ASS 和 ZIP。
制作前先展示字号、字体和位置效果图，等我选择后按所选设置制作。
使用独立的任务输出目录，继续复用我指定的俚语库；未核实内容请列出时间点。
```

也可请求修复编码、更换字体、校对现有字幕、翻译剧情画面字或继续未完成任务。用户明确偏好优先；已指定完整参数或某预设时展示综合效果图并按实际指令执行，不重复询问同一偏好。仅有部分偏好时展示候选并询问缺项，推荐参数不会因未回复而自动成为选择。其它尚未调整的选择采用 [版本选择记录](references/decisions.md) 中的当前默认值，并随更新留档。

### 效果图与排版选择

先探测视频分辨率，再生成字号、字体、位置三组选项：

- 字号：XS 28/32、S 34/38、M 42/46（推荐）、L 50/54、XL 58/62；数值为英文/中文的 1080p 基准，按分辨率缩放。
- 字体：最多 6 组真实已安装、样例字形检查通过的组合；不足时少给，不下载或安装字体。英文、中文及画面文字可分别自定。
- 位置：上/中/下 × 左/居中/右的九宫格，另加底部抬高预设；默认上下/侧边距 60，抬高预设下边距 120，均为 1080p 基准。

每组保持相同背景和双语样例，按字号、字体、位置分轮先展示图再询问，不一次要求比较二十多个候选。优先使用真实视频截图；没有可用截图时，明确标注“示意背景，非实际片源”；没有对白记录时，文字标注为样式示例。样例通过字形检查不等于最终全部字幕已经检查，仍须核对实际文本与渲染结果。

[`scripts/preview.py`](scripts/preview.py) 调用最终导出使用的 ASS 排版函数生成 PNG，预览本身不修改字幕记录、不升级核实状态。选项 JSON 格式、分组可修改字段及完整流程见 [效果图与排版选择](references/style-selection.md)。例如从 skill 目录运行：

```text
python scripts/preview.py presets --records work/records/video.json --out work/style-options.json
python scripts/preview.py render --records work/records/video.json --options work/style-options.json --out work/style-preview --ffmpeg "<FFmpeg可执行文件路径>" --frame work/frame.png
```

显示图片并收到用户实际回复后，才调用选择命令。下面的选项 ID 应替换为本次选项中的 ID，确认文本使用用户回复原文：

```text
python scripts/preview.py select --records work/records/video.json --manifest work/style-preview/preview-manifest.json --size size-m --font font-01 --position pos-bottom-center --confirmation "<用户实际回复原文>"
```

选择命令组合三组设置、生成综合效果图并保存完整样式和图片 SHA-256。新任务处于待选择状态时阻止最终导出；已确认后，参数与确认指纹不符，或确认图片缺失/哈希改变，也会阻止导出。调整设置须重新预览并取得选择。候选字体的实际替代、缺字及渲染日志会记录；选中字体存在替代或缺字、字体解析日志无法核实时，不保存确认，先处理字体并重新提供候选。

新一批提供“沿用上次”和“调整”的效果图；同批已确认配置可复用。升级新增默认字段使旧指纹失配时，按已有明确设置生成新综合图并保存新指纹，不静默改变字号、字体或位置，也不重问已答参数。

ASS 中英文各有独立固定锚点，行数变化不推动另一语言；真正同期对白在整个显示期间保持同一槽位。保留自动换行，仍须检查长句、行容量和画面遮挡。SRT 的字体、位置和自动换行由播放器决定，不能保证与 ASS 相同的固定布局。已有字幕文件不会随 skill 更新自动改变；需要从记录重新导出，并重新进行画面验收。

### 来源台账、漏项检查与草稿

[`scripts/coverage.py`](scripts/coverage.py) 检查独立来源候选与字幕的去向，以及原片实际复查范围。清单在翻译前从原片音频、画面和独立参考收集，不能由成品 cues/SRT 反推。ASR、VAD、OCR 只补候选，未识别到或没有字幕不等于无内容。

每个候选保留稳定 ID，使用 freeze 固定期待 ID 和收集依据；后续只扩展基准，不删除旧 ID。候选必须映射到真实 included、类型正确且时段相交的字幕，或有据排除，或保留待核。已冻结候选消失、新候选未登记、字幕没有来源、原片音频/画面存在未扫描段，都会阻止正式交付。台账内容、字幕或片源改变时，旧复核摘要失效。

旧记录缺少 coverage 时先初始化；已有清单不重新初始化。参考导入只建立候选并登记 ID，不自动翻译、映射或标为已审听。例如：

```text
python scripts/coverage.py init --records work/records/video.json
python scripts/coverage.py import-srt --records work/records/video.json --srt work/reference.srt --source-id ref-a
python scripts/coverage.py freeze --records work/records/video.json --evidence "<独立候选收集的来源和范围>"
python scripts/coverage.py audit work/records/video.json --report work/coverage-report.json
```

按原片解决候选去向并记录实际音频/画面复查，再保存内容复核。重点回听、回看片头片尾、弱声、重叠说话、切片边界、快画面字和复杂长文本；不能用时段重合、条数或汉字数量证明译义完整。

```text
python scripts/coverage.py review --records work/records/video.json --evidence "<实际原片复核范围和依据>"
python scripts/coverage.py audit work/records/video.json
python scripts/subtitles.py export work/records/video.json --out work/delivery
```

默认正式导出要求 coverage.ready；review 不会自动消除待核候选或补造扫描证据。未完成时可保存明确草稿：

```text
python scripts/subtitles.py export work/records/video.json --out work/draft-delivery --draft
```

草稿文件加 `.草稿`，包含漏项检查报告，manifest 标明 `export_mode: draft`；不能称为全片最终已核。草稿只豁免覆盖就绪，结构错误、排版选择或图片/参数指纹失配仍阻断。默认覆盖范围为全片音频与画面；局部或关闭某一模态须有明确任务依据，并在交付中说明范围外未核。

该工具检查已登记候选与已记录复查范围，不能自动发现尚未进入清单的全部内容，也不能证明真实听过/看过原片。缺少音频或画面访问时保留可完成内容与待核时间点，不伪造“全片完成”。完整约定见 [来源台账与漏项复核](references/coverage.md)。

开始前可从 skill 目录运行：

```text
python scripts/project.py preflight
```

任务与数据、脚本参数及导出流程分别见以下文档：

- [Skill 入口](SKILL.md)
- [任务与数据约定](references/data.md)
- [制作与审校流程](references/workflow.md)
- [来源台账与漏项复核](references/coverage.md)
- [漏项辅助脚本](scripts/coverage.py)
- [字体与画面文字](references/visual.md)
- [效果图与排版选择](references/style-selection.md)
- [效果图辅助脚本](scripts/preview.py)
- [俚语库](references/slang.md)
- [导出与验收](references/export.md)
- [版本选择记录](references/decisions.md)

原视频、音频、字幕记录、人物术语表、缓存和持久俚语库保存在 skill 目录之外。俚语库从空库起步，不附带某部剧的字幕或私人词条；指定同一持久库路径即可在后续任务检索。候选不会因为出现次数多或曾经采用就自动变成已核条目。

## 第一版验证范围

首版已有项目流程、导出、知识库和字体输入检查，并完成短视频独立试用。发布前检查 UTF-8、文档链接及冻结 skill 文件的一致性。

独立试用使用的演示视频**没有音轨**，因此仅验证可执行的记录、导出、字体与画面流程，不构成对白精听或音画同步验证。格式校验、模型一致或参考数量均不能证明台词、译文已经按原视频核实。每次实际字幕任务仍须按用户要求完成听辨、译义、校时和真实画面验收，并说明未完成范围。

本仓库不包含视频、剧集字幕、私人知识库、测试原始日志或环境凭据。

## 1.01 验证范围

本次完成 12 项原有布局回归、7 项选择状态回归，以及真实 FFmpeg CLI 闭环的 71 项检查。覆盖固定语言锚点、换行、同期槽位生命周期、毫秒边界取整、分辨率缩放、实际事件计数、完整 SRT/ASS/ZIP 交付，及效果图比较、选择保存、样式/图片指纹和阻断流程。在 1080p、720p 的 libass 实际渲染中检查了另一语言增行与同期对白进出时的定位稳定性。

这些验证针对布局、选择流程和数据完整性，不构成具体影片的对白精听、译义或音画同步验证；实际字幕任务仍需按片源验收。

## 版本更新

当前发布版本为 **1.02**，项目、字幕记录与俚语库 schema 均为 1，新增 coverage 子结构 schema 1。历史发布 1.01 合并此前本地 1.0.1、1.1.0 草稿的改动，历史记录保留；见 [CHANGELOG](CHANGELOG.md) 与 [version.json](version.json)。旧记录可检查结构和保存明确草稿，正式导出须从原片补建独立清单并复核；已有 ASS 不会自动修复。

后续调整记录旧值、新值、原因、适用版本和复核范围；未调整选项保持原记录，不随升级悄悄重置。持久库独立于 skill，升级时不会随安装文件覆写。

## 1.02 验证范围

完成 25 项覆盖台账检查、6 项正式/草稿导出集成检查和 12 项既有布局回归；实际字体枚举、字形检查及 FFmpeg 预览流程完成 117 项断言。覆盖来源冻结与丢失、去向映射、扫描缺口、复核摘要失效、交付阻断、草稿标记，以及扩展选项渲染、组合选择和旧偏好保留。

验证针对脚本和合成字幕场景，未对用户影片做实际审听或全片画面验收。覆盖就绪不等于自动发现全片所有遗漏；每次任务仍按原片完成核对并报告实际范围。


