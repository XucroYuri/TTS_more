# 语义剧本分析与人工校对工作台设计

## 1. 目标

把现有“保存剧本后立即提取并生成正式台词”的流程，升级为一个以原文为事实源、由大模型提出候选、由用户最终确认的语义分析工作台。

系统需要从格式混乱的剧本中识别：

- 角色或说话者；
- 台词，包括没有引号的台词；
- 原文中明确存在的情感证据；
- 可以由模型推断但原文未直接表达的标准化情感。

分析页面采用独立双栏布局：左侧显示并高亮原文，右侧显示结构化结果。用户可以在左侧自由框选遗漏内容并修正结果。只有点击“确认分析”后，已接受且已分配角色的台词才进入现有 TTS 工作台。

## 2. 第一性原则

1. 原文剧本是唯一事实源。任何正式台词必须能够精确映射回一个不可变原文版本。
2. 大模型输出是候选，不是生产数据。模型负责语义判断，程序负责原文定位和不变量校验。
3. 人工确认后的语义版本才是生产事实。草稿自动保存，但不自动覆盖正式台词，也不触发合成。
4. 正式 ScriptLine 是已确认语义结果的投影，不承担完整语义标注模型的职责。
5. 局部不确定性不应摧毁有效结果。除供应商、传输或整体结构契约失败外，分析允许部分成功。

## 3. 范围

### 3.1 第一版包含

- 粘贴纯文本剧本；
- 上传 .txt 和 .md 文件；
- 角色、情感证据、台词三类原文标注；
- 无引号台词的模型语义识别；
- 左侧自由范围选择和人工补标；
- 角色重分配、标注重分类、拒绝和删除；
- 角色规范名称与受控别名；
- 低置信度和待确认筛选；
- 草稿自动保存；
- 部分结果确认并投影为正式 TTS 台词；
- 持久错误显示、结构化诊断信息和服务端滚动日志；
- 新旧项目兼容。

### 3.2 第一版不包含

- Word、PDF、OCR 输入；
- 多人实时协作；
- 在语义草稿里直接改写原文；
- 自动确认、自动进入配音或自动开始合成；
- 模型评测后台和通用提示词管理平台；
- 对旧项目进行破坏性批量迁移；
- 删除旧解析接口。

## 4. 用户流程与页面结构

### 4.1 显式阶段门

用户流程固定为：

AI 分析草稿 → 用户检查和修正 → 确认分析 → 生成正式角色与台词 → 进入现有 TTS 工作台

上传或粘贴后先保存新的不可变 ScriptRevision，再创建分析任务。修改原文会创建新 ScriptRevision 和新草稿，不把旧坐标静默迁移到新原文。

### 4.2 独立双栏分析页面

左侧是原文校对区：

- 按角色、情感证据、台词显示三种稳定颜色，并同时显示文本标签或图例，不能只依赖颜色传达含义；
- 原文先按所有 SourceSpan 边界切成不重叠 DOM 片段；同一片段可以携带多个 annotation id；
- 重叠时台词使用蓝色浅背景，角色使用紫色下划线，情感证据使用琥珀色波浪下划线；点击重叠片段时弹出关联标注列表，不依赖覆盖顺序猜测命中项；
- 点击高亮时选中右侧关联项；
- 用户在原文上自由框选后，可新建角色、情感证据或台词标注；
- 新建台词时可选择已有角色、创建角色或暂存为角色待确认；
- 错误范围通过重新框选替换，第一版不实现复杂拖拽手柄；
- 原文在分析阶段只读。

右侧是结构化结果区：

- 按原文顺序展示发言；
- 每条发言显示角色、别名、台词、情感证据、标准化情感、强度、模型推断标记、置信度和审核状态；
- 点击结果会滚动并定位左侧原文；
- 提供全部、待确认、低置信度筛选；
- 提供角色合并、拆分和重分配入口；
- 分析警告与结构化结果共存，不遮挡已成功内容。

### 4.3 确认规则

确认前显示导入摘要：

- 已接受且已经分配规范角色的发言会导入；
- 待确认、已拒绝或缺少角色的发言保留在语义版本中，但本次不生成 ScriptLine；
- 用户可以确认部分结果；
- 确认请求必须携带客户端生成的 idempotency_key。首次成功后草稿写入 confirmed_revision_id、confirmed_parse_revision_id 和 confirm_idempotency_key，并变为只读；
- 同一草稿使用相同 idempotency_key 重试时直接返回原 SemanticRevision 和 ParseRevision，不再检查旧 expected_version；不同 key 再次确认已确认草稿时返回 409 draft_confirmed；
- 已接受角色优先绑定名称一致的现有 ProjectCharacter；没有匹配时按规范名称创建新的项目角色，别名不创建重复角色；
- ScriptLine.text 取 dialogue SourceSpan.text，ScriptLine.character_id 取已绑定或新建的项目角色，ScriptLine.note 取标准化情感，ScriptLine.language 取发言语言；
- 每个新 ScriptLine 保存可选的 semantic_revision_id 和 utterance_id，支持从配音工作台追溯到原文；旧 ScriptLine 没有这些字段仍然有效。

为兼容现有工作台，确认时还创建一个不可变兼容 ParseRevision：

- parse_revision_id 为 semantic-{semantic_revision_id}；
- provider 标记为 semantic-confirmed；
- line.id 使用稳定 utterance id；
- active_parse_revision_id 指向该兼容版本；
- 现有 line_uid 继续由 parse_revision_id 和 line.id 组成；
- 同一确认幂等重试复用相同版本和行，不追加副本。

## 5. 领域模型

### 5.1 SourceSpan

SourceSpan 表示可验证的原文范围：

- source_revision_id；
- start_utf16，含起点；
- end_utf16，不含终点；
- text，原文精确片段；
- source_sha256，用于阻止把旧坐标应用到不同原文。

坐标统一使用 UTF-16 code unit，使浏览器 Selection API 与服务端契约一致。服务端提供统一转换函数，把 Python 字符索引与 UTF-16 坐标互转，并再次验证转换后的 Python 切片与 text 完全一致。

原文按以下规则保存和哈希：

- .txt 或 .md 文件必须按 UTF-8 解码，可移除一个开头 BOM；
- 移除 BOM 后不得转换 CRLF、Unicode normalization、内部空白或标点；
- 粘贴文本按浏览器提交的精确字符串保存；
- source_sha256 是精确存储字符串编码为 UTF-8 后的 SHA-256；
- ScriptRevision 新增可选 source_filename、source_media_type 和 source_sha256；旧版本读取时按现有 source_markdown 计算哈希。

### 5.2 SemanticAnnotation

每个语义标注包含：

- id；
- kind：speaker、emotion_evidence 或 dialogue；
- span；
- origin：ai 或 human；
- confidence：AI 标注为 0 到 1，人工标注可为空；
- status：pending、accepted 或 rejected；
- created_at 和 updated_at。

人工新建标注默认 accepted。模型标注只有在原文范围唯一、confidence 不低于 0.8 且没有要求待确认的不确定性代码时才进入 accepted，否则进入 pending。阈值由服务端常量统一管理。模型不能直接产生最终不可修改结果。

### 5.3 CharacterCandidate

角色候选包含：

- id；
- canonical_name；
- aliases；
- supporting_annotation_ids；
- project_character_id，可为空；
- confidence，AI 候选为 0 到 1，人工候选可为空；
- status：pending、accepted 或 rejected；
- origin：ai 或 human。

AI 角色候选只有在名称证据唯一、confidence 不低于 0.8 且没有角色不确定性代码时才默认 accepted，否则为 pending。状态可在未确认草稿中由 pending 转为 accepted 或 rejected，rejected 也可由人工恢复为 pending 或 accepted；确认后不可修改。代词和群体泛称不会自动创建为角色。确定性的空格、大小写和标点差异可以自动归一化。诸葛九九与九九之类别名只在模型明确提出且上下文一致时作为一个可见候选关系；仅凭字符串包含关系不自动合并。用户能够接受、拆分或重新合并。

### 5.4 Utterance

一次发言包含：

- id；
- dialogue_annotation_id；
- speaker_annotation_id，可为空；
- character_candidate_id，可为空；
- emotion_evidence_annotation_ids，可为空数组；
- normalized_emotion，可为空；
- emotion_intensity，可为空；
- emotion_origin：source_grounded、inferred 或 none；
- language；
- confidence；
- status。

一条可导入发言必须有已接受的 dialogue 标注和已接受的规范角色。speaker_annotation_id 只表示原文中的角色证据；人工重分配角色时可以保留该证据，也可以清空。情感证据不是导入前置条件；没有原文证据的情感必须明确标记为 inferred。

合法状态组合固定为：

- utterance 为 accepted 时，dialogue annotation 和 CharacterCandidate 必须都是 accepted；
- dialogue annotation 为 pending 时，utterance 只能是 pending；dialogue 被 rejected 时，关联 utterance 自动 rejected；
- CharacterCandidate 为 pending 或 rejected 时，关联 utterance 不能 accepted；
- emotion evidence 为 pending 或 rejected 只会从投影中排除该证据，不阻止台词导入；
- 删除 dialogue annotation 级联删除关联 utterance；删除 speaker annotation 会清空 speaker_annotation_id；删除 emotion evidence 会从所有关联数组移除；
- PATCH 后若存在悬挂引用或非法状态组合，整个 PATCH 返回 422 且不写入。

### 5.5 SemanticAnalysisDraft、SemanticRevision 与 AnalysisRun

SemanticAnalysisDraft 绑定：

- project_id；
- source_revision_id；
- 当前 version；
- annotations、characters、utterances、unresolved_candidates；
- warnings；
- provider、model、prompt_version 和 contract_version；
- created_at、updated_at。

SemanticRevision 是确认时生成的不可变快照。后续修改要从该版本派生新草稿。

AnalysisRun 保存 queued、running、completed、failed 或 interrupted 状态。completed 结果进一步标明 complete 或 partial。创建任务时立即持久化空草稿和运行记录，分析完成后以原子写替换草稿内容。运行记录和错误记录必须持久化，以便刷新页面后恢复。

只有 run 进入 completed、failed 或 interrupted 等终态后草稿才允许 PATCH；queued 和 running 期间页面只读。终态空草稿也允许用户完全通过人工标注构建结果。confirm 只要求任务已终止且草稿满足状态不变量。partial 是 completed 的 quality 值，不是独立运行状态。这样后台 worker 与人工自动保存不会同时写同一草稿。

## 6. 大模型契约

### 6.1 模型职责

提示词要求模型：

- 判断文字是否是角色能够说出口的台词，而不是仅依据引号、括号、冒号或 Markdown；
- 识别无引号台词；
- 区分台词、角色名、情感证据、动作、场景、镜头、音效和普通叙述；
- 原样返回台词，不得润色、补字、删字、改标点或改变内部空白；
- 无法确定说话者时返回空角色和固定不确定性代码，不得编造；
- 把原文情感证据和标准化情感推断分开；
- 用固定枚举和置信度表达不确定性，不返回可见的冗长推理过程；
- 输出规范角色和候选别名关系。

格式符号是弱证据。带引号通常增加台词置信度，但没有引号不降低为非台词硬规则。

normalized_emotion 使用以下第一版枚举：

- neutral、happy、excited、surprised、sad、angry、fearful；
- disgusted、anxious、calm、serious、gentle、confused、other。

没有可靠情感时使用空值而不是猜测。emotion_intensity 在存在情感时为 0 到 1 的浮点数。other 必须同时带一个不超过 32 个字符的 custom_emotion，其他枚举不得带 custom_emotion。

uncertainty_codes 是可多选且去重的固定数组：

- speaker_unknown；
- speaker_ambiguous；
- dialogue_ambiguous；
- emotion_inferred；
- emotion_ambiguous；
- source_anchor_ambiguous。

speaker_unknown、speaker_ambiguous 或 dialogue_ambiguous 会使已定位发言进入 pending。source_anchor_ambiguous 使候选进入 unresolved_candidates，不创建发言。emotion_inferred 和 emotion_ambiguous 不阻止台词 accepted，但必须在页面显示。

### 6.2 模型输出

模型输出共享结构化契约，至少包括：

- character_candidates：规范名、别名、可选原文证据；
- utterance_candidates：原样 dialogue_excerpt、speaker_name、可选 speaker_evidence_excerpt、可选 emotion_evidence_excerpts、normalized_emotion、custom_emotion、emotion_intensity、emotion_origin、language、confidence、uncertainty_codes、anchor_before、anchor_after 和 occurrence_index；
- chunk metadata：chunk_id、start_utf16、end_utf16、overlap_before 和 overlap_after。

模型可以提供候选位置，但服务端不得直接信任模型坐标。服务端使用原文片段、出现次序和邻近上下文重新定位。

anchor_before 和 anchor_after 是最多 32 个 Unicode 字符的精确邻近原文，occurrence_index 是 dialogue_excerpt 在当前 chunk 中按出现顺序计算的零基序号。定位算法固定为：

1. 在 chunk 原文中查找 dialogue_excerpt 的所有精确出现；
2. 唯一出现直接定位；
3. 多次出现时依次用 anchor_before 的后缀、anchor_after 的前缀和 occurrence_index 过滤；
4. 最终唯一时定位；仍有多个候选时不创建 SourceSpan、annotation 或 utterance，只在 unresolved_candidates 和 warnings 中保留脱敏诊断，代码为 source_anchor_ambiguous；
5. 模型给出的 offset 只用于诊断，永不覆盖上述结果。

### 6.3 长文本

长剧本按段落和供应商上下文限制切分，保留少量重叠区和全局偏移。每个分段独立分析，随后按全局原文位置去重和排序：

- 重叠区中相同原文范围和类型的标注合并；
- 角色候选按受控别名规则合并；
- 单个分段失败产生分段警告，其他分段仍形成草稿；
- 所有分段都结构失败时，任务才以整体结构错误失败。

## 7. 原文定位与校验

服务端校验器是事实边界：

- 每条已接受台词必须是当前 ScriptRevision 中的连续精确子串；
- 台词按照原文位置排序；
- 重复台词依据前后文锚点和出现次序定位；
- 相同范围的重复候选去重；
- 无法定位的候选不生成高亮或正式台词，只保存安全的警告摘要；
- source_excerpt 对说话者或情感的推断不再是硬失败；
- 明显引号台词未被模型覆盖时生成召回警告或待确认提示，不再使整篇失败；
- 不要求最少台词数。零台词是有效空结果，页面提示用户人工框选；
- 单条候选失败不清空其他有效候选。

台词逐字匹配和原文顺序是不可放宽的不变量。说话者和情感属于可人工修正的语义判断。

## 8. API 与任务状态

新增语义分析 API：

- POST /api/projects/{project_id}/analysis-runs：以 source_revision_id 创建持久分析任务，返回 202、run_id 和 draft_id；
- GET /api/analysis-runs/{run_id}：读取任务状态、进度、警告和错误；
- GET /api/analysis-drafts/{draft_id}：读取草稿；
- PATCH /api/analysis-drafts/{draft_id}：携带 expected_version 和 operations 数组，原子应用一批受控编辑；
- POST /api/analysis-drafts/{draft_id}/confirm：携带 expected_version 和 idempotency_key，幂等创建 SemanticRevision、兼容 ParseRevision 和正式 ScriptLine。

operations 只允许以下命令：

- create_annotation、replace_annotation、delete_annotation、set_annotation_status；
- upsert_character、set_character_status、merge_characters、split_alias；
- create_utterance、update_utterance、delete_utterance、set_utterance_status；
- dismiss_warning。

整批命令在同一 expected_version 上校验，任一命令非法则全批失败且不增加版本。服务端应用后统一执行 SourceSpan、引用完整性和状态真值表校验。

第一版新增专用 SemanticAnalysisExecutor，使用 FastAPI lifespan 管理的受控 ThreadPoolExecutor，默认最多两个并发任务，并持久化运行记录；不复用 TTS GenerationJobManager，也不引入外部队列。服务启动时把仍为 queued 或 running 的旧任务标记 interrupted。前端轮询任务状态；任务进入 completed、failed 或 interrupted 后必须停止加载动画。

草稿 PATCH 和确认接口使用乐观并发。版本不一致返回 409，服务端不会覆盖较新的人工修改。语义存储使用项目级进程内锁保护“检查版本—写文件—更新项目”的临界区，并继续使用临时文件替换保证单文件原子写。

## 9. 错误处理与可观察性

### 9.1 错误分类

- 400：空文本、文件类型不支持或输入超过配置上限；
- 409：原文或草稿版本冲突；
- 422：同步 PATCH 或 confirm 契约无效；异步分析中表示模型响应整体不符合结构契约，或所有分段均完全无法解析；
- 502：供应商鉴权、网络或上游响应失败；
- 504：供应商超时。

POST analysis-runs 只对同步输入检查直接返回 4xx；一旦返回 202，后续供应商或模型失败不会改变创建请求的 HTTP 响应，而是写入 AnalysisRun.error.http_status。GET 任务状态返回 200 和持久运行记录，前端依据 error.http_status 显示 422、502 或 504。局部台词定位失败、角色不确定和情感证据不足属于 warnings，不是整体失败。

每个错误包含：

- code；
- http_status；
- stage；
- message；
- retryable；
- run_id；
- trace_id；
- occurred_at；
- 安全的 details。

### 9.2 前端持续显示

错误使用项目和分析任务范围内的持久错误面板，不使用自动消失 Toast：

- 页面刷新和工作区重渲染后从 AnalysisRun 恢复；
- 切换右侧结果不会清除；
- 只有用户主动关闭，或同一项目同一原文版本的新分析成功后才清除；
- 失败时立即停止加载动画；
- 提供复制诊断信息，内容不含密钥和完整剧本。

关闭动作只隐藏当前 run_id 的错误，前端把已关闭 run_id 持久化在本机 localStorage；AnalysisRun 错误记录仍保留在诊断历史中。新的运行失败必须重新显示。

### 9.3 服务端日志

新增独立 semantic-analysis.jsonl 结构化滚动日志，目录可配置并默认位于运行时数据目录，不纳入 Git。默认单文件 10 MB，保留 5 个文件；现有 parse 日志的文件名、1 MB 上限和测试预期保持不变。

记录字段：

- timestamp、level、event；
- project_id、source_revision_id、draft_id、run_id、trace_id；
- provider、model、stage、duration_ms；
- source_char_count 和 source_sha256；
- chunk_index、chunk_count；
- candidate_count、accepted_count、warning_count；
- error_code、http_status、异常类型和脱敏异常摘要。

默认禁止记录原始异常堆栈、API Key、Authorization、完整原文、完整提示词和供应商完整响应，因为上游异常可能携带敏感正文。日志测试必须覆盖脱敏。

## 10. 后端组件边界

为避免继续扩大 backend/app/main.py：

- semantic_models.py：语义领域模型和 API 请求响应模型；
- semantic_analysis.py：提示词契约、分段、模型候选归一化、原文定位和草稿构建；
- semantic_storage.py：分析任务、草稿和语义版本的文件存储；
- semantic_executor.py：受控后台线程池、任务状态转换和启动恢复；
- semantic_routes.py：分析任务、草稿修改和确认路由；
- semantic_logging.py：独立结构化事件、滚动策略和脱敏；现有 parse_logging.py 保持兼容。

现有 parser provider 配置、URL/SSRF 防护、密钥解析和低层 HTTP 传输继续复用。新增独立 SemanticProvider 契约及结构化响应解码器；不得调用旧 OpenAICompatibleProvider.parse、AnthropicProvider.parse 或旧 ScriptParseVerifier，因为它们只产出 TTS 行、丢弃 source evidence，并把零台词视为质量错误。旧 parser.py 接口保留兼容，但不参与新语义状态机。

现有 backend/app/models.py 的 ScriptLine 必须真实增加可选 semantic_revision_id 和 utterance_id；仅在序列化时附加未知字段不可接受。确认时的角色投影固定为：

1. CharacterCandidate 已绑定有效 project_character_id 时复用该角色；
2. 否则在当前项目角色中按规范化名称查找唯一精确匹配并复用；
3. 没有匹配时创建新的 ProjectCharacter，id 从 CharacterCandidate.id 稳定派生；
4. 出现多个冲突匹配时角色保持 pending，该发言不导入；
5. 新角色沿用现有角色库精确匹配能力，但不自动猜测声音或覆盖人工绑定。

## 11. 前端组件边界

新增 frontend/src/features/script-analysis：

- ScriptAnalysisWorkspace：阶段容器和总体状态；
- SourceAnnotationPane：只读原文渲染、范围选择、高亮和定位；
- AnalysisResultsPane：发言、角色和警告列表；
- SelectionAnnotationMenu：把选区创建为三类标注；
- CharacterAliasEditor：规范角色和别名管理；
- useAnalysisDraft：任务轮询、乐观版本、自动保存和错误恢复；
- selectionOffsets：DOM Range 与 UTF-16 SourceSpan 的纯函数转换。

frontend/src/App.tsx 只负责在分析阶段和现有 TTS 工作台之间切换，并在确认后刷新正式台词。所有新增用户可见文字必须加入中英文 i18n。

## 12. 文件输入

- 支持粘贴文本以及 .txt、.md；
- 前端读取文本内容，去除 UTF-8 BOM，但不改变换行、内部空白或标点；
- 不支持类型给出明确 400 级用户错误；
- 默认输入上限为 Python len 所计的 200,000 个 Unicode code point，可通过后端配置修改；前端只做提前提示，后端是最终边界；
- 上传文件名作为 ScriptRevision 元数据，不参与语义判断。

## 13. 测试策略

### 13.1 后端单元测试

- UTF-16 与 Python 字符索引互转，包括中文、代理对 Emoji 和换行；
- 引号台词和无引号台词精确定位；
- 重复台词按照上下文和出现次序定位；
- 一字符或标点变化不能成为 accepted 台词；
- 说话者 source_excerpt 不一致只产生警告；
- 引号覆盖不足只产生召回警告；
- 零台词形成有效空结果；
- 诸葛九九与九九受控合并，王与老王不因包含关系自动合并；
- 分段重叠去重和部分分段失败；
- 草稿版本冲突和确认幂等；
- 日志字段完整且敏感内容被脱敏。

### 13.2 提示词契约回归

使用固定供应商响应，不访问真实 API。回归样本包含：

- 用户提供的混合格式剧本代表片段；
- 明确样例“胶布（惊喜，大喊）：真的！真的有加速效果！跑得好快！！这下大笨蛙追不上了”，其中角色、情感证据和无引号台词分别定位；
- 明确样例“真的！真的有加速效果！跑得好快！！”被引号包裹时，台词 SourceSpan 只覆盖可朗读原文，不把包裹引号加入 ScriptLine.text；
- 多条无引号台词；
- Markdown 动作和叙述；
- 诸葛九九与九九别名；
- 情感有原文证据和纯推断两种情况。

测试断言契约、定位、警告和最终草稿，不断言真实模型的随机输出。

### 13.3 API 集成测试

- 创建任务、轮询完成、读取草稿；
- 部分成功仍返回可编辑结果；
- PATCH 自动保存和 409；
- confirm 仅投影已接受且有角色的发言；
- 重复 confirm 不重复创建；
- failed 和 interrupted 任务可在刷新后读取；
- 422 错误结构保持稳定。

### 13.4 前端测试

- .txt 和 .md 读取以及 BOM 处理；
- 自由框选中文与 Emoji 后坐标准确；
- 左右双向定位；
- 新增、拒绝、删除、重分配和别名修正；
- 自动保存冲突提示；
- 全部、待确认、低置信度筛选；
- 422 停止加载、持续显示并在刷新后恢复；
- 部分确认摘要和进入 TTS 工作台。

### 13.5 端到端与人工测试

使用模拟模型完成：

粘贴或上传 → 分析 → 人工框选补标 → 确认部分结果 → 验证 ScriptLine → 进入现有配音工作台

真实模型仅做人工冒烟测试，不进入 CI。

## 14. 兼容与迁移

- 保留现有 ScriptRevision、ParseRevision、Project、Character 和 ScriptLine 数据；
- 新存储采用附加文件和可选字段，不要求批量改写旧 JSON；
- 旧项目没有语义版本时直接进入现有配音工作台；
- 新的“分析剧本”入口使用语义分析流程；
- 旧解析 API 暂时保留，前端新流程不调用；
- 不修改 ComfyUI、声音绑定、生成队列和音频历史行为；
- 现有工作树里的解析器修复、错误日志和 422 展示改动需要在实现时保留并整合，不能覆盖或丢失。

## 15. 验收标准

1. 已接受台词都是当前原文的连续精确子串，且按原文顺序输出。
2. 测试样本中的引号和无引号台词都能进入可检查草稿。
3. 角色、情感证据和台词都能由用户自由框选补充。
4. 情感原文证据与模型推断标签在数据和页面上明确分离。
5. 诸葛九九与九九形成可检查、可拆分的别名关系。
6. 单项或单分段失败不会清空其他有效结果。
7. 确认只导入已接受且已分配角色的台词，重复确认不会重复创建。
8. 422 会停止加载并持续显示，刷新后仍包含 trace_id。
9. 服务端日志能够按 trace_id 定位阶段，且不包含 API Key 或完整剧本。
10. 旧项目无需破坏性迁移即可正常打开、配音和查看历史。

## 16. 实施批次

整体第一版保持本规格范围，但按三个可独立验证的批次实施，前一批通过后才进入后一批：

### 批次 A：领域内核与兼容投影

1. 添加领域模型、UTF-16 范围工具、状态真值表和测试；
2. 实现语义草稿、运行记录、语义版本存储和项目级锁；
3. 实现受控 PATCH commands、确认幂等和兼容 ParseRevision 投影；
4. 验证旧项目、旧 ParseRevision 和旧 TTS 工作台不回归。

### 批次 B：模型分析与可观察性

1. 实现独立 SemanticProvider 契约、单段候选定位和别名候选；
2. 实现专用后台执行器、异步错误持久化和 semantic-analysis.jsonl；
3. 实现长文本分段、重叠去重、部分分段成功和召回警告；
4. 完成提示词契约、供应商模拟和 API 集成测试。

### 批次 C：双栏人工校对页面

1. 实现前端类型、API、轮询、自动保存和持久错误；
2. 实现独立双栏页面、重叠高亮、双向定位和自由框选；
3. 实现角色重分配、别名管理、筛选、部分确认摘要和文件输入；
4. 完成组件、端到端和完整回归测试；
5. 重启本地服务供人工验收。

## 17. 批准记录

用户于 2026-08-31 逐节确认：

- 角色指剧本中的角色或说话者；
- 使用自由框选标注；
- 允许部分成功并要求人工确认；
- 情感分为原文证据与模型推断；
- 第一版输入为粘贴、.txt 和 .md；
- 草稿与正式 TTS 数据之间设置明确确认门；
- 页面采用独立双栏分析阶段；
- 架构、交互、模型校验、API、错误日志、测试和迁移设计均已确认。
