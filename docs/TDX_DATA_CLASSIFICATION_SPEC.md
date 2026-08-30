# 通达信盘后数据分类入库规格

最后更新：2026-08-30。本文记录通达信金融终端/期货通盘后下载数据的已知文件名映射与待确认项。终端类型、目录路径和文件名前缀用于判定市场归属；二进制内容仍必须用于验证记录布局、价格精度与量纲，不能仅凭文件名提升数据到 Silver。

> 实施状态：`tdx_local.py` 已安全支持金融终端 `12/16/17/18/27/31/48/62/69/102#` 的浮点日线和 5 分钟布局。2026-08-30 已在只读审计后增量写入 4,043 个来源文件、38,663,985 根 `PASS` K 线；907 个文件/108,456 根记录隔离，138 个文件拒绝。`16/17/18#` 的金额与 `00W/00Y` 经济含义尚未验证，统一写为 `amount=null`、`TDX_FOREIGN_FUTURE_RAW` 和 `UNVERIFIED_CONTINUOUS`，不标主连或连续。`10#` 汇率、`38#` 宏观、`49#` 基金、`98#` 及未知前缀保留在待分类表。本机仍有 429 个 `49#` 文件，故“已删除”不能作为导入依据。

## 核心结论

**通达信 `.day` / `.lc5` 二进制文件不包含任何市场类型元数据。** 每条记录固定 32 字节，只有日期、OHLC、成交额/持仓量、成交量和一个保留字段。市场分类的唯一依据是：

1. **来源终端**：`C:\tongdaxin`（金融终端）还是 `C:\new_tdxqh`（期货通）
2. **目录路径**：`vipdoc/sh` / `vipdoc/sz` / `vipdoc/bj` / `vipdoc/ds`
3. **文件名前缀**：`sh` / `sz` / `bj` + 6 位代码，或 `NN#` + 代码
4. **代码前缀/后缀**：6 位代码的前 3 位区分股票/ETF/指数/转债等；期货代码的后缀 `L7`/`L8`/`L9` 或 `00W`/`00Y` 区分连续/主连/加权

因此，已验证映射应集中在文件发现与分类逻辑中；未验证的前缀、字段语义或序列种类必须留在待分类表，不能依赖文件内容猜测市场类型或直接导入。

## 两个终端的职责划分

| 终端 | 安装路径 | 下载数据类型 | vipdoc 扫描范围 |
| --- | --- | --- | --- |
| 金融终端 | `C:\tongdaxin` | 沪深京 AB 股、港股、国际期货、国际指数、汇率、宏观 | `sh/` `sz/` `bj/` `ds/` |
| 期货通 | `C:\new_tdxqh` | 国内商品/金融期货、商品指数 | `ds/` |

两个终端的 `vipdoc/ds/` 目录包含完全不同的数据，用数字前缀（`NN#`）区分。同一个 `ds/` 目录在不同终端下含义不同，**分类器必须先判断来源终端再判断文件名前缀**。

## 文件名命名规则总览

通达信文件名只有三种模式：

### 模式 A：沪深京证券（仅金融终端）

```
vipdoc/{sh|sz|bj}/{lday|fzline}/{sh|sz|bj}{6位代码}.{day|lc5}
```

- 前缀 `sh` / `sz` / `bj` 同时是目录名和文件名开头
- 代码固定 6 位数字
- `.day` = 日线，`.lc5` = 5 分钟线
- 示例：`sh600000.day`（浦发银行日线）、`sz159001.lc5`（ETF 5 分钟线）

### 模式 B：港股与多市场数据（金融终端 ds 目录）

```
vipdoc/ds/{lday|fzline}/{NN}#{代码}.{day|lc5}
```

- `NN#` 是数字市场前缀，不同数字代表不同市场
- 港股代码 5 位数字，国际期货/指数/汇率/宏观代码格式各异
- 示例：`31#00700.day`（腾讯港股日线）

### 模式 C：国内期货（期货通 ds 目录）

```
vipdoc/ds/{lday|fzline}/{NN}#{产品代码}{后缀}.{day|lc5}
```

- `NN#` 数字前缀对应国内期货交易所
- 产品代码为字母（如 `RB` 螺纹钢、`AU` 黄金）
- 后缀有三种：`L7`/`L8`/`L9`（次连/主连/加权）或 3-4 位数字交割月（如 `2610`）
- 示例：`30#AGL8.day`（上海期货所白银主连日线）、`28#AP2610.day`（郑州商品所苹果 2610 合约日线）

## 14 类盘后数据的完整映射表

下表把用户在金融终端"盘后数据下载"工具中能选择的 14 类数据，与实际文件位置、命名模式和分类规则一一对应。前缀已于 2026-08-29 由用户亲自比对通达信盘后数据下载工具确认。标 ✅ 的已由现有代码处理；标 ❌ 的当前落入待分类审计表；标 ⚠️ 的部分处理。

| # | 盘后数据类型 | 来源终端 | 文件位置 | 文件名模式 | 市场前缀 | market | exchange | assetType | 当前状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 沪深京 AB 股日线 | 金融终端 | `vipdoc/{sh,sz,bj}/lday/` | `{sh\|sz\|bj}{6位}.day` | 目录名 | CN | SSE/SZSE/BSE | 按代码前缀区分 | ✅ 已处理 |
| 2 | 沪深京 AB 股 5 分钟线 | 金融终端 | `vipdoc/{sh,sz,bj}/fzline/` | `{sh\|sz\|bj}{6位}.lc5` | 目录名 | CN | SSE/SZSE/BSE | 按代码前缀区分 | ✅ 已处理 |
| 3 | 港股指数日线 | 金融终端 | `vipdoc/ds/lday/` | `27#{代码}.day` | `27#` | HK | HKEX | INDEX | ✅ 已支持 |
| 4 | 香港主板日线 | 金融终端 | `vipdoc/ds/lday/` | `31#{5位}.day` | `31#` | HK | HKEX | STOCK | ✅ 已处理 |
| 5 | 香港创业板日线 | 金融终端 | `vipdoc/ds/lday/` | `48#{5位}.day` | `48#` | HK | HKEX | STOCK | ✅ 已支持 |
| 6 | 纽约 COMEX 期货日线 | 金融终端 | `vipdoc/ds/lday/` | `16#{产品}{后缀}.day` | `16#` | GLOBAL | COMEX | FUTURE | ⚠️ 仅原始量纲，序列种类未验证 |
| 7 | 纽约 NYMEX 期货日线 | 金融终端 | `vipdoc/ds/lday/` | `17#{产品}{后缀}.day` | `17#` | GLOBAL | NYMEX | FUTURE | ⚠️ 仅原始量纲，序列种类未验证 |
| 8 | 芝加哥 CBOT 期货日线 | 金融终端 | `vipdoc/ds/lday/` | `18#{产品}{后缀}.day` | `18#` | GLOBAL | CBOT | FUTURE | ⚠️ 仅原始量纲，序列种类未验证 |
| 9 | 国际指数日线 | 金融终端 | `vipdoc/ds/lday/` | `12#{代码}.day` | `12#` | GLOBAL | — | INDEX | ✅ 已支持 |
| 10 | 基本汇率日线 | 金融终端 | `vipdoc/ds/lday/` | `10#{代码}.day` | `10#` | GLOBAL | — | FX_RATE | ⛔ 字段契约未验证 |
| 11 | 中证指数日线 | 金融终端 | `vipdoc/ds/lday/` | `62#{代码}.day` | `62#` | CN | CSI | INDEX | ✅ 已支持 |
| 12 | 国证指数日线 | 金融终端 | `vipdoc/ds/lday/` | `102#{代码}.day` | `102#` | CN | CNI | INDEX | ✅ 已支持 |
| 13 | 华证指数日线 | 金融终端 | `vipdoc/ds/lday/` | `69#{代码}.day` | `69#` | CN | HUAZHENG | INDEX | ✅ 已支持 |
| 14 | 宏观指标日线 | 金融终端 | `vipdoc/ds/lday/` | `38#{代码}.day` | `38#` | CN/GLOBAL | — | MACRO | ⛔ 字段契约未验证 |

### 前缀无冲突

所有 14 类数据的前缀现在一一对应，无任何冲突。`38#` 只用于宏观指标，`48#` 只用于港股创业板，两者不再共用前缀。

### 暂不导入的前缀

- `49#`：此前记录称香港基金日线已手动删除；2026-08-30 文件系统审计仍发现 429 个文件。缺少基金资产、币种、单位与数据来源确认，继续待分类，不删除也不导入。

### 金融终端 ds/ 前缀总表（用户确认版）

| 前缀 | 数据类型 | market | exchange | assetType | 文件名代码格式 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `10#` | 基本汇率 | GLOBAL | — | FX_RATE | 待确认 | 新增枚举 |
| `12#` | 国际指数 | GLOBAL | — | INDEX | 待确认 | |
| `16#` | COMEX 期货主连 | GLOBAL | COMEX | FUTURE | 产品代码+后缀 | 后缀待确认（00W/00Y?） |
| `17#` | NYMEX 期货主连 | GLOBAL | NYMEX | FUTURE | 产品代码+后缀 | 后缀待确认 |
| `18#` | CBOT 期货主连 | GLOBAL | CBOT | FUTURE | 产品代码+后缀 | 后缀待确认 |
| `27#` | 港股指数 | HK | HKEX | INDEX | 待确认 | |
| `31#` | 港股主板 | HK | HKEX | STOCK | 5 位数字 | ✅ 已处理 |
| `38#` | 宏观指标 | CN/GLOBAL | — | MACRO | 待确认 | 不再与港股创业板冲突 |
| `48#` | 港股创业板 | HK | HKEX | STOCK | 5 位数字 | ✅ 已支持 |
| `62#` | 中证指数 | CN | CSI | INDEX | 待确认 | |
| `69#` | 华证指数 | CN | HUAZHENG | INDEX | 待确认 | |
| `102#` | 国证指数 | CN | CNI | INDEX | 待确认 | |

### 期货通 ds/ 前缀总表（已有，无变化）

| 前缀 | 数据类型 | market | exchange | assetType | 备注 |
| --- | --- | --- | --- | --- | --- |
| `28#` | 郑商所期货 | CN | CZCE | FUTURE | ✅ 已处理 |
| `29#` | 大商所期货 | CN | DCE | FUTURE | ✅ 已处理 |
| `30#` | 上期所期货 | CN | SHFE/INE | FUTURE | ✅ 已处理，SC/NR/LU/BC/EC 归 INE |
| `47#` | 中金所期货 | CN | CFFEX | FUTURE | ✅ 已处理 |
| `66#` | 广期所期货 | CN | GFEX | FUTURE | ✅ 已处理 |
| `42#` | 通达信商品指数 | CN | TDX | INDEX | ✅ 已处理 |

## 已处理的分类规则详情

### 沪深京 AB 股（类型 1、2）— 已由 `tdx_local.py` 处理

文件名正则：`^(sh|sz|bj)(\d{6})\.(day|lc5)$`

按 6 位代码前 3 位区分资产类型。以下前缀已于 2026-08-29 由用户实地核对 `vipdoc/{sh,sz,bj}/lday/` 目录确认：

| 交易所 | 代码前缀 | 资产类型 | 说明 | 用户确认 |
| --- | --- | --- | --- | --- |
| SSE (sh) | `600` `601` `603` `605` | STOCK | 沪市主板 | ✅ |
| SSE (sh) | `688` `689` | STOCK | 科创板 | ✅ |
| SSE (sh) | `900` | B_SHARE | 沪 B 股（USD） | ✅ |
| SSE (sh) | `510` `511` `512` `513` `515` `516` `517` `518` `520` `530` `551` `560` `561` `562` `563` `588` `589` | ETF | | ✅ |
| SSE (sh) | `501` `502` `506` | LOF | | ✅（`500`/`505` 用户目录中未见，保留在代码中） |
| SSE (sh) | `508` | REIT | | ✅ |
| SSE (sh) | `110` `111` `113` `118` | CONVERTIBLE_BOND | | ✅（`126` 用户目录中未见，保留在代码中） |
| SSE (sh) | `132` | EXCHANGEABLE_BOND | | ✅ |
| SSE (sh) | `204` | PLEDGED_REPO | | ✅ |
| SSE (sh) | `201`-`207` | REPO | | 用户仅确认 `204`，其余 `201`-`203`/`205`-`207` 保留在代码中 |
| SSE (sh) | `000` `999` | INDEX | 权益指数 | ✅（`999` 见下文不一致说明） |
| SSE (sh) | `880` | INDEX | 通达信综合板块指数 | ✅ 用户确认 |
| SSE (sh) | `881` | INDEX | 通达信行业板块指数 | ✅ 用户确认 |
| SZSE (sz) | `000` `001` `002` `003` | STOCK | 深市主板 | ✅ |
| SZSE (sz) | `300` `301` | STOCK | 创业板 | ✅（`302` 用户目录中未见，保留在代码中） |
| SZSE (sz) | `200` | B_SHARE | 深 B 股（HKD） | ✅ |
| SZSE (sz) | `158` `159` | ETF | | ✅ |
| SZSE (sz) | `160`-`169` | LOF | 深市 LOF 基金（16xxxx） | ✅ 用户确认 `160`-`169` 全系列 |
| SZSE (sz) | `180` | REIT | | ✅（`181` 用户目录中未见，保留在代码中） |
| SZSE (sz) | `123` `124` `127` `128` | CONVERTIBLE_BOND | | ✅（`121` 用户目录中未见，保留在代码中） |
| SZSE (sz) | `131` | REPO | | ✅ |
| SZSE (sz) | `399` | INDEX | 权益指数 | ✅ |
| BSE (bj) | `920` | STOCK | 北交所 | ✅ |
| BSE (bj) | `899` | INDEX | 北证指数 | ✅ |

#### 已统一的沪市指数前缀：`999`/`889`/`950`

`tdx_local.py` 将 `000`、`889`、`950`、`999` 都识别为沪市指数：

```python
if code.startswith(("000", "889", "950", "999")):
    return "INDEX", "EQUITY_INDEX", exchange
```

`market_classification.json` 的 `indexPrefixes.SSE` 已同步列出 `000`、`889`、`930`、`931`、`932`、`950`、`999`。因此在显式资产类型尚未补齐的旧行中，`889`/`950`/`999` 也会进入 `a-index`，而不会因库存筛选回退为未分类。保留 `930`、`931`、`932`，因为它们仍可能出现在沪市目录。

#### 深市 LOF 前缀：`16` vs `160`-`169`

`tdx_local.py` 第 668 行用 `code.startswith("16")` 匹配深市 LOF，这覆盖了 `160`-`169` 全系列。`market_classification.json` 的 `lofPrefixes.SZSE` 只写了 `"16"`，也覆盖全系列。用户确认目录中有 `160`-`169` 全部 10 个前缀，两者一致，无需修改。

### 港股与港股指数（类型 3、4、5）— 已由 `tdx_local.py` 处理

港股主板文件名正则：`^31#(\d{5})\.(day|lc5)$`
港股创业板文件名正则：`^48#(\d{5})\.(day|lc5)$`

- `31#` 前缀的 5 位代码统一为 `HK` / `HKEX` / `STOCK`（港股主板），✅ 已处理。
- `48#` 前缀的 5 位代码为港股创业板，归类为 `HK` / `HKEX` / `STOCK`，✅ 已处理。
- 主板与创业板现在是不同前缀，不需要按代码区间区分。
- 港股指数使用 `27#` 前缀，归类为 `HK` / `HKEX` / `INDEX`，✅ 已处理。

### 国内期货 — 已由 `futures_bulk.py` 处理

数字市场前缀对应交易所：

| 前缀 | 交易所 | 特殊规则 |
| --- | --- | --- |
| `28#` | CZCE 郑商所 | |
| `29#` | DCE 大商所 | |
| `30#` | SHFE 上期所 | 产品 `SC` `NR` `LU` `BC` `EC` 归 INE 上海能源 |
| `47#` | CFFEX 中金所 | |
| `66#` | GFEX 广期所 | |
| `42#` | TDX 内部商品指数 | 非交易所，资产类型 INDEX |

期货后缀规则：

| 后缀 | 序列种类 | 含义 | 示例 |
| --- | --- | --- | --- |
| `L7` | SECONDARY | 次连 | `30#AGL7.day` |
| `L8` | MAIN | 主连 | `30#AGL8.day` |
| `L9` | WEIGHTED | 原生加权 | `30#AGL9.day` |
| 3-4 位数字 | CONTRACT | 具体交割月 | `30#AG2610.day` = 2026年10月交割 |

## 待处理类型的规则规格

本节的“待处理”仅指尚不能安全进入正式 Silver 的字段契约或业务语义；上方实施状态已支持的文件发现、浮点布局和标准字段映射不再视为待实现。历史提议中把 `00W/00Y` 直接标为主连/连续、或把 FX/宏观仅凭前缀写入 Bar 的部分，均不构成当前实现。

以下类型或字段当前仍落入待分类审计表（`unclassified_instruments.py`），或虽已有安全文件发现规则但仍需补齐业务语义。前缀已于 2026-08-29 由用户确认。

### 国际期货序列（类型 6、7、8）

前缀已确认：`16#` = COMEX，`17#` = NYMEX，`18#` = CBOT。

**仍需确认**：`00W`/`00Y` 等后缀的业务语义。文件名格式已允许字母、数字和下划线，但当前不根据后缀推断主连或连续；所有这些外盘序列均保持 `UNVERIFIED_CONTINUOUS`。

```powershell
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\16#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\17#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\18#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
```

**分类规则**：

```
前缀 16# → market = GLOBAL, exchange = COMEX, assetType = FUTURE
前缀 17# → market = GLOBAL, exchange = NYMEX, assetType = FUTURE
前缀 18# → market = GLOBAL, exchange = CBOT,  assetType = FUTURE
任意后缀 → seriesKind = UNVERIFIED_CONTINUOUS（直到有来源证据）
```

**产品代码到交易所的映射**（前缀已直接区分交易所，产品代码仅用于品种识别）：

| 交易所 | 前缀 | 常见产品代码 | 产品名称 |
| --- | --- | --- | --- |
| COMEX | `16#` | `GC` `SI` `PL` `PA` `HG` | 黄金/白银/铂金/钯金/铜 |
| NYMEX | `17#` | `CL` `NG` `HO` `RB` `BZ` | 原油/天然气/取暖油/汽油/布油 |
| CBOT | `18#` | `ZC` `ZS` `ZW` `ZB` `ZN` `ZT` | 玉米/大豆/小麦/30年债/10年债/2年债 |

### 港股指数（类型 3）

前缀已确认：`27#`。

```
前缀 27# → market = HK, exchange = HKEX, assetType = INDEX
代码允许字母、数字和下划线，已按 `HK/HKEX/INDEX` 入库。
```

### 港股创业板（类型 5）

前缀已确认：`48#`。

```
前缀 48# → market = HK, exchange = HKEX, assetType = STOCK
代码为 5 位数字
```

### 国际指数（类型 9）

前缀已确认：`12#`。

```
前缀 12# → market = GLOBAL, exchange = null, assetType = INDEX
代码允许字母、数字和下划线，已按 `GLOBAL/GLOBAL_INDEX/INDEX` 入库。
```

### 基本汇率（类型 10）

前缀已确认：`10#`。

```
前缀 10# → market = GLOBAL, exchange = null, assetType = FX_RATE（新增枚举）
代码格式、币种、价格精度、成交量及金额语义均未验证，继续保留在待分类表。
```

### 中证指数（类型 11）

前缀已确认：`62#`。

```
前缀 62# → market = CN, exchange = CSI, assetType = INDEX
代码固定为 6 位数字，已按 `CN/CSI/INDEX` 入库；与沪市 `000xxx` 是否重叠仍作为跨来源去重问题处理，不用文件名前缀猜测合并。
```

注意：中证指数也可能部分存在于 `vipdoc/sh/lday/` 中（以 `000` 开头的 6 位代码，如 `sh000300.day`）。`ds/62#` 和 `sh/000` 两处的中证指数可能有重叠或互补关系，需要按 `canonicalInstrumentId` 与来源时间处理，不能仅用代码合并。

```powershell
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\62#*.day | Select-Object -First 20 | ForEach-Object { $_.Name }
```

### 国证指数（类型 12）

前缀已确认：`102#`。

```
前缀 102# → market = CN, exchange = CNI, assetType = INDEX
代码固定为 6 位数字，已按 `CN/CNI/INDEX` 入库。
```

注意：国证指数也可能部分存在于 `vipdoc/sz/lday/` 中（以 `399` 开头的 6 位代码，如 `sz399001.day`）。与中证指数同理，需要去重。

### 华证指数（类型 13）

前缀已确认：`69#`。

```
前缀 69# → market = CN, exchange = HUAZHENG, assetType = INDEX
代码固定为 6 位数字，已按 `CN/HUAZHENG/INDEX` 入库。
```

### 宏观指标（类型 14）

前缀已确认：`38#`。

```
前缀 38# → market = CN 或 GLOBAL, exchange = null, assetType = MACRO
代码格式待确认（请运行命令查看样例）
```

## 历史设计草案（非当前规范，请勿执行）

> 以下内容保留为 2026-08-29 的审计与设计演变证据。其中的前缀、枚举、代码路径、Top-N 数量、`00W/00Y` 主连推断和“待实现”表述均可能与当前实现不一致。现行规范是本文前半部分的实施状态、`market_classification.json`、`tdx_local.py`、`Plan_R4.md` 与最新审计报告；不要把本节示例配置或操作建议直接用于导入。

### 当时建议的确认步骤（历史）

前缀已于 2026-08-29 由用户比对通达信盘后下载工具确认。仍需确认的细节是各前缀下的**代码格式和后缀格式**，请运行以下命令：

```powershell
# 1. 查看国际期货主连的文件名格式，确认后缀是 00W/00Y 还是其他
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\16#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\17#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\18#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }

# 2. 查看各指数和汇率前缀的代码格式
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\10#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\12#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\27#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\38#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\48#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\62#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\69#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
Get-ChildItem C:\tongdaxin\vipdoc\ds\lday\102#*.day | Select-Object -First 10 | ForEach-Object { $_.Name }
```

把这些命令的输出贴给 Codex，它就能完成代码格式和后缀的最终规则，然后把分类逻辑写进新增的 `tdx_global.py` 适配器。

## 给 Codex 的实现指引

### 不要做的事

1. **不要打开 `.day` / `.lc5` 文件内容来判断市场类型**——二进制记录里没有市场字段
2. **不要把金融终端的 `ds/` 目录和期货通的 `ds/` 目录混为同一个扫描器**——两个终端的同名目录包含完全不同的数据
3. **不要给未确认前缀的文件猜测分类**——未命中规则的文件必须进入待分类审计表（R4-T010 的 `unclassified` 机制）
4. **不要把国际期货的 `00W`/`00Y` 后缀和国内期货的 `L7`/`L8`/`L9` 混用**——两套后缀体系属于不同终端
5. **不要对 `38#` 前缀猜测为港股数据**——`38#` 只用于宏观指标，港股创业板是 `48#`

### 要做的事

1. **在文件发现阶段先判断来源终端**（通过根目录路径 `C:\tongdaxin` vs `C:\new_tdxqh`），再决定用哪套前缀规则
2. **把数字市场前缀 `NN#` 到市场/交易所的映射表写进配置文件**（类似现有 `futures_bulk.py` 的 `_EXCHANGES` 字典），不要硬编码在多处
3. **国际期货主连的后缀 `00W`/`00Y` 单独建立一套 seriesKind 映射**，不与 `L7`/`L8`/`L9` 共用枚举值
4. **港股主板 `31#` 和创业板 `48#` 是不同前缀**，分别直接识别，不需要按代码区间区分
5. **中证 `62#` 和国证 `102#` 指数在 `ds/` 目录中的文件可能与 `sh/`/`sz/` 目录中的 `000`/`399` 开头文件有重叠**，实现时需要按 `instrument_id` 去重，优先保留 `sh`/`sz` 目录的版本（因为它们已被现有代码处理）
6. **汇率和宏观指标需要新增 assetType 枚举值**（`FX_RATE`、`MACRO`），并在 `market_classification.py` 中加入对应的分类路径

### 建议的代码结构

```
tdx_local.py          — 金融终端沪深京证券 + 港股主板31#（已有，扩展 ds/ 扫描范围）
futures_bulk.py       — 期货通国内期货（已有，不变）
tdx_global.py（新增） — 金融终端 ds/ 下非 31# 的全部数据
  ├── 扫描 C:\tongdaxin\vipdoc\ds/ 下所有文件
  ├── 按数字前缀分派：
  │   10# → 汇率（FX_RATE）
  │   12# → 国际指数（INDEX）
  │   16# → COMEX 期货（FUTURE）
  │   17# → NYMEX 期货（FUTURE）
  │   18# → CBOT 期货（FUTURE）
  │   27# → 港股指数（INDEX）
  │   38# → 宏观指标（MACRO）
  │   48# → 港股创业板（STOCK）
  │   62# → 中证指数（INDEX）
  │   69# → 华证指数（INDEX）
  │   102# → 国证指数（INDEX）
  │   其他 → unclassified
  ├── 国际期货：解析后缀（00W/00Y 或其他），确定 seriesKind
  └── 指数/汇率/宏观：按前缀直接分类，不需后缀解析
```

### 配置文件扩展

在 `config/market_classification.json` 中新增（前缀已于 2026-08-29 由用户确认）：

```jsonc
{
  "tdxDsPrefixes": {
    "10":  { "market": "GLOBAL", "assetType": "FX_RATE", "exchange": null, "label": "基本汇率" },
    "12":  { "market": "GLOBAL", "assetType": "INDEX", "exchange": null, "label": "国际指数" },
    "16":  { "market": "GLOBAL", "assetType": "FUTURE", "exchange": "COMEX", "label": "COMEX期货" },
    "17":  { "market": "GLOBAL", "assetType": "FUTURE", "exchange": "NYMEX", "label": "NYMEX期货" },
    "18":  { "market": "GLOBAL", "assetType": "FUTURE", "exchange": "CBOT", "label": "CBOT期货" },
    "27":  { "market": "HK", "assetType": "INDEX", "exchange": "HKEX", "label": "港股指数" },
    "31":  { "market": "HK", "assetType": "STOCK", "exchange": "HKEX", "label": "港股主板" },
    "38":  { "market": "CN", "assetType": "MACRO", "exchange": null, "label": "宏观指标" },
    "48":  { "market": "HK", "assetType": "STOCK", "exchange": "HKEX", "label": "港股创业板" },
    "62":  { "market": "CN", "assetType": "INDEX", "exchange": "CSI", "label": "中证指数" },
    "69":  { "market": "CN", "assetType": "INDEX", "exchange": "HUAZHENG", "label": "华证指数" },
    "102": { "market": "CN", "assetType": "INDEX", "exchange": "CNI", "label": "国证指数" }
  },
  "globalFuturesSuffixes": {
    "00W": "MAIN",
    "00Y": "CONTINUOUS"
  },
  "globalFuturesExchangeMap": {
    "GC": "COMEX", "SI": "COMEX", "PL": "COMEX", "PA": "COMEX", "HG": "COMEX",
    "CL": "NYMEX", "NG": "NYMEX", "HO": "NYMEX", "RB": "NYMEX",
    "ZC": "CBOT", "ZS": "CBOT", "ZW": "CBOT", "ZB": "CBOT", "ZN": "CBOT"
  }
}
```

## 从文件分类到页面显示分类的映射

上文的文件名规则解决的是"入库时如何判断 market/exchange/assetType"。但行情页面（`MarketView.vue`）的筛选下拉框和目标行情按钮栏用的是另一套**显示分类**（display category），由 `market_classification.py` 的 `classify_market()` 函数从标的的标准字段推导。两套分类的关系是：文件名规则 → 标准字段 → 显示分类。

### 当前 26 个显示分类

`market_classification.json` 已定义的分类（`MarketView.vue` 的 `fallbackCategories` 做了镜像）：

| 分类 ID | 显示标签 | 覆盖范围 |
| --- | --- | --- |
| `all` | 全部市场 | 虚拟分类，匹配所有标的 |
| `a-index` | A股-指数 | 沪深京权益指数（000/399/899 等前缀） |
| `tdx-industry-index` | 通达信-行业板块指数 | 881 前缀 |
| `tdx-board-index` | 通达信-综合板块指数 | 880 前缀 |
| `a-sh` | A股-沪市 | 600/601/603/605 |
| `a-sz` | A股-深市 | 000/001/002/003 |
| `a-bse` | A股-北证 | 920/43/83/87/88 |
| `a-chinext` | A股-创业板 | 300/301/302 |
| `a-star` | A股-科创板 | 688/689 |
| `a-etf` | A股-ETF基金 | 510-519 等 |
| `a-convertible` | A股-可转债 | 110/113/123 等 |
| `a-exchangeable` | A股-可交债 | 132/120 |
| `a-pledged-repo` | A股-债券通用质押式回购 | 204/1318 |
| `a-repo` | A股-债券回购 | 201-207/131 |
| `a-lof` | A股-LOF基金 | 500-506/16 |
| `a-reit` | A股-REITs | 508/180/181 |
| `b-sh` | B股-沪市 | 900（USD） |
| `b-sz` | B股-深市 | 200（HKD） |
| `hk-index` | 港股-指数 | 港股非数字代码或 assetType=INDEX |
| `hk-stock` | 港股-个股 | 31# 前缀 5 位数字 |
| `global-index` | 全球-指数 | `market=GLOBAL` 且 `assetType=INDEX` |
| `global-future` | 全球-期货 | `market=GLOBAL` 且 `assetType=FUTURE`；连续/主连语义仍可能未知 |
| `cn-future-index` | 国内期货-指数 | 商品指数（42# 等） |
| `cn-future-cffex` | 国内期货-中金所 | 47# 前缀 IF/IH/IC/IM 等 |
| `cn-future-commodity` | 国内期货-商品期货 | 28#/29#/30#/66# 商品期货 |
| `cn-future-night` | 国内期货-商品期货夜盘 | 虚拟分类，按夜盘时段匹配 |

### 尚未纳入当前公开筛选器的提议分类

下表是早期的展示分类提议，不是当前待办的完成结论。已验证的全球指数/期货使用较中性的 `global-index` / `global-future`；外盘期货不标“主连”。FX、宏观以及三个指数编制方的独立筛选器需要先补齐字段语义、来源与产品要求，当前不得靠此表把待分类数据提升到正式库。

| 拟新增分类 ID | 显示标签 | 覆盖范围 | 对应盘后数据类型 | classify_market 路由条件 |
| --- | --- | --- | --- | --- |
| `intl-future` | 国际期货-主连 | COMEX/NYMEX/CBOT 主连 | 类型 6、7、8 | `market == "GLOBAL"` 且 `assetType == "FUTURE"` |
| `intl-index` | 国际指数 | 道琼斯/标普/纳斯达克等 | 类型 9 | `market == "GLOBAL"` 且 `assetType == "INDEX"` |
| `fx-rate` | 基本汇率 | 美元/欧元/日元等汇率 | 类型 10 | `assetType == "FX_RATE"`（新增枚举） |
| `macro-indicator` | 宏观指标 | GDP/CPI/利率等 | 类型 14 | `assetType == "MACRO"` 且非交易所行情 |
| `csi-index` | 中证指数 | 中证编制的指数 | 类型 11 | `market == "CN"` + `assetType == "INDEX"` + 代码在 `csiIndexPrefixes` 列表 |
| `cni-index` | 国证指数 | 国证编制的指数 | 类型 12 | `market == "CN"` + `assetType == "INDEX"` + 代码在 `cniIndexPrefixes` 列表 |
| `huazheng-index` | 华证指数 | 华证编制的指数 | 类型 13 | `market == "CN"` + `assetType == "INDEX"` + 代码在 `huazhengIndexPrefixes` 列表 |

### 显示分类的优先级顺序

`classify_market()` 是一棵优先级决策树，新增分类必须按以下顺序插入，否则会被上层规则截获：

1. `market == "HK"` → `hk-index` / `hk-stock`（已有，不变）
2. `market == "GLOBAL"` 且 `assetType == "FUTURE"` → `intl-future`（新增）
3. `market == "GLOBAL"` 且 `assetType == "INDEX"` → `intl-index`（新增）
4. `market == "GLOBAL"` 且 `assetType == "FX_RATE"` → `fx-rate`（新增）
5. `market == "GLOBAL"` 且 `assetType == "MACRO"` → `macro-indicator`（新增）
6. `market == "CN"` 且期货 → `cn-future-*`（已有，不变）
7. `market == "CN"` 且 `assetType == "INDEX"` → 先查 `csiIndexPrefixes` / `cniIndexPrefixes` / `huazhengIndexPrefixes`，命中则返回 `csi-index` / `cni-index` / `huazheng-index`，未命中则回退 `a-index`（新增子路由）
8. `market == "CN"` 且 A 股 → `a-sh` / `a-sz` / `a-etf` 等（已有，不变）
9. 其他 → `unclassified`

关键约束：第 7 步的指数提供商区分必须在现有 `a-index` 回退**之前**判断，否则所有指数都会被 `a-index` 截获。但中证/国证/华证的代码前缀有重叠（例如中证 000 和上证指数 000 共用前缀），因此不能只靠前缀区分，需要用 `indexAllowlist` 风格的精确代码列表或 `indexProvider` 元数据字段。

### 指数提供商区分的三种方案

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| A. 精确代码列表 | 在 JSON 中维护中证/国证/华证各自的完整指数代码列表 | 最准确，不依赖前缀 | 列表长，需随上游新增指数更新 |
| B. 代码前缀为主 + 允许列表兜底 | 中证用 `000`/`930`-`932` 前缀，国证用 `399` 前缀，华证用独立前缀（待确认），重叠代码用 allowlist 区分 | 列表短 | 前缀重叠时仍需 allowlist |
| C. 入库时写入 `indexProvider` 字段 | 在文件导入阶段就查一个 `code → provider` 映射表，把 `indexProvider` 写入标的元数据 | 分离关注点，分类器只读字段 | 需要维护映射表，首次导入需补数据 |

建议采用**方案 B**：用前缀做主路由，对重叠代码用 allowlist。中证指数大多数以 `000` 开头（沪深 300 是 `000300.SH`），国证指数大多数以 `399` 开头（深证成指是 `399001.SZ`），这两套前缀基本不重叠。华证指数的前缀需要确认（可能是 `930`-`932`，当前已在 `indexPrefixes.SSE` 中但未区分提供商）。

### 配置文件扩展（market_classification.json）

```jsonc
{
  "categories": [
    // ... 已有 24 个 ...
    { "id": "intl-future", "label": "国际期货-主连" },
    { "id": "intl-index", "label": "国际指数" },
    { "id": "fx-rate", "label": "基本汇率" },
    { "id": "macro-indicator", "label": "宏观指标" },
    { "id": "csi-index", "label": "中证指数" },
    { "id": "cni-index", "label": "国证指数" },
    { "id": "huazheng-index", "label": "华证指数" }
  ],
  "csiIndexPrefixes": ["000", "930", "931", "932"],
  "csiIndexAllowlist": ["000001", "000300", "000688", "000905", "000852"],
  "cniIndexPrefixes": ["399"],
  "cniIndexAllowlist": ["399001", "399006", "399300", "399005", "399330"],
  "huazhengIndexPrefixes": [],
  "huazhengIndexAllowlist": [],
  "tdxGlobalPrefixes": {
    "68": { "market": "GLOBAL", "assetType": "FUTURE", "label": "国际期货" },
    "102": { "market": "GLOBAL", "assetType": "INDEX", "label": "国际指数" }
  },
  "globalFuturesSuffixes": {
    "00W": "MAIN",
    "00Y": "CONTINUOUS"
  },
  "globalFuturesExchangeMap": {
    "GC": "COMEX", "SI": "COMEX", "PL": "COMEX", "PA": "COMEX", "HG": "COMEX",
    "CL": "NYMEX", "NG": "NYMEX", "HO": "NYMEX", "RB": "NYMEX",
    "ZC": "CBOT", "ZS": "CBOT", "ZW": "CBOT", "ZB": "CBOT", "ZN": "CBOT"
  }
}
```

### 前端同步修改（提议，未纳入当前实现）

`MarketView.vue` 需要同步更新两处：

1. **`fallbackCategories` 数组**（约第 36-41 行）：追加 7 个新分类的 `{ id, label }` 对象，顺序与 JSON 一致。这是 API 不可用时的降级列表。

2. **`marketText()` 显示函数**：当标的的 `classify_market()` 返回新增分类时，行情列表的"市场类型"列需要显示对应的中文标签。当前 `marketText()` 返回 `formatMarket() + " · " + formatAssetType()`，对于国际期货应显示"国际期货 · COMEX 主连"而非"全球 · 期货"。

3. **`api.ts` 的 `formatAssetType`**：需要新增 `FX_RATE: "汇率"` 和 `MACRO_INDICATOR: "宏观指标"`（当前 `MACRO: "宏观指标"` 已存在但枚举值不匹配）。

4. **目标行情按钮栏**：新增分类会自动出现在按钮栏中（它遍历 `categories` 数组），无需额外代码。但如果分类总数超过 30 个，需要检查按钮栏的换行布局是否仍然可用。

### 行情页面的分类显示逻辑总结

行情页面有三种地方涉及分类显示：

**A. 全部行情下拉筛选器**（`el-select`，约第 477 行）
- 用户选择一个分类 → `loadAll()` 把 `categoryKey` 传给 `/api/market/instruments?category=xxx`
- 后端 `matches_market_category()` 用 `classify_market(item)` 逐个匹配
- 新增分类自动出现在下拉框中（遍历 `categories` 数组），无需额外代码

**B. 目标行情按钮栏**（button tab nav，约第 448 行）
- 多选切换，用户点击多个分类按钮 → `loadTargets()` POST 到 `/api/strategy/matches`
- 新增分类自动出现在按钮栏中，但按钮总数从 24 增加到 31，需要验证窄屏布局

**C. 行情列表"市场类型"列**（约第 148 行 `marketText()`）
- 每行显示 `formatMarket(market) + " · " + formatAssetType(assetType)`
- 对于新增分类，需要扩展 `marketText()` 或 `formatMarket()` 使其能显示"国际期货"、"汇率"等标签，而不是只显示"全球"

### 不需要新增显示分类的情况

- **港股指数（类型 3）**：`hk-index` 已存在，只要入库时把港股指数的 `assetType` 设为 `INDEX`，`classify_market()` 会自动路由到 `hk-index`。问题是当前港股指数在通达信中的文件名前缀未确认，确认后只需在 `tdx_local.py` 中新增发现规则，不需要新增显示分类。

- **港股主板 vs 创业板（类型 4 vs 5）**：当前都归入 `hk-stock`。如果需要在行情页面区分主板和创业板，可以在 `market_classification.json` 中新增 `hk-gem` 分类，用代码区间 `80000`-`89999` 匹配；但这不是通达信文件名规则，而是港股市场惯例，属于可选增强而非必需。

## 验收标准

### 文件入库分类

1. 对 14 类数据中的每一类，程序都能通过终端 + 目录 + 文件名前缀/后缀正确判断 market / exchange / assetType / seriesKind，不需要打开文件内容。
2. 已处理的类型 1、2、4、5 和国内期货保持现有行为不变。
3. 新增类型的文件不落入 `unclassified` 审计表（除非规则确实未覆盖）。
4. 未确认前缀的文件继续进入 `unclassified`，不猜测分类。
5. 国际期货的 `00W`/`00Y` 后缀与国内期货的 `L7`/`L8`/`L9` 后缀独立映射，不混用。
6. 新增配置项集中在 `market_classification.json`，不在代码中散落硬编码。
7. `test_market_classification.py` 和 `test_tdx_local.py` 新增对应夹具用例。

### 页面显示分类

8. `market_classification.json` 新增 7 个分类后，`/api/market/categories` 返回 31 个分类（含 `all`）。
9. `MarketView.vue` 的 `fallbackCategories` 数组与 JSON 同步，数量和顺序一致。
10. `classify_market()` 的决策树优先级正确：`GLOBAL` 路由在 `CN` 路由之前；`csi-index`/`cni-index`/`huazheng-index` 子路由在 `a-index` 回退之前。
11. 行情页面下拉筛选器选择"国际期货-主连"时，只返回 `market == "GLOBAL"` 且 `assetType == "FUTURE"` 的标的；其他新分类同理。
12. 行情列表"市场类型"列对新增分类显示中文标签（如"国际期货 · COMEX 主连"），不显示"全球 · 期货"。
13. 目标行情按钮栏在 31 个分类下窄屏布局正常，按钮可换行或横向滚动。
14. `api.ts` 的 `formatAssetType` 新增 `FX_RATE` 和 `MACRO_INDICATOR` 标签。
15. `test_market_classification.py` 新增 7 个分类的夹具用例，覆盖路由优先级和未命中回退。
