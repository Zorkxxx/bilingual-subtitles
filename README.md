# 影视双语字幕制作与校对

`bilingual-subtitles` 是供 Codex 使用的字幕 skill，当前版本 **1.0.0**。默认制作英文在上、简体中文在下的双语字幕，支持单片、批量剧集、既有字幕修复与断点继续。

## 功能

- 以实际视频对白为依据，保留英文措辞、口语与人物语气；中文结合情境处理俚语、粗口、讽刺和幽默。
- 分别记录原文听辨、译义、时间轴和画面文字的核实状态，保留来源、证据与待确认时间点。
- 检查整体偏移、渐进漂移和剪辑差异；参考字幕使用前核对剧集与剪辑版本。
- 分别选择英文、中文和画面文字字体，检查字形覆盖；ASS 默认白字、细黑边，按视频分辨率调整。
- 翻译剧情相关书信、短信、文件、墙面文字等，保留完整可辨读内容及必要的全文附录。
- 跨剧集积累俚语与习语，按具体义项和语境检索，区分候选与已核条目，保留修订和引用历史。
- 使用稳定条目 ID、修订号和冲突检测继续任务，按变更范围重做相关阶段。
- 从同一字幕记录生成同名 UTF-8 BOM、CRLF 的 SRT 与 ASS，附来源和待确认说明，按白名单校验并打包 ZIP。

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
使用独立的任务输出目录，继续复用我指定的俚语库；未核实内容请列出时间点。
```

也可请求修复编码、更换字体、校对现有字幕、翻译剧情画面字或继续未完成任务。用户明确偏好优先；尚未调整的选择采用 [版本选择记录](references/decisions.md) 中的当前默认值，并随更新留档。

开始前可从 skill 目录运行：

```text
python scripts/project.py preflight
```

任务与数据、脚本参数及导出流程分别见以下文档：

- [Skill 入口](SKILL.md)
- [任务与数据约定](references/data.md)
- [制作与审校流程](references/workflow.md)
- [字体与画面文字](references/visual.md)
- [俚语库](references/slang.md)
- [导出与验收](references/export.md)
- [版本选择记录](references/decisions.md)

原视频、音频、字幕记录、人物术语表、缓存和持久俚语库保存在 skill 目录之外。俚语库从空库起步，不附带某部剧的字幕或私人词条；指定同一持久库路径即可在后续任务检索。候选不会因为出现次数多或曾经采用就自动变成已核条目。

## 第一版验证范围

首版已有项目流程、导出、知识库和字体输入检查，并完成短视频独立试用。发布前检查 UTF-8、文档链接及冻结 skill 文件的一致性。

独立试用使用的演示视频**没有音轨**，因此仅验证可执行的记录、导出、字体与画面流程，不构成对白精听或音画同步验证。格式校验、模型一致或参考数量均不能证明台词、译文已经按原视频核实。每次实际字幕任务仍须按用户要求完成听辨、译义、校时和真实画面验收，并说明未完成范围。

本仓库不包含视频、剧集字幕、私人知识库、测试原始日志或环境凭据。

## 版本更新

当前规则版本为 1.0.0，项目、字幕记录与俚语库 schema 均为 1。见 [CHANGELOG](CHANGELOG.md) 与 [version.json](version.json)。后续调整记录旧值、新值、原因、适用版本和复核范围；未调整选项保持原记录，不随升级悄悄重置。持久库独立于 skill，升级时不会随安装文件覆写。
