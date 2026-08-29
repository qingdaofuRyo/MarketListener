# R4 当前启动协议与历史 FULL 角色模板

## 必读顺序

每个新的实现、审查或验收任务必须依次阅读：

1. 根目录 `Plan_R4.md`，确认唯一当前任务和状态；
2. `ADR.md` 与当前任务相关的单项 ADR；
3. `CONTEXT.md`；
4. `Experience.md` 与 `Log.md` 中相关环境事实；
5. 当前任务相关的契约、数据审计、测试和历史交付记录。

`Plan_R1.md`、`Plan_R2.md`、`Plan_R3.md`、`Plan_full.md`、`STATUS.md`、`Plan.md` 与 `docs/deliveries/D0-*` 只用于查阅历史规范和证据，不是 R4 待办队列。下方按 `FULL-*` 状态领取任务的提示词保留为历史角色模板；R4 实际状态以 `Plan_R4.md` 为准。

## 按角色固定启动提示词

启动者必须先明确自己是实现、审查还是验收角色，并使用对应提示词。三种角色不共享“领取 `READY`”规则。

### 实现或修复

```text
项目路径：C:\Users\qingd\Documents\MarketListener

阅读 START_HERE.md 和 STATUS.md。若存在 CHANGES_REQUIRED，修复其中指定的同一任务；
否则执行第一项 READY 任务。严格遵守 Plan_full.md 中该任务的依赖、修改范围、
必跑测试和验收标准。
只完成一个任务；写交付报告并进入 REVIEW 后停止，不自动继续下一项。
```

### 独立审查

```text
项目路径：C:\Users\qingd\Documents\MarketListener

阅读 START_HERE.md、STATUS.md 和目标任务交付记录，只审查 STATUS.md 中处于 REVIEW
的当前任务。按 P0/P1/P2/P3 输出带文件和行号的问题并运行只读复核。
通过则进入 ACCEPTANCE；否则进入 CHANGES_REQUIRED。不得修改实现或启动其他任务。
```

### 独立验收

```text
项目路径：C:\Users\qingd\Documents\MarketListener

阅读 START_HERE.md、STATUS.md 和目标任务交付/审查记录，只验收 STATUS.md 中处于
ACCEPTANCE 的当前任务。重新运行 Plan_full.md 规定的测试和真实操作并保存证据。
通过才可标为 ACCEPTED 并解锁依赖；否则进入 CHANGES_REQUIRED 或 BLOCKED。
```

## 角色协议

### 实现任务

1. 新实现必须领取 `STATUS.md` 第一项 `READY` 且依赖全部 `ACCEPTED`；修复只能领取当前 `CHANGES_REQUIRED` 的同一任务。
2. 将该任务更新为 `IN_PROGRESS`，不启动其他任务。
3. 只修改任务定义允许的范围；架构冲突先停止并交回用户。
4. 运行任务要求的自动化测试和必要诊断，记录实际命令与真实结果。
5. 创建或更新 `docs/deliveries/FULL-<编号>.md`。
6. 将任务更新为 `REVIEW` 后停止；不得自审、自验或继续下一项。

### 审查任务

1. 只领取处于 `REVIEW` 的指定任务，确认审查者不是实现者。
2. 只审查该任务差异，按 `P0` 至 `P3` 输出文件和行号。
3. 不做无关重构，不把自动化测试当作真实数据或真机验收。
4. 有阻断发现时追加审查记录并改为 `CHANGES_REQUIRED`；否则改为 `ACCEPTANCE` 后停止。

### 验收任务

1. 只领取处于 `ACCEPTANCE` 的指定任务，确认验收者独立于实现者。
2. 重新运行验收命令和必要的真实数据、设备或恢复流程。
3. 证据不足时改为 `CHANGES_REQUIRED` 或 `BLOCKED`，不得推测通过。
4. 通过时追加验收记录，将任务改为 `ACCEPTED`，再把依赖已满足的最早任务改为 `READY`。

## 安全停止条件

- 实现角色既没有 `CHANGES_REQUIRED` 当前任务，也没有 `READY` 任务；
- 审查角色没有 `REVIEW` 当前任务，或验收角色没有 `ACCEPTANCE` 当前任务；
- 新实现任务的依赖状态不满足；
- 需要凭据、授权、付费资源或用户设备但尚未提供；
- 需要改变 ADR、公开契约、数据库边界、交易日语义或付费上限；
- 工作会越过当前单一任务范围。

遇到停止条件时，记录事实与解除条件，不使用模拟成功或擅自扩大范围。
