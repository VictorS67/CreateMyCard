# Compact/Terse 模板路由与双产物设计

## 设计目标

模板是 Compact 和 TerseDSL-Nested-2 create 场景的内部生成方式，不新增外部协议。原始入口只负责构造
既有 `GenerationRoutePolicy`，随后调用对应模板路由门面。Compact 保留 dev 原有回退契约；Terse 使用严格
模板契约。

## 路由状态机

```text
generateWidgetCardCompactDsl
  └─ route_compact_generation
       ├─ edit
       │    └─ 原始 Compact 流程
       └─ create
            ├─ 准备 dev 能力裁决、CardSpec、TaskSpec
            ├─ 第一层 LLM：选择 Theme 和一个或多个业务模板
            ├─ 服务端完整覆盖校验
            │    ├─ 未覆盖任一 candidateOutputField → 原始 Compact 流程
            │    └─ 全部覆盖 → 锁定模板路由
            ├─ 第二层 LLM：只生成受限布局和模板调用
            ├─ 服务端解析、参数校验、模板展开
            ├─ 内部 A2UI 适配当前 dev Form profile
            ├─ A2UI → A2UI-Compact
            ├─ dev Compact Processor → 最终 A2UI
            ├─ dev ArtifactValidator
            └─ ArtifactStore 保存 genui + designcompactdsl
```

```text
generateWidgetCardTerseDslNested2
  └─ route_terse_nested2_generation
       ├─ edit → failed
       └─ create
            ├─ 第一层 LLM 和服务端完整覆盖校验
            │    └─ 未匹配、字段未完整覆盖或模型不可用 → failed
            ├─ 第二层 LLM、参数校验和模板展开
            │    └─ 任一失败 → failed
            ├─ 展开后的 TerseDSL-Nested-2 → 原始 Terse Processor → 最终 A2UI
            ├─ dev ArtifactValidator
            └─ ArtifactStore 保存 genui + 展开后的 TerseDSL-Nested-2
```

## 为什么先归档 Compact 再确定最终 A2UI

模板编译器先产生内部 A2UI，但 artifact 中的 A2UI-Compact 会在后续 edit 中由原始 dev Processor 读取。
如果首次展示直接保存模板编译器输出，后续 Processor 的规范化可能造成视觉或逻辑漂移。

因此本模块执行以下闭环：

1. 模板内部 A2UI 仅作为中间结果。
2. 适配当前 dev 的 `catalogId`、root 尺寸、圆角和裁剪约束。
3. 确定性生成 A2UI-Compact。
4. 使用原始 dev Compact Processor 将该 Token 转回标准 A2UI。
5. 将回转结果作为首次展示的最终 A2UI，并将同一个 Token 写入 `designcompactdsl`。

这样首次展示和二次更新共享同一条 Compact 转换链。

## 失败与回退边界

| 阶段 | 行为 | 原因 |
|---|---|---|
| edit 请求 | 原始流程 | 二次更新不重新选择模板 |
| 无真实模型运行时 | 原始流程 | 保持 dev mock 和既有测试行为 |
| 第一层拒绝或异常 | 原始流程 | 尚未承诺模板可完整表达 |
| 确定性覆盖失败 | 原始流程 | 任一用户选定字段无法由模板表达 |
| 第二层或模板编译失败 | 返回 failed | 模板路由已经锁定，禁止静默换实现 |
| Compact 归档或 Validator 失败 | 返回 failed | 禁止保存无法稳定编辑的半成品 |

`before_model_call` 由门面包装为单次通知。第一层已经触发通知时，即使回退原始模型，也不会重复下发开始事件。

Terse 路线没有回退分支：create 模板不匹配时返回 `failed`，edit 也直接返回 `failed`。旧 Python 模板
流水线仅通过 `legacy_python.route_legacy_python_terse_generation(...)` 作为问题定位入口保留；生产默认入口
不引用该函数，`widget_generation_service.py` 中的切换点注释用于需要时进行临时对照。

## 对原始 dev 的修改边界

`widget_generation_service.py` 只增加公共入口 import，并将 Compact、Terse 两个入口分别收敛为一次门面调用。
A2UI Form、能力注册、API、配置、日志和批量接口均不需要为模板功能修改。
