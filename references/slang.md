# 持久俚语与习语库

将知识库放在用户选定的持久目录，通过 `--library` 指定；不要保存在 skill 安装目录，也不要把任务临时文件当作唯一副本。首版使用 Python 3.10+ 标准库脚本 `scripts/slang.py`，无网络或模型依赖。

## 数据与来源

`library.json` 包含 `schema_version: 1`、库级 `revision`、`entries` 和 `history`。每次新增或修订递增库版本；每个条目也有独立 `revision`。历史保留修订原因、UTC 时间和修改前后完整快照，因此来源的版本信息随修订一起保留。可在证据对象中附加 `source_version`、`source_fingerprint`、`audio_track`、`reviewer` 等字段，脚本不会丢弃这些字段。

条目字段：

| 字段 | 类型与用途 |
| --- | --- |
| `id` | 稳定字符串；省略时新增自动生成，修订不能改动 |
| `expression`, `sense` | 必填字符串：表达及这一条的特定含义 |
| `variants` | 字符串数组，词形或口语变体 |
| `region`, `era`, `register`, `relationship` | 地域、时代、语气强度与人物关系；省略默认为空字符串 |
| `scope`, `work` | `general` 可跨作品；`work` 限定作品且必须填写 `work`；通用条目的 `work` 为空 |
| `translations` | 对象数组，每项 `text` 为译法，`conditions` 为适用条件字符串 |
| `evidence` | 对象数组，每项必填 `source`；可填 `episode`, `time_ms`, `cue_id`, `note` 及来源版本字段 |
| `status` | `candidate` / `verified` / `deprecated` |
| `usage_refs` | 对象数组，记录使用处，可填记录文件、稳定字幕 ID、来源版本等 |
| `verification` | 升级为已核实时必须提供的核实记录，见下文 |

同一个表达可以有多条含义或适用作品。仅当归一化后的 `expression + sense + scope + work` 完全一致时拒绝重复；报告已有 ID，不合并或覆盖。不要只靠词形相同合并含义，也不要把多次出现当作已核实的依据。

## 操作

以下路径是示意，换成实际文件；条目和补丁 JSON 使用 UTF-8（可带 BOM）。命令成功输出 JSON，失败返回非零状态并说明原因。

```text
python scripts/slang.py init --library <持久目录>
python scripts/slang.py add --library <持久目录> --entry <条目.json>
python scripts/slang.py search --library <持久目录> --query "完整对白或表达" --work "作品名" --region "UK"
python scripts/slang.py search --library <持久目录> --query "表达" --include-candidates
python scripts/slang.py revise --library <持久目录> --id <稳定ID> --patch <补丁.json> --reason "修订依据"
python scripts/slang.py impacts --library <持久目录> --id <稳定ID> --records <字幕记录.json> --records <另一集记录.json>
```

`add` 始终生成候选，即使提交 `status: verified` 也不会自动核实。新增文件不要填托管字段 `revision`。`revise` 是浅层字段替换，更新数组或对象应提交完整新值；`id`、`revision`、创建与更新时间不能由补丁修改。可加 `_expected_revision` 防止将基于旧版本的补丁应用到新版本。

核实或按用户纠正更新后，使用显式补丁：

```json
{
  "_expected_revision": 1,
  "status": "verified",
  "verification": {
    "basis": "human_correction",
    "source": "用户纠正记录的实际文件或会话标识",
    "note": "该义项、语气及译法已针对所列语境确认"
  },
  "evidence": [
    {
      "source": "用户纠正记录的实际文件或会话标识",
      "note": "记录实际纠正内容及适用范围"
    }
  ]
}
```

`basis` 可为 `audio_check`、`reference_check` 或 `human_correction`，`source` 必须出现在证据数组中，`note` 不能为空。填入真实依据后再升级；脚本只校验记录结构，不证明核实内容真实。核实只适用于这一义项和记录的语境。已核实条目的表达、义项、语境、译法或证据发生变更，且补丁没有新的显式核实记录时，会退回候选并标注旧核实已失效。修改使用记录不会自动影响核实状态。历史保留旧核实信息。

## 调用与纠正影响

默认仅检索已核实的通用条目以及 `--work` 匹配的作品条目；候选必须显式开启并带未核实标志，停用条目不参与检索。跨作品不调用作品专属条目。字面短语及变体优先，统一大小写、英式引号和空白，检查词边界；部分词汇匹配排在后面。地域只提供排序提示，不据此保证适用。首版不提供语义向量或自动语境判断：使用结果前仍需结合人物关系、时代、语气和当前对白判断。

字幕统一记录的 `cues[].slang_ids` 保存所用稳定 ID。条目修订后，`impacts` 列出这些文件中受影响的字幕 ID，不自动重写旧字幕；回到原视频语境重审后再导出。首版不会检出缺少链接的历史用法，也不会根据条目自动追踪外部文件，因此记录链接及输入版本应在翻译时保存。

## 写入边界与限制

写入使用 `library.lock` 的独占创建和临时文件原子替换；主库与历史一起提交。遇到并发锁立即失败并明确提示重试。进程异常留下锁时，先确认没有写入进程，再手动移除；不要自动抢锁。格式、状态、重复 ID、未知主版本、无效 UTF-8、控制字符、替换字符和明显损坏标记会阻止写入。有效 Unicode 并不保证语义正确，也无法识别所有乱码；仍需阅读及来源检查。

备份或迁移整个持久库目录；主版本变化需要明确迁移，不能忽略未知版本继续写入。首版保持本地 JSON，并不训练模型，也不在 skill 更新时删除或覆盖知识库。
