# CardPlan Template 融合服务化说明

## 目标与路由

本实现只改变 `generateWidgetCardTerseDslNested2` 的低置信度 create 路径，不增加第三次模型调用：

1. 第一次模型调用输出兼容扩展后的 `UIBrief`。
2. 服务端使用 `UIBrief` 对既有整卡模板候选评分。
3. 置信度不低于阈值时继续填充整卡模板；低于阈值时第二次模型调用生成
   `card@1` 外壳及局部 Template/标准组件混排的 Hybrid DSL。
4. 服务端静态展开 Template，复用现有 Terse Nested2 到 A2UI Adapter，端侧只接收标准 A2UI。

edit 路径保持既有完整 Design Token 编辑协议。模型 mock 下的旧回归调用也保留原 Terse 路径；测试可通过
受控 bypass 强制验证混合路线。

## 代码与 TypeScript 基线映射

| TypeScript 基线能力 | Python 正式服务 |
| --- | --- |
| card-plan-template 模型和 Contract | `cloud/services/cardplan_template/models.py` |
| terse-template-registry | `registry.py` 和 `cloud/data/cardplan_template/source` |
| advanced-component-registry | `advanced_component_pipeline/composition.py` 和机械导出的 `advanced-component-registry.json` |
| hybrid-fragment / Nested-2 Parser | `parser.py`、`framer.py` |
| template-expander / composition | `compiler.py` |
| prompt-runtime | `prompt.py`、`generated/prompts.py` |
| generation-runner | `advanced_component_pipeline/pipeline.py` |
| UI IR / A2UI Adapter | 复用 `services/terse_dsl_nested2_a2ui_converter.py`，输出 `v0.9` / `ohos.a2ui.extended.catalog` |
| Manifest / SHA gate | `scripts/build_cardplan_bundle.py` 与两个 TS export 脚本 |

生产代码不读取 Golden。`tests/fixtures/cardplan_golden_scenarios.json` 仅由测试脚本机械导出，用于跨语言回归。

## 高级组件补齐

正式服务从 TypeScript 行为基线机械导入 15 个高级组件族、8 个自适应模板族、领域组合白名单和 2x2/2x4
内容预算。`AdvancedComponentPipeline` 在第一次 UIBrief 调用前根据 Query 与 TaskSpec Schema 生成只读
`advancedComposition`；模型可以省略这些字段，服务端会覆盖模型同名值，不增加第三次调用。

高级组件不是端侧新 Catalog 节点。每个组件族只声明领域语义、角色、presentation、variant、隐私、
Action/Chart 能力和版本化局部 Template 映射；第二轮 Hybrid 输出仍只允许标准组件与批准的局部
Template。服务端展开并转成 A2UI 后出现 `Template` 或高级组件名即失败。

`composition.py` 负责主领域排序、跨领域共同目标、尺寸角色、forecast 前置条件和预算；
`domain_rules.py` 负责日程、待办、通话、电量、App 使用、运动、睡眠、位置、系统模式与蓝牙等确定性派生。
位置、电话、日程正文、睡眠窗口等默认使用 `masked`，真实 `0` 不能按空值裁掉。计划 Validator 在模型前
拒绝两个 Hero、多个主 Action/Chart、无共同目标组合、未填槽位、敏感全量展示和所有超预算输入。

## 安全边界

- Parser 使用 Python AST 解析声明式调用和字面量，不使用 `eval` 或 `exec`。
- 只允许 `card@1`、Registry 中的版本化 Template、Catalog 标准组件及白名单字段。
- Template 展开前后分别校验 variant、参数 Schema、父组件、Action、素材、字面量、节点、深度和空间预算。
- Template 只在可信服务端展开；编译后 A2UI 出现 `Template` 即失败。
- 模型只能引用本次 Contract 暴露的数据路径、素材和 Action。Template 的占位 Action 在展开后绑定回
  TaskSpec 中已批准的完整 `call/args`。
- CardTemplate UX 测试页可在候选数据能力中携带受限 `previewData`，用于构造与真实数据同形的 TaskSpec
  样例；服务端限制其类型、深度、节点数、数组/字符串和编码大小，并从 Pydantic 序列化及生产日志中排除。
- 生产日志不记录业务正文、Prompt、原始输出或密钥。评估证据只写入忽略目录并设置为 `0600`。

## bypass

请求中的测试参数为：

```json
{
  "options": {
    "forceHybridTemplate": true,
    "testAuthorization": "由测试环境注入的短期 token"
  }
}
```

四项条件必须同时成立：

- `WIDGET_SERVICE_ENABLE_HYBRID_TEST_BYPASS=true`；
- `WIDGET_SERVICE_ENV=local` 或 `test`；
- 服务端配置了非空 `WIDGET_SERVICE_HYBRID_TEST_BYPASS_TOKEN`；
- 请求 token 常量时间比较通过。

生产默认关闭。`testAuthorization` 从 Pydantic 序列化中排除，并由日志清洗器无条件移除。任何条件不满足都
返回未授权错误，不会静默进入混合路线。

## DeepSeek 硬预算

`cloud/custom/deepseek_call_budget.py` 使用 SQLite `BEGIN IMMEDIATE` 在每次真实
`deepseek_platform`/`llmclient` 调用前预留。请求发送失败仍计数；达到 400 后抛出
`DeepSeekCallBudgetExceeded`，该异常不会进入重试、fallback 或旧 Terse 路线。

配置：

```dotenv
WIDGET_SERVICE_DEEPSEEK_CALL_BUDGET_LIMIT=400
WIDGET_SERVICE_DEEPSEEK_CALL_BUDGET_PATH=workspace/runtime/deepseek_call_budget.sqlite3
```

生产默认值仍为 `400`。经明确授权的隔离评估任务可在单次进程环境中设置
`WIDGET_SERVICE_DEEPSEEK_CALL_BUDGET_LIMIT=0` 进入无限模式；该模式只取消拒绝门槛，仍会在每次调用前
原子增加同一计数器，因此不会丢失或重置既有历史。不得把 `0` 写入生产部署配置。预算数据库属于运行状态，
不提交、不删除、不重置。多进程可共享
同一 SQLite 文件；多主机部署前必须提供具有可靠文件锁的共享持久卷，或迁移到等价的共享原子计数服务。

使用直连 DeepSeek 的本地真实评估时，把凭据写入 `widget_service/.env`（已被 Git 忽略），不要写入 Shell
历史、测试 Fixture、报告或提交：

```dotenv
WIDGET_SERVICE_OPENAI_MASTER_CLIENT=llmclient
WIDGET_SERVICE_OPENAI_FALLBACK_CLIENT=deepseek_platform
WIDGET_SERVICE_ENABLE_OPENAI_FALLBACK=false
WIDGET_SERVICE_DEEPSEEK_API_KEY=替换为本地密钥
WIDGET_SERVICE_DEEPSEEK_API_URL=https://api.deepseek.com
WIDGET_SERVICE_DEEPSEEK_MODEL=deepseek-v4-flash
WIDGET_SERVICE_DEEPSEEK_ENABLE_THINKING=false
```

直连 HTTP 请求按 DeepSeek OpenAI 兼容协议显式发送
`"thinking":{"type":"disabled"}`；不能只依赖本地布尔配置，因为 V4 默认启用 thinking。HTTP 流在应用
事件循环中执行，达到 `WIDGET_SERVICE_MODEL_REQUEST_TIMEOUT_SECONDS` 后会关闭连接；内部 WebSocket 和测试
注入的同步 Transport 继续持有并发令牌直到物理调用结束。路由单测必须把
`WIDGET_SERVICE_DEEPSEEK_CALL_BUDGET_PATH` 指向测试临时目录，不能污染生产/真实评估预算。

## 生成物和 SHA 门禁

在 `intermediate_expression` 执行：

```bash
pnpm exec tsx ../CreateMyCard/widget_service/scripts/export_cardplan_baseline.ts --check
pnpm exec tsx ../CreateMyCard/widget_service/scripts/export_cardplan_golden_fixture.ts --check
```

在 `widget_service` 执行：

```bash
python scripts/build_cardplan_bundle.py --check
```

需要接受上游 TS 变更时，先不带 `--check` 重新导出，再审查 Registry、Prompt 和 Manifest diff。Prompt
Manifest 对每个源片段和生成常量保存 SHA-256；任何未重新生成的漂移都会使门禁失败。

## 测试与评估

确定性评估不调用模型：

```bash
PYTHONPATH=cloud python scripts/evaluate_cardplan_golden.py \
  --mode deterministic \
  --output workspace/runtime/cardplan_template_evaluation/deterministic-latest.json
```

真实评估只在全部确定性门禁通过后执行：

```bash
PYTHONPATH=cloud python scripts/evaluate_cardplan_golden.py \
  --mode live --confirm-live \
  --output workspace/runtime/cardplan_template_evaluation/live-latest.json
```

真实命令强制关闭模型 mock、模型 fallback 和模型失败重试。报告保存每轮原始 Prompt/输出，并在供应商
协议提供时保存原始 usage 和 finish reason；供应商未返回的字段保持 `null`，同时单独提供标记为
`char-estimate` 的估算 Token。任一场景原始协议失败、最终未 ready 或使用 fallback 时命令返回非零。

编译器或评估器修正后可对保存的真实证据零调用重分析：

```bash
PYTHONPATH=cloud python scripts/reanalyze_cardplan_golden.py \
  --input workspace/runtime/cardplan_template_evaluation/live-latest.json \
  --input workspace/runtime/cardplan_template_evaluation/live-low-power.json \
  --input workspace/runtime/cardplan_template_evaluation/live-family-care-weather.json \
  --output workspace/runtime/cardplan_template_evaluation/live-final-reanalyzed.json
```

同一场景在后一个输入报告中出现时覆盖前一个证据，但不会修改原始模型调用内容，也不会预留预算。

Action 位置、Theme/Template 联动、整卡 Action 基础组件外壳和 capsule Progress 投影优化后的确定性结果：
10/10 `finalReady`、0 fallback、10/10 达到结构阈值；最低组件类型相似度为 0.7727。该结果仍由机械导出的
Hybrid Source 经过正式 Parser、Registry、Compiler 和 A2UI Adapter 得到，没有以 Golden A2UI 覆盖结果。

关闭 thinking 并把单阶段最大输出收紧为 8192 后，当前真实 DeepSeek 最终重分析结果：10/10 原始双阶段
协议成功、10/10 `finalReady`、0 fallback、10/10 达到严格 Golden 阈值。供应商原始 Token 合计
62,193，场景累计时延 146,616ms；相对旧报告 131,828 Token / 929,046ms，分别下降 52.8% / 84.2%。
该时延是各场景串行证据的累计值，且 `headset-music` 单场受上游抖动影响占 103,121ms，不代表生产并发
吞吐。逐场结果如下：

| 场景 | Template | 展开组件 | Token | 时延 ms | 类型/数量/根样式相似度 | 严格对齐 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| current-meeting | `ux-meeting-metadata@1` | 17 | 7,037 | 4,369 | 0.8000 / 0.8947 / 0.7778 | pass |
| headset-music | `ux-audio-device-status@1`, `ux-action-metric@1` | 21 | 6,153 | 103,121 | 0.7600 / 0.9130 / 0.5455 | pass |
| focus-mode | `ux-calendar-content@1` | 14 | 5,498 | 3,795 | 0.8000 / 0.9286 / 0.7273 | pass |
| low-power | `ux-battery-status@1` | 18 | 5,495 | 4,848 | 0.8421 / 0.9444 / 0.7000 | pass |
| sleep | `ux-sleep-metric@1` | 20 | 7,469 | 3,742 | 0.8095 / 0.9000 / 0.7273 | pass |
| family-care-weather | `ux-weather-hero@1`, `ux-condition-index@1`, `ux-icon-action@1` | 22 | 7,651 | 5,441 | 0.7273 / 0.7273 / 0.6250 | pass |
| rainy-commute | `ux-weather-hero@1`, `ux-context-summary@1` | 19 | 5,586 | 5,385 | 0.7895 / 0.7895 / 0.6667 | pass |
| device-clean | `ux-device-metric@1` | 26 | 5,906 | 5,157 | 0.8077 / 0.8077 / 0.7000 | pass |
| digital-wellbeing | `ux-segmented-limit@1` | 23 | 5,595 | 5,668 | 0.8333 / 0.9130 / 0.7000 | pass |
| race-countdown | `ux-countdown@1`, `ux-action-summary@1` | 18 | 5,803 | 5,090 | 0.7778 / 0.7778 / 0.7778 | pass |

最终合并报告的持久计数快照为 428；本轮经明确授权使用无限评估模式，因此 limit/remaining 为 null，生产
默认仍为 400。原始 Prompt/输出报告位于忽略目录并保持 `0600`，不提交。若部署环境未提供 DeepSeek
凭据或网络不可达，评估必须保持失败且 `fallback=false`，不得用确定性结果替代真模型结论。预算状态以
评估报告中的原子预留快照为准，不在版本库文档中持续更新运行时数据库计数。

## 上线、观测与回滚

仓库根目录的 `.dockerignore` 与 `widget_service/Dockerfile` 用于构建 Python 3.12 运行镜像。运行时将
`cloud/workspace` 挂载为持久卷，以保留预算 SQLite 与 mock artifact；环境文件必须位于宿主机受限权限路径，
不得打入镜像。对 CardTemplate UX 替换旧 TS 服务时，先在旁路端口验证 `GET /health`、未授权连接以 1008
拒绝、带 Token 的 `/ws` `card.generate` 返回 `card.generate.delta/result` 且最终消息为 ready A2UI，再切换
原端口。`createSurface.catalogId` 必须为 `ohos.a2ui.extended.catalog`；带 `.form` 的历史值会被端侧扩展
渲染器拒绝。切换前记录旧容器镜像 ID 与端口映射，回滚仅恢复旧容器，不删除 Python 持久卷。

公网部署必须设置 `WIDGET_SERVICE_WEBSOCKET_BEARER_TOKEN`；该 Token 同时保护 `/ws` 与全部工具 WebSocket。
端侧本地配置可用独立的 `cardTemplateWebSocketUrl` 指向旁路端口，未设置时兼容回退到 `webSocketUrl`。
测试环境经明确授权可将预算上限设为 `0`，但必须保留已有计数数据库；正式生产仍不得使用无限预算。

上线顺序：

1. 在预发执行生成物 SHA、Ruff、mypy、全量 pytest、wheel build 和十场景真实评估。
2. 确认预算数据库位于持久卷、进程用户可写，且当前 used/remaining 已记录。
3. 保持 bypass 关闭；以小流量灰度第五接口 create 请求。
4. 观测 `route`、`whole_card_confidence`、`confidence_bypassed`、`raw/effective` 长度、`fallback_used`、
   Template 调用/展开组件数、编译失败类别、ready 率、Token 和时延。
5. 高置信度整卡模板与其它四个工具接口作为对照组，确认成功率和延迟无回归后再扩大流量。

回滚不删除预算数据库、不改 Golden，也不把失败输出保存为 artifact：将服务回滚到前一制品即可恢复旧
低置信度 Terse 路由；如果仅需阻止测试入口，保持或恢复
`WIDGET_SERVICE_ENABLE_HYBRID_TEST_BYPASS=false`。回滚后继续保留预算和评估报告用于审计。

## 已知限制

- `card@1` 当前只支持 Registry 导出的 Catalog、主题和尺寸；新增 Template 必须先更新 TS 基线再走 SHA 门禁。
- 空间估算是服务端保守预算，超过推荐高度会标记 `space_constrained`，硬节点/深度限制仍会拒绝。
- 共享 SQLite 预算依赖文件锁；跨不共享文件系统的多副本不能宣称全局 400 次保证。
- 真实 DeepSeek 的 usage/finish reason 是否可得取决于上游协议是否在 final 消息中返回这些字段。
