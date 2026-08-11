---
promptGroup: ux-mixed-generator
fragmentId: ux-mixed-kernel
order: 0
promptVersion: ux-mixed-prompt/0.6
protocolVersion: tersedsl-nested-2-ux-mixed/0.3
contractVersion: hybrid-body-contract/0.5
---

<!-- prompt:start -->
第五接口 UX 混合模式覆盖规则（优先级高于旧 advancedComposition 说明）：

1. 这是第二层生成。第一层已经只确定 Theme 与业务高级组件范围；不得重新选择整卡模板、计算整卡置信度或输出整卡参数映射。
2. 禁止 card@1。根必须直接使用一个批准的布局高级组件；服务端在布局 Lowering 后统一补可信 CardFrame，模型不得生成另一层整卡壳。
3. 布局组件可省略配置；需要覆盖默认重排时，只能把 Contract 声明的一个闭合 config 对象放在第一个 child 前，不得生成 Schema 外字段或值。Contract 的 businessChildren 数量不包含 Action。业务 child 可由批准的局部 Template 与标准 Column/Row/Text/Image/Progress 混排；所有 Action 必须是布局根连续的末尾直接 children，禁止放进 Column/Row/Stack/List/Template。除 `ActionMatrixLayout` 可按 Contract 使用 2～4 个不重复控制 Action 外，其它布局最多一个 Action。若布局只允许一个 business child，先用 Column 组合多个内容单元，再把该 Column 作为唯一 business child，随后放置 Action。
4. 业务高级组件是语义职责，不是端侧节点。每个已选业务高级组件必须从本次 `requiredLocalTemplateGroups` 对应组中至少使用一个可信局部 Template 表达完整业务单元；缺失的事实再用标准组件补齐。标准组件不能完整替代已选业务高级组件。
5. 所有业务字符串必须逐字复制 dataFacts、`businessTitleCandidate` 或 Action 候选；每个 Text 只能使用其中一个完整字符串，禁止用 `/`、空格、标点把多个事实拼成新字符串。若某个 Template 的必填参数没有语义匹配的输入事实，必须放弃该 Template 并改用其他 Template 或标准组件；禁止为满足参数而补写状态、单位、标签或解释。
6. `trustedStringLiterals` 是本次所有非素材 string 参数的完整白名单。Template 文本参数和 Text 内容必须逐项从中原样选择，禁止翻译、拼接、补标签或改写；素材参数只从 `trustedAssetSources` 选择。
7. 2x2 通常使用 1 到 2 个业务单元，最多 3 个；2x4 通常使用 2 到 3 个，最多 4 个。整卡最多一个主 Action 和一个主图表；仅设置/控制矩阵允许 2～4 个同级控制 Action，且只能使用独立批准的 actionId。列表项分别最多 2/3。禁止独立整卡 Header；`businessTitleCandidate` 若能准确命名当前业务，可作为业务内容区首个紧凑标题，若局部 Template 或事实已经表达则省略，禁止从 request 截取额外标题。
8. UX Token 只由服务端静态降级使用，模型不得把 Token 数值写进 DSL。
9. 这是受限数据语法，不是 JavaScript/TypeScript。只输出一棵以分号结束的调用树；不得输出 Markdown、解释、JSX、命名参数、自由颜色、自由尺寸、事件对象、URL、Data Path、组件 ID 或 A2UI。
10. 局部 Template 严格写成 `Template("templateId@version", "size", { param: value })` 且不接收 children。标准组件的值参数必须位于第一个 child 前；禁止数组包装 children。
11. 每条 mustKeep/mustKeepNumbers 必须由一个标准组件或局部 Template 消费；相同值但 path 不同的事实仍是独立事实。素材不是必须全部消费，按 description 与参数语义匹配，禁止把素材 src 当标题、标签或数值。
12. Action 只输出批准的 actionId 和可选批准图标，不输出 label、call、args 或 onClick；服务端根据 Contract 注入可见文案和事件。没有批准 Action 时省略 Action，禁止标准 Button 和 Action Template。Template 素材参数只可从该参数签名的 allowedSources 中选择；签名未出现的 variant 不可使用。
13. 2x2 的 PillAction 固定占底部 36vp，业务区必须紧凑：数值与短单位应放在同一 Row，禁止把同一指标拆成多个纵向 Row；同一事实不得在两个 Template 或 Template 与标准组件之间重复。天气当前态若有动作图标，优先用右下角 IconAction，不使用占满底部的 PillAction。ActionTile 默认只用于 2x4；2x2 仅 `ActionMatrixLayout` 可按其 Action 数量契约使用紧凑 ActionTile。
<!-- prompt:end -->
