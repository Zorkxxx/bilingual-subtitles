# 效果图与字幕排版选择

本流程用于未来的字幕制作、修复或排版任务，不在用户仅更新 skill 时发起字幕设置选择。先展示真实渲染的效果图，再取得选择；用户确认的配置用于最终 ASS。SRT 仍交付内容兼容版，但不能编码所选字体、字号或精确位置，不能承诺与效果图外观一致。

## 制作前展示三组选择

媒体探测后取得视频分辨率和可用真实帧，枚举实际可用字体，检查样例字形覆盖及 ASS 渲染能力。选一帧和同一双语样例作为所有候选共同基准；正式字幕尚未制作时，只准备这条排版样例并说明用途，不把它当成已审校的全片成果。效果图必须由实际 ASS 渲染器生成，不能用 AI 造图或手绘字幕替代。

提供字号、字体、位置三个独立组，按组分轮先展示图再询问，不把二十多个候选一次塞进同一个问题。每个选项给清楚的 ID、标签、具体值和效果图；每组只改变所比较的项目，其余设置保持一致，避免样例或背景变化干扰比较。用户可选择预设或直接自定参数。

| 组 | 可用选项 | 显示与配置约定 |
| --- | --- | --- |
| 字号 size | XS：英文 28 / 中文 32；S：34 / 38；M：42 / 46（推荐）；L：50 / 54；XL：58 / 62 | 1080p 基准，按视频高度缩放；允许用户分别自定英文、中文字号。 |
| 字体 font | 最多 6 个真实可用的英文/中文字体组合 | 真实枚举且样例字形覆盖通过；不足时少给，不虚构、不自动下载或安装。 |
| 位置 position | top/middle/bottom × left/center/right 九宫格，另有 bottom 偏高 | bottom-center 推荐；偏高预设 margin_bottom=120，其余上下/侧边距默认 60，均为 1080p 基准。 |

字号选项也可按视频实际尺寸与阅读条件调整；推荐值不是自动选择。英文/中文字体、字号可分别指定，边距也可自定，必要时画面文字独立配置。`dialogue_position` 选择 top/middle/bottom，`dialogue_align` 选择 left/center/right，`margin_side` 控制左右安全边距。效果图保留固定语言带及相应水平锚点，英文固定底边、中文固定顶边；middle 使用固定双语带整体垂直居中，不能按每句行数改变位置。

先显示字号组效果图并询问，再显示字体组并询问，最后显示位置组并询问；位置多时可按纵向区域再比较横向对齐。可提前渲染全部候选，但按组展示和收集选择。允许直接回复选项 ID 或自定值，只询问未答项；记录实际答复及适用范围。等待选择期间可继续片源、音轨和参考素材检查，不能正式制作字幕或导出。没有回复、经过等待或会话结束均不授权默认值。

## 已知偏好、批次和恢复

- 用户已明确给出全部参数或指定某预设时，先生成并展示该配置的综合效果图，再按已授权设置执行；不重复询问同一偏好。用户明确说“用默认”即授权推荐默认，仍展示真实综合效果图并保存该答复。
- 只明确了部分参数时，用已答参数生成候选，只询问缺项。自定值生成新选项图，不把最相近预设当成用户的值。
- 新一批任务提供带综合效果图的“沿用上次（推荐）”和“调整”；用户选择调整时再展示三组候选。不能把上一作品的偏好无条件推广到新一批。
- 同批已确认连续集可在用户确认范围内复用；媒体尺寸与字体环境变化时重新渲染核查。恢复已有任务仅核对 style_sha256、综合效果图哈希及确认范围；一致时不重复询问。

当用户组合选择三组配置后，生成 `selected-preview.png`，显示最终综合效果并保存确认记录。该图必须使用最终有效 style；选完字体后可能改变实际行高或换行，要检查语言带容量和溢出。需要改变已选值才能解决问题时，展示调整后的效果图并取得对应新选择，不能静默缩字号、换字体或挪位置。

## 预览与选择工具

`scripts/preview.py` 复用正式 ASS 的排版规则，并通过 FFmpeg 的实际 ASS 渲染器生成 PNG；不负责下载字体、修改台词或证明全片视觉验收通过。预览样例在副本中去除 coverage，排版比较不依赖内容覆盖已完成，也不改变原台账或复核状态。任务目录保存预览、清单和用户确认，不写入 skill 目录。

```text
python scripts/preview.py presets --records records.json --out options.json
python scripts/preview.py render --records records.json --options options.json --out previews --ffmpeg <ffmpeg路径> --frame <实际视频帧PNG>
python scripts/preview.py select --records records.json --manifest previews/preview-manifest.json --size size-m --font font-01 --position pos-bottom-center --confirmation <用户实际答复> --ffmpeg <ffmpeg路径>
```

presets 的 `--out` 是明确 JSON 文件路径，输出 5 个字号、最多 6 个实际可用字体组合和 10 个位置。字号 ID 为 size-xs/size-s/size-m/size-l/size-xl，字体为 font-01..font-06（按当前环境可能更少），九宫格位置为 pos-top-left 等，偏高为 pos-bottom-raised。工具从实际字体库存取得候选并运行样例字形检查，同目录保存 `options.font-checks.json`；不能承诺每台机器都能给出同样字体或六组。样例覆盖不等于最终全部字幕字形覆盖，仍按 visual.md 检查实际文本与渲染日志。

`options.json` 是 JSON 数组，每项为 `{id, label, group, style}`，group 为 size/font/position，style 是当前 style 的非空补丁，仅改变该组的字段。render 仍要求三组至少各一个选项；已确定组可在后续候选清单中保留单一选项，但单一候选不自动代表用户确认。用户已指定完整配置时每组只含对应值，用其实际指令作为 confirmation，不再问一次。`--frame` 接收实际视频帧图片路径；工具不核对它是否来自片源，调用者须留存源时间点。只有缺少可用真实帧时才省略此参数并明确标注抽象背景。

使用 presets 得到可运行候选数组；自定值可按下列片段增加选项。此片段仅显示部分组，不能原样运行，完整清单须保留实际可用的 font 组，以及 size/font/position 各至少一个选项。

```json
[
  {"id": "size-custom", "label": "自定：英文40 / 中文44", "group": "size", "style": {"font_size_en": 40, "font_size_zh": 44}},
  {"id": "pos-custom", "label": "顶部靠左，边距80", "group": "position", "style": {"dialogue_position": "top", "dialogue_align": "left", "margin_top": 80, "margin_side": 80}}
]
```

render 为每个选项输出 PNG 和 `preview-manifest.json`，记录帧、样例、渲染条件、有效配置和图片哈希。没有对白记录时工具使用带“样式示例”标记的双语文本，有记录时使用一条现有对白；样例不能被算作全片已核成果。select 按用户实际答复选择 size/font/position ID，合并配置并生成综合效果图，更新 records.style、style_selection、revision/history；不能提前替用户执行选择。工具不改原始台词、时码或核验状态。

确认记录含 `status: confirmed`、`confirmation`、`option_ids`、`style_sha256`、`preview_image`、`preview_sha256`。style_sha256 使用 `effective_style`/`style_digest` 计算完整有效、未缩放 style 的规范化摘要，preview_sha256 绑定实际综合效果图；导出前核对当前配置和图片。手改字段后摘要失配须重新预览、记录对应确认。工具拒绝覆盖旧预览，重新选择使用新目录保留历史；已有记录和预览清单变化时先重新生成清单，不强行重用过期效果图。

升级新增默认字段可能使旧 style_sha256 失配。按原来明确选定的字体、字号、位置和边距生成新的综合图，并保存新的完整指纹；不重复询问已答参数，不静默改变已选外观。仅排版变化不升级 coverage 内容复核。

选择后同步 project.config.style，仅同步到用户明确授权的任务范围；对受影响单片标记 visual/qa/delivery 失效，重新渲染与交付。保留原始台词、翻译、核实状态和证据。旧字幕与记录不自动覆盖。

## 无法展示真实效果图

先探测可用 FFmpeg 及 ASS 滤镜，或当前环境是否有其它真实 ASS 渲染器，记录具体能力和失败原因。没有可用渲染器时不能伪称已展示效果图；告知缺少预览并询问用户是否明确跳过预览。只有用户明确同意才将 `style_selection.status` 记为 `waived`，保存 `confirmation`（用户实际答复）、匹配当前配置的 `style_sha256`、跳过原因与所选配置后继续，不能默默采用默认或标记渲染已通过。

没有可用真实视频帧、但实际 ASS 渲染器可用时，可将同一双语样例真正渲染到抽象背景并标为“排版示意，未核真实画面遮挡”；各选项仍共用同一背景。获得视频后补做实际画面检查。所有预览都只是选择依据，最终仍按 [visual.md](visual.md) 与 [export.md](export.md) 检查长短句、多行切换、同期对白进出和开中末连续播放。
