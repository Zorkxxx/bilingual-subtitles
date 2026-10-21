# 任务、字幕记录与断点数据

Python 3.10+，所附脚本只依赖标准库。听辨/OCR/媒体渲染按环境选择，脚本不自动安装模型、获取 API 密钥或发送视频。以下命令从 skill 目录执行；其它目录时改成脚本绝对路径。任务目录应是独立输出目录。

```text
python scripts/project.py preflight
python scripts/project.py init --work-dir <任务目录> --video <视频1> --video <视频2> --library <持久俚语库目录>
python scripts/project.py status --work-dir <任务目录>
python scripts/project.py probe --work-dir <任务目录> --video-id <初始化输出的ID> --ffprobe <ffprobe路径>
python scripts/project.py checkpoint --work-dir <任务目录> --video-id <ID> --stage transcription --status draft --evidence <实际完成范围> --artifact <识别JSON> --parameters <参数JSON>
python scripts/project.py invalidate --work-dir <任务目录> --video-id <ID> --change style --reason <更换字体>
```

`init` 建 project.json 与每片 records/<稳定视频ID>.json，保留视频 basename；同目录冲突时拒绝初始化，不覆写旧任务。未传 --library 时记录默认持久目录 `$CODEX_HOME/data/bilingual-subtitles/slang`，CODEX_HOME 未设置则为用户 `.codex/data/bilingual-subtitles/slang`；首次需要积累时用 slang.py init 建空库，已有库直接读取不重建。默认片源指纹是完整路径+大小+修改时间，用作变化探测而非内容等同证明；`--hash` 改为全文件 SHA-256。初始时长、尺寸为 null，必须由实际媒体探测补齐。`probe` 记录轨道属性；未探测片源的字段可通过可靠媒体工具手工填入，写清依据。

## project.json

- schema_version=1；skill_version 独立于数据 schema，task_id 稳定，revision/history 留下操作。
- videos 每项包含 id、path、records 相对路径、fingerprint 和 stages。
- config 保存语言、编码、已确认的字体/字号/底边距预设、画面字模式、持久 library_path 与 decisions_revision。config.style 的复用范围以用户确认为准。
- glossary 保存本作品的名字、称呼和术语，不自动写入全局俚语库。
- stages 为 inspect/references/transcription/translation/timing/visual/qa/delivery，各阶段 pending/in_progress/draft/complete/stale。complete 必须写实际证据，不能靠状态声称人工精听。

draft 是内部工作状态，不是默认交付节点。第一版及各轮修补内部保存，不增加等待用户验收第一稿的阶段。qa/delivery complete 要求 coverage.ready，以及本次 scope 内启用模态的 included cues 适用核实维度 verified、无未解决 unresolved，pending 窗口已结；字段状态和程序通过不能代替真实复听复看。

`status` 检查片源和阶段产物是否变化，以及 qa/delivery 是否针对旧 records revision；不自动刷新指纹或让失效成果变成有效。`checkpoint` 保存产物全文件哈希与工具参数。以 `invalidate` 管理依赖；直接手改 records 或更换工具参数时须主动标记相关阶段，第一版不自动监控文件或推断全部语义变化。

## 单片 records.json

```json
{
  "schema_version": 1,
  "revision": 0,
  "video": {"id": "video-a", "path": "<输入视频路径>", "basename": "Example.S01E01", "duration_ms": 60000, "width": 1920, "height": 1080},
  "review_scope": "说明实际听辨/翻译/时间/画面检查范围。",
  "sources": [{"id": "ref-a", "title": "来源名称", "url": "<实际URL或本地出处>", "version_match": "开中末锚点已核/尚未核", "note": "版本差异"}],
  "style": {"font_en": "Arial", "font_zh": "Microsoft YaHei", "font_onscreen": "Microsoft YaHei", "font_size_en": 42, "font_size_zh": 46, "font_size_onscreen": 40, "outline": 1.5, "margin_bottom": 60, "margin_top": 60, "margin_side": 60, "dialogue_position": "bottom", "dialogue_align": "center", "dialogue_en_lines": 2, "dialogue_zh_lines": 2, "dialogue_line_height": 1.35, "dialogue_gap": 8},
  "style_selection": {"status": "pending"},
  "coverage": {"schema_version": 1, "items": [], "expected_item_ids": [], "inventory_evidence": [], "scans": [], "review": {"status": "pending", "evidence": []}},
  "cues": [{
    "id": "audio-0007", "kind": "dialogue", "start_ms": 1200, "end_ms": 3400,
    "en": "<实际听辨原文>", "zh": "<对应译文>", "include": true,
    "verification": {
      "english": {"status": "draft", "evidence": ["参考候选，尚未实听"]},
      "translation": {"status": "draft", "evidence": []},
      "timing": {"status": "pending", "evidence": []},
      "visual": {"status": "not_applicable", "evidence": []}
    },
    "unresolved": [], "slang_ids": [], "slang_refs": []
  }],
  "pending": [{"id": "window-0001", "start_ms": 4000, "end_ms": 5500, "kind": "audio", "note": "听辨待核窗口", "cue_ids": []}],
  "history": []
}
```

此结构示例只说明字段，不能把示例状态或文本当成已完成成果。cue ID 不随重新编号、译法或时码变化；SRT 序号仅在导出时生成。include=false 的条目须保存非空 `exclusion_reason` 和 `exclusion_evidence`，保留来源与依据，不能静默过滤。kind 可为 dialogue/onscreen/lyric；对白 en 可承载实际外语原文，不能由中文回译伪造英语。

四项 verification 各自为 pending/draft/verified/stale/not_applicable；verified 必须有非空真实证据，写明检查者/方式、位置与来源。对白/歌词最终要求 english/translation/timing 已 verified，visual 不适用；画面文字四维均要求 verified。scope 外或有依据排除的条目继续保留真实状态和说明，不据此冒充全片已核。

unresolved 非空表示仍有未解决问题；pending 列表表示尚未结案窗口，不新增 resolved 字段。实际修复后从 pending 移出，并在 history 保留解决依据；不能为使导出通过而删除仍待核的记录。pending 的 audio/dialogue/lyric 属于音频，visual/onscreen/ocr 属于画面，其余类型结合 cue_ids 判断；本次 scope 内启用模态的未结问题阻止最终导出。听辨证据和模型输出分别标识，画面字同时保存 onscreen（见 visual.md）。

范围判断采用正时长交集；单点 pending 按 scope 的 `[start_ms,end_ms)` 判断，scope 结束等于全片结尾时该影片终点也计入。未知模态且无关联 cue 时保守归入启用模态。scope 外记录和有依据排除项不误拦范围内最终交付，但保留在说明/待确认报告中。

俚语调用时 slang_ids 保存稳定库条目 ID；slang_refs 保存 `{id, revision, library_revision, sense}` 使修订关联当时版本。库 usage_refs 保存 records 路径、cue ID 和片源版本。

`coverage` 是独立来源台账，嵌入 records.json，子 schema_version 为 1；items 保存候选和去向，expected_item_ids/inventory_evidence 保存冻结基准与收集依据，scans 保存音频/画面实际复查范围，review 保存内容摘要。详见 [coverage.md](coverage.md)。freeze/参考导入只扩展基准，缺收集依据、旧 ID 消失或新项目未登记时不能最终导出。scope 缺省全片双模态，局部或关闭某模态须有任务依据。空清单或冻结空清单不能证明原片无内容，pending 窗口也不能替代来源清单。

初版后自主逐段复听复看，修补再复查，记录轮次、字幕修订及实际范围；结束后再保存现有 coverage.review，不新增 postdraft 结构。旧记录缺少台账时可内部保存和 lint，但不能从成品 cues 补造来源或证据。能力缺口下内部保留成果并说明最终未完成；显式草稿仅内部断点或用户主动要求时使用，不默认发送链接或字幕包。

画面文字 `onscreen` 可含 full_en/full_zh、display_mode（缺省 chinese）、appendix、source_region、region。source_region 保存实际原文字区供核对，region 指字幕显示区域供 ASS 定位，两者均为归一化 `[x,y,w,h]`；具体判断见 visual.md。屏幕只显示中文时仍保存对应英文原文。

style 的字号、描边、边距和 dialogue_gap 以 1080p 为基准，按 video.height/1080 缩放。新任务固定 dialogue_position=bottom、dialogue_align=center，位置只选择 margin_bottom：40/60（推荐）/90/120/160 或自定值。margin_bottom 是固定中文预留语言带底边到画面底部的距离；字体留白和行容量会影响实际字形下沿，不承诺文字边缘恰好相距该像素数。旧已确认记录的其他定位字段继续按原值兼容，不新增方位菜单，也不擅改明确偏好。

dialogue_en_lines/dialogue_zh_lines 为 1–8 整数容量，默认各 2；dialogue_line_height 为 1–3 倍率，默认 1.35；三者不缩放。dialogue_gap 默认 8。语言带与静态槽位不随每句行数改变位置，也不自动缩小字号；显式行数超容量由 lint 报错，真实自动换行、字体行高和溢出按 visual.md 渲染验收。

`style_selection.status` 为 pending/confirmed/waived。confirmed 保存 `confirmation`（用户实际答复）、`option_ids`（各组选项 ID）、`style_sha256`（完整有效、未缩放 style 的规范化摘要）、`preview_image`（综合效果图路径）与 `preview_sha256`（图片哈希）；预览清单另保存实际样例/帧与渲染条件。waived 仅在用户明确同意跳过预览时保存 confirmation 与 style_sha256，并记录原因。具体流程见 [style-selection.md](style-selection.md)。pending 不能最终导出；confirmed/waived 的 style_sha256 与当前有效 style 不符也不能导出。恢复任务核对摘要与图片哈希；摘要一致只证明配置和文件未变，不证明字体渲染、位置或内容已经验收。

config.style 是新片预设，导出读取每片 records.style。选择完成后将配置同步到 project.config.style，仅更新用户确认适用的单片或同一批次；其它任务不自动改变。同批连续集复用确认时保存该确认来源及当前有效 style_sha256。修改全任务预设时同步预期使用该预设的每片，保留 revision/history，并标记 visual/qa/delivery 失效；不修改台词、时码、原有核实状态或证据。

## 并行审校与修改

```text
python scripts/project.py apply-patch --records <单片JSON> --patch <修订JSON>
```

修订 JSON 是 `{base_revision, reason, edits, add}`；每条 edits 是 `{cue_id, expected: {待改字段: 旧值}, set: {待改字段: 新值}}`。add 是完整新 cue 数组。基准版本过旧、重复 ID 或任何 expected 不符时全部拒绝，不部分覆盖。先重读最新文件，解决冲突后再提交；不要在失败后强行去掉 expected。

内容修改会将受影响 verification 标记 stale，保留旧证据与原因；核对后用独立仅修改 verification 的 patch 记录新的状态和依据。更改 en 影响英文、译义、时间核对；更改 zh 影响译义；更改时码影响时间核对；更改画面文本信息影响 visual/translation。排版通过 invalidate --change style，仅重做渲染与交付。局部可用 --cue-id 限定范围，媒体变化应全片。

写入使用同目录临时文件和原子替换、独占锁；锁冲突立即报告。单个文件原子，但 project/records 不是跨文件事务；系统异常后先用 status 检查，较旧的阶段不能直接升级，重新确认受影响阶段即可。不要自动删除未确认失效的锁。

## 迁移与版本

1.03 的 records 与 coverage 子 schema 均保持 1，不猜测或降级未来 schema。缺 style_selection 的旧任务仍按排版流程处理已知偏好和缺项；缺 coverage 时先内部保留旧结构，从原片补建独立来源并在初版后自主复听复看。最终导出同时满足覆盖、逐条核实及排版条件；显式草稿仅作内部断点或应用户主动要求提供，不能默认交付或伪装完成。不自动覆盖旧记录、字幕、审校依据或俚语库；版本更新与历史见 decisions.md。
