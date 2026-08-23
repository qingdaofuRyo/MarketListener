# 文档索引

[`Plan_R3.md`](../Plan_R3.md) 是当前唯一未完成工作清单。`Plan_R1.md`、`Plan_R2.md`、`STATUS.md` 和更早计划仅保留背景与证据，本文只提供稳定入口；历史文档不新增独立待办。

| 类别 | 长期有效入口 | 说明 |
| --- | --- | --- |
| 项目约束与术语 | `ADR.md`、`CONTEXT.md`、`adr/` | 架构决策与统一语言。 |
| 架构与契约 | `ARCHITECTURE.md`、`DESKTOP_API_CONTRACT.md`、`DESKTOP_ANDROID_PARITY_ANALYSIS.md`、`industry-graph-terminology.md` | 当前整体架构、跨端接口和领域模型。 |
| 数据源与数据能力 | `DATA_SOURCE_CAPABILITY_MATRIX.md`、`TICKDB_TDX_DATA_AUDIT_2026-08-24.md` | 真实实现/存量/授权边界，以及 TickDB/通达信接入审计。 |
| 测试与发布 | `reviews/`、`release/`、`release-checklist.md` | 审查、验收和发布证据。 |
| 历史规划与阶段分析 | `Plan_R1.md`、`Plan_R2.md`、`Plan.md`、`Plan_full.md`、`STATUS.md`、`*_CURRENT_ANALYSIS.md`、`history/` | 保留背景与证据，不作为 R3 新待办；已确认无活动引用的阶段队列归入 `history/`。 |
| 交付与经验 | `R3_CHANGE_INVENTORY_2026-08-24.md`、`deliveries/`、`Log.md`、`Experience.md`、`正式开发交接.md` | 当前逐文件清单、可复核实现记录和环境事实。 |

物理归档或删除历史文档前，必须先完成代码、测试、README 和文档的反向引用审计；在此之前优先使用本索引分类，避免破坏既有链接。
