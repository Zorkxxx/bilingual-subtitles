# 来源台账与漏项复核

整条对白或画面文字没有进入 cues 时，格式检查无从发现。翻译前建立独立来源清单；初版内部保存后，自主逐段复听、复看原片寻找遗漏，核对每项去向，修补后再复查，不等待用户验收第一稿。台账来自原片实际内容和独立参考/识别候选，不能从本次成品 cues、SRT 或 ASS 反推；删 cue、过滤或合并不能同时删掉来源候选以消除缺口。

## 来源、去向与实际复查

在 records.coverage.items 保存对白、歌词、画面文字候选，建立清单后用 freeze 固定 expected_item_ids 并保存 inventory_evidence。参考导入自动登记源文件全部条目的 ID 及依据；后续新增候选扩展基准，不能删除旧期待 ID 或从当前剩余 items 重建较短基准，以删除候选掩盖漏项。原片逐段音频/画面复查是最终依据；ASR、VAD、OCR、参考字幕只补候选、时段和出处。参考复制同一字幕不构成独立证据；识别没有输出或某时段没有字幕不等于无内容，冻结空清单也不能证明无内容。

scope 内每个已知候选必须保留下列去向之一：

- mapped：cue_ids 指向真实 included cue，kind 正确且与来源原时段有正时长交集，evidence 保存整项内容与字幕去向已核对的依据。拆分、合并可多对多链接，保存每段对应内容。
- excluded：cue_ids 为空，明确 reason 和非空 evidence，解释该候选为何不属于实际片源或为何按任务省略；不得以“已有字幕没这一条”作为依据。
- pending：仍待听辨、识别、匹配或决定，保留时段及候选，不能默默删掉。

每个 include=false 的 cue 另外保存非空 exclusion_reason/exclusion_evidence，来源 item 仍须有明确去向；scope 内每条 included cue 也须有独立来源映射。映射和时段检查只证明链接可用；一封长信只译首段、合并后丢掉一人发言、链接到同时间的另一句话，仍需语义复核。不能用条数、汉字数量或时间重合证明内容完整。

scans 分别记录原片音频与画面实际复查区间。reviewed 必须是 audio_review 或 visual_review，含实际方法、范围与证据；ASR、VAD、OCR、抽帧数量或参考覆盖率不能自动填成 reviewed。长片按块持续保存，回查拼接边界；即使确认没有应制字幕的内容，也要保存该段的实际复查依据。

疑似空白须回听/回看，重点片头片尾、弱声和背景对白、同期说话、切片边缘、快速画面文字变化、复杂文件和长信。画面字候选见 [visual.md](visual.md)。初版后的实际复查记录轮次、字幕修订与范围；修补后更新对应核实证据，复查受影响区间和接缝，不能只重跑 audit。缺少可听音频或原片画面时，内部保留可完成内容与准确未核范围，明确最终未完成；不默认发送草稿，也不能补造扫描记录或证据。

## coverage 子结构

records 主 schema 保持 1，coverage.schema_version 也为 1；下例仅展示未完成台账，不能当作已核成果：

```json
{
  "schema_version": 1,
  "expected_item_ids": [],
  "inventory_evidence": [],
  "items": [{
    "id": "source-audio-0007", "kind": "dialogue", "start_ms": 1200, "end_ms": 3400,
    "source": "video-a:audio-track-1 / 原片指纹与来源时间",
    "text": "<候选原文或描述，不能补造>", "status": "pending", "cue_ids": [],
    "evidence": ["尚待原片复听；候选来源另行记录"]
  }],
  "scans": [{
    "id": "scan-audio-0001", "kind": "audio", "start_ms": 0, "end_ms": 60000,
    "status": "pending", "method": "audio_review", "evidence": []
  }],
  "review": {"status": "pending", "evidence": []}
}
```

items 字段为稳定 id、kind（dialogue/lyric/onscreen）、start_ms/end_ms、非空 source 字符串、可选 text、status（pending/mapped/excluded）、cue_ids、可选 reason、evidence 数组。source 可记录路径、指纹、参考序号或原片音轨/镜头出处，不能只有“已生成字幕”。source item ID 不随导出编号改变。

expected_item_ids 是独立保存的数组，元素为唯一、非空的来源 ID 字符串；inventory_evidence 是候选实际收集依据数组。初始化二者为空表示尚未冻结，并非没有应译内容。freeze 首次记录现有 items ID，后续仅扩展，已冻结 ID 缺失时拒绝操作；没有冻结依据、source_item_lost（旧 ID 消失）或 source_item_unregistered（新项目未登记）都不能 ready。出现旧 ID 缺失须找回来源项和去向，不能重新盖一次 review 使漏项消失。

scans 字段为 id、kind（audio/visual）、start_ms/end_ms、status（pending/reviewed）、method（audio_review/visual_review）和 evidence 数组；类型、方法和实际证据须对应。所有候选与扫描时间都在实际全片 0..duration_ms 内，scope 不允许把越界来源合理化。

scope 可选，格式为 `{start_ms, end_ms, audio, visual, reason}`；audio/visual 为布尔值，不能同时 false。缺省为全片双模态。局部范围或关闭某模态须保存明确任务依据 reason，不可为使检查通过擅自缩小范围。范围外候选仍保留，报告计入 items_out_of_scope，并说明 scope 外未核；局部 ready 不代表全片无遗漏。

review 为 `{status, basis_sha256, evidence}`，pending 时可省略 basis_sha256。完成实际内容复核后由工具生成 complete 与当前 basis_sha256。coverage_digest 绑定片源稳定字段、scope、items/scans、expected_item_ids/inventory_evidence，以及 cues 的 ID、类型、内容、时段、include/exclusion_reason/exclusion_evidence 和画面字全文/显示模式等；不包含 style、revision/history 或 review 自身。改变片源、来源去向或字幕内容后旧复核失效；仅改字体/排版不会伪造或升级内容复核。

## 初始化、导入、核对和确认

```text
python scripts/coverage.py init --records records.json
python scripts/coverage.py import-srt --records records.json --srt reference.srt --source-id ref-a --offset-ms 0
python scripts/coverage.py freeze --records records.json --evidence <独立候选收集的来源和范围>
python scripts/coverage.py audit records.json --report coverage-report.json
python scripts/coverage.py review --records records.json --evidence <实际原片复核的范围和依据>
```

执行顺序为 init → 独立来源收集 → freeze（参考导入自动登记）→ 制作内部初版并建立去向 → 自主逐段复听复看 → 修补、扩展来源基准并再次复查 → audit → 复核结束后 review → audit 与逐条核实条件通过 → 最终 export。发现新问题即回到修补和复查，不能把首版交付给用户作为继续条件。

init 建空 items/scans、空冻结基准和 pending review，不自动从 cues 推断来源或标已复查。import-srt 将参考 SRT 加为候选，记录 source-id、偏移、源路径与哈希，并登记全部来源 ID；不替代原片复听、翻译或最终映射。不要导入本次成品字幕制造独立清单。items/scans 按实际工作编辑并保留历史，新增 items 后扩展冻结基准；coverage.review 在初版后自主复听复看及修补结束再调用，无需新增 postdraft 子结构。

audit 不编辑 records，输出中文报告，`--report` 保存 JSON；退出码 ready 为 0、未完成为 1、结构异常为 2。报告 summary 包含 scope、full_video_dual_review、items_in_scope/items_out_of_scope、items_pending/items_mapped/items_excluded、expected_items、included_cues_in_scope/mapped_cues_in_scope 和 reviewed_ms（audio/visual）；正文保留完整候选、冻结依据、扫描记录、缺口及 review。计数不是内容完整性的证明。

先按原片消除缺口，再调用 review：该命令仅在除了 review 自身 pending/stale 外其它覆盖条件通过时保存 complete、内容摘要与非空证据；遇实际缺项返回 1 且不写 records，不会把 pending 候选、ASR/OCR 扫描或未知范围自动升为完成。scope 内未映射 cue（coverage_cue_untracked）、未扫描段（coverage_scan_gap）及复核失效（coverage_review_stale）都需要对应处理，不能仅重跑命令使其消失。

init/import-srt/freeze/review 使用同目录原子写与 `.lock`，写入成功后 revision+1 并追加 history；重复导入相同参考可幂等返回，保留原有校准和状态。source-id 仅用英文字母、数字、下划线、点和连字符；参考文件内容改变时使用新的 source-id，不覆盖旧来源。并行修改先检查最新记录，不靠覆盖消除冲突。review 成功后再运行 audit，确认 ready 绑定当前内容。

默认 `subtitles.py export` 和 qa/delivery complete 除 ready 外，还要求本次 scope 内启用模态的 included cues 适用 verification 为 verified、无未解决 unresolved，范围内 pending 已结。对白/歌词要求 english/translation/timing，画面字要求四维 verified；有据排除项不当作待翻译 cue，scope 外未核记录保留并报告。scope 内未扫描段、未映射候选或复核失效均不能最终交付。结构 lint 报告中的 final_review 列表及 warnings 提示逐条问题，不改变真实核实状态。

旧记录可 lint，并在内部断点需要时用显式 `--draft` 保存工作文件，但不能自动补造台账；未经用户主动要求，不交付草稿文件或字幕包。草稿仍须结构、排版选择和预览哈希有效，不据此标完成。具体规则见 [export.md](export.md)。ready 不证明证据真实、全部原片内容已发现或译义正确，不能代替实际复听复看；能力缺口下保留内部成果并明确最终未完成。
