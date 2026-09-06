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
- `98#`：在 `ds/fzline/` 中发现 2 个文件（`98#02261F.lc5`、`98#02261H.lc5`）。用户在通达信社区查到 `02261F` 和 `02261H` 均指向港股"拿森科技"，可能是已废弃的旧代码。此前缀不列入 14 类盘后数据，继续待分类，不导入。

### 金融终端 ds/ 前缀总表（用户确认版）

| 前缀 | 数据类型 | market | exchange | assetType | 文件名代码格式 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `10#` | 基本汇率 | GLOBAL | — | FX_RATE | 6 位货币对（如 `USDJPY`） | 16 个文件，新增枚举 |
| `12#` | 国际指数 | GLOBAL | — | INDEX | 字母数字混合（如 `A_IXIC`、`B_DAX`、`NK0Y`） | 部分带 A_/B_/C_/D_ 地区前缀 |
| `16#` | COMEX 期货 | GLOBAL | COMEX | FUTURE | 产品代码+`00W`/`00Y` | `00W`=主连，`00Y`=连续 |
| `17#` | NYMEX 期货 | GLOBAL | NYMEX | FUTURE | 产品代码+`00W`/`00Y` | 同上 |
| `18#` | CBOT 期货 | GLOBAL | CBOT | FUTURE | 产品代码+`00W`/`00Y` | 同上，含农产品和国债品种 |
| `27#` | 港股指数 | HK | HKEX | INDEX | 字母或字母数字（如 `HSI`、`CES100`、`HZ5014`） | 331 个文件 |
| `31#` | 港股主板 | HK | HKEX | STOCK | 5 位数字 | ✅ 已处理 |
| `38#` | 宏观指标 | CN | — | MACRO | `N_XXX` 格式（如 `2_CPI`、`5_M2`、`9_MLF3M`） | 214 个文件 |
| `48#` | 港股创业板 | HK | HKEX | STOCK | 5 位数字（`08` 开头） | 339 个文件 |
| `62#` | 中证指数 | CN | CSI | INDEX | 6 位数字 | 与 `sh/000` 部分重叠，两者都保留 |
| `69#` | 华证指数 | CN | HUAZHENG | INDEX | 6 位数字 | |
| `102#` | 国证指数 | CN | CNI | INDEX | 6 位数字 | 与 `sz/399` 部分重叠，两者都保留 |

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

### COMEX/NYMEX/CBOT 期货序列（类型 6、7、8）

前缀已确认：`16#` = COMEX，`17#` = NYMEX，`18#` = CBOT。

实际文件名已于 2026-08-29 从 `C:\tongdaxin\vipdoc\ds\lday` 确认，后缀为 `00W` 和 `00Y`：

| 前缀 | 后缀 | 文件名样例 | 含义 |
| --- | --- | --- | --- |
| `16#` | `00W` | `16#GC00W.day`、`16#HG00W.day`、`16#SI00W.day`、`16#EHR00W.day` | COMEX 品种主连 |
| `16#` | `00Y` | `16#GC00Y.day`、`16#HG00Y.day`、`16#SI00Y.day`、`16#EHR00Y.day` | COMEX 品种连续 |
| `17#` | `00W` | `17#CL00W.day`、`17#NG00W.day`、`17#HO00W.day`、`17#RB00W.day`、`17#PA00W.day`、`17#PL00W.day`、`17#BZ00W.day` | NYMEX 品种主连 |
| `17#` | `00Y` | `17#CL00Y.day`、`17#NG00Y.day`、`17#HO00Y.day`、`17#RB00Y.day`、`17#PA00Y.day`、`17#PL00Y.day`、`17#BZ00Y.day` | NYMEX 品种连续 |
| `18#` | `00W` | `18#ZC00W.day`、`18#ZS00W.day`、`18#ZW00W.day`、`18#ZM00W.day`、`18#ZO00W.day`、`18#ZR00W.day`、`18#ZL00W.day`、`18#TY00W.day`、`18#US00W.day`、`18#TU00W.day`、`18#FV00W.day`、`18#UL00W.day` | CBOT 品种主连 |
| `18#` | `00Y` | `18#ZC00Y.day`、`18#ZS00Y.day`、`18#ZW00Y.day`、`18#ZM00Y.day`、`18#ZO00Y.day`、`18#ZR00Y.day`、`18#ZL00Y.day`、`18#TY00Y.day`、`18#US00Y.day`、`18#TU00Y.day`、`18#FV00Y.day`、`18#UL00Y.day` | CBOT 品种连续 |

用户确认：这些不是"国际期货"，就是 COMEX/NYMEX/CBOT 三个交易所的期货品种。特殊合约就两种（`00W` 和 `00Y`），剩下的都是月份合约。

**分类规则**：

```
前缀 16# → market = GLOBAL, exchange = COMEX, assetType = FUTURE
前缀 17# → market = GLOBAL, exchange = NYMEX, assetType = FUTURE
前缀 18# → market = GLOBAL, exchange = CBOT,  assetType = FUTURE
后缀 00W → seriesKind = MAIN（主连）
后缀 00Y → seriesKind = CONTINUOUS（连续）
月份合约（如 16#GC2609）→ seriesKind = CONTRACT
```

**产品代码**（从实际文件名提取）：

| 交易所 | 前缀 | 产品代码 | 产品名称 |
| --- | --- | --- | --- |
| COMEX | `16#` | `GC` | 黄金 |
| COMEX | `16#` | `SI` | 白银 |
| COMEX | `16#` | `HG` | 铜 |
| COMEX | `16#` | `EHR` | ？（待确认） |
| NYMEX | `17#` | `CL` | 原油 |
| NYMEX | `17#` | `NG` | 天然气 |
| NYMEX | `17#` | `HO` | 取暖油 |
| NYMEX | `17#` | `RB` | 汽油 |
| NYMEX | `17#` | `PA` | 钯金 |
| NYMEX | `17#` | `PL` | 铂金 |
| NYMEX | `17#` | `BZ` | 布伦特原油 |
| CBOT | `18#` | `ZC` | 玉米 |
| CBOT | `18#` | `ZS` | 大豆 |
| CBOT | `18#` | `ZW` | 小麦 |
| CBOT | `18#` | `ZM` | 豆粕 |
| CBOT | `18#` | `ZO` | 燕麦 |
| CBOT | `18#` | `ZR` | 稻谷 |
| CBOT | `18#` | `ZL` | 豆油 |
| CBOT | `18#` | `TY` | 10年期国债 |
| CBOT | `18#` | `US` | 30年期国债 |
| CBOT | `18#` | `TU` | 2年期国债 |
| CBOT | `18#` | `FV` | 5年期国债 |
| CBOT | `18#` | `UL` | 超长期国债 |

### 港股指数（类型 3）

前缀已确认：`27#`。实际文件名已于 2026-08-29 确认。

文件名样例：`27#HSI.day`（恒生指数）、`27#HKL.day`、`27#GEM.day`、`27#CES100.day`、`27#CES300.day`、`27#HZ5014.day` 等，共 331 个文件。代码格式为字母或字母数字混合（如 `HSI`、`CES100`、`HZ5014`），不是纯数字。

```
前缀 27# → market = HK, exchange = HKEX, assetType = INDEX
代码为字母或字母数字混合
```

### 港股创业板（类型 5）

前缀已确认：`48#`。实际文件名已于 2026-08-29 确认。

文件名样例：`48#08048.day`、`48#08598.day`、`48#08003.day` 等，共 339 个文件。代码为 5 位数字，以 `08` 开头（港股创业板代码区间为 08001-08999）。

```
前缀 48# → market = HK, exchange = HKEX, assetType = STOCK
代码为 5 位数字
```

### 国际指数（类型 9）

前缀已确认：`12#`。实际文件名已于 2026-08-29 确认。

文件名样例：`12#A_IXIC.day`（纳斯达克指数）、`12#A_NDX.day`（纳斯达克100）、`12#A_SOX.day`（费城半导体）、`12#B_DAX.day`（德国DAX）、`12#B_OMXS30.day`（瑞典OMXS30）、`12#C_NQHK.day`、`12#NK0Y.day`（日经225连续）等。代码格式为 `A_`/`B_`/`C_`/`D_` 前缀+字母数字，或直接字母数字如 `NK0Y`、`CNY0`。

```
前缀 12# → market = GLOBAL, exchange = null, assetType = INDEX
代码为字母数字混合，部分带 A_/B_/C_/D_ 地区前缀
```

### 基本汇率（类型 10）

前缀已确认：`10#`。实际文件名已于 2026-08-29 确认。

文件名样例：`10#AUDUSD.day`、`10#EURUSD.day`、`10#GBPUSD.day`、`10#USDJPY.day`、`10#USDCNY.day`、`10#CNHUSD.day`、`10#HKDCNH.day` 等，共 16 个文件。代码格式为 6 位货币对代码（如 `USDJPY` = 美元日元）。

```
前缀 10# → market = GLOBAL, exchange = null, assetType = FX_RATE
代码为 6 位货币对（XXXYYY 格式）
```

### 中证指数（类型 11）

前缀已确认：`62#`。

```
前缀 62# → market = CN, exchange = CSI, assetType = INDEX
代码固定为 6 位数字，已按 `CN/CSI/INDEX` 入库；与沪市 `000xxx` 是否重叠仍作为跨来源去重问题处理，不用文件名前缀猜测合并。
```

注意：用户确认 `sh/lday/sh000300.day` 和 `ds/lday/62#000300.day` 是同一个指数（沪深 300），价格等数据完全一样，前者来自上交所、后者来自中证指数公司。用户认为两者都分类保留即可，不需要去重——它们来源不同（`SSE` vs `CSI`），`canonicalInstrumentId` 自然不同。

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

前缀已确认：`38#`。实际文件名已于 2026-08-29 确认，共 214 个文件。

代码格式为 `数字_字母` 模式，数字前缀代表宏观指标分类，字母为指标缩写。样例：

| 代码 | 文件名 | 推测含义 |
| --- | --- | --- |
| `1_GDP` | `38#1_GDP.day` | GDP |
| `1_GDPI` | `38#1_GDPI.day` | GDP 指数 |
| `1_MSR` | `38#1_MSR.day` | ？ |
| `2_CPI` | `38#2_CPI.day` | 消费者物价指数 |
| `2_PPI` | `38#2_PPI.day` | 生产者物价指数 |
| `2_CGPI` | `38#2_CGPI.day` | 企业商品价格指数 |
| `2_PPPI` | `38#2_PPPI.day` | ？ |
| `2_PPCI` | `38#2_PPCI.day` | ？ |
| `3_PMI` | `38#3_PMI.day` | 采购经理指数 |
| `3_PMIN` | `38#3_PMINH.day` | 非制造业 PMI |
| `3_PMIH` | `38#3_PMIH.day` | ？ |
| `3_BCI` | `38#3_BCI.day` | 信心指数 |
| `3_CCI` | `38#3_CCI.day` | 消费者信心指数 |
| `4_CBEC` | `38#4_CBEC.day` | 跨境电商 |
| `4_TIE` | `38#4_TIE.day` | 贸易进出口 |
| `4_MEI` | `38#4_MEI.day` | 进口 |
| `4_MEU` | `38#4_MEU.day` | 出口 |
| `5_M2` | `38#5_M2.day` | M2 货币供应 |
| `5_M1` | `38#5_M1.day` | M1 货币供应 |
| `5_M0` | `38#5_M0.day` | M0 货币供应 |
| `5_GOLD` | `38#5_GOLD.day` | 黄金储备 |
| `5_FER` | `38#5_FER.day` | 外汇储备 |
| `5_SHIBOR` | `38#5_SHIBOR.day` | 上海银行间同业拆放利率 |
| `5_BDI` | `38#5_BDI.day` | 波罗的海干散货指数 |
| `5_DDR` | `38#5_DDR.day` | ？ |
| `6_BTCUSD` | `38#6_BTCUSD.day` | 比特币 |
| `6_ETHUSD` | `38#6_ETHUSD.day` | 以太坊 |
| `6_EPI` | `38#6_EPI.day` | ？ |
| `6_ERAIL` | `38#6_ERAIL.day` | ？ |
| `8_USDEBTGDP` | `38#8_USDEBTGDP.day` | 美国债务/GDP |
| `9_OMOR1D` | `38#9_OMOR1D.day` | 公开市场操作 1 天 |
| `9_MLF3M` | `38#9_MLF3M.day` | 中期借贷便利 3 个月 |
| `9_SLF1D` | `38#9_SLF1D.day` | 常备借贷便利 1 天 |
| `9_GKDC2M` | `38#9_GKDC2M.day` | 国库现金定存 2 个月 |
| `5_TSR` | `38#5_TSR.day` | ？ |
| `5_TRD` | `38#5_TRD.day` | 贸易差额 |
| `5_RMBUS` | `38#5_RMBUS.day` | 人民币兑美元 |
| `5_XAG` | `38#5_XAG.day` | 白银 |
| `5_STAMP` | `38#5_STAMP.day` | 印花税 |
| `5_TAX` | `38#5_TAX.day` | 税收 |

数字前缀的分类含义（推测）：
- `1_` = GDP 及总量指标
- `2_` = 物价指数（CPI/PPI/CGPI）
- `3_` = PMI 及信心指数
- `4_` = 贸易及进出口
- `5_` = 货币/金融/大宗商品/储备
- `6_` = 加密货币及其他
- `8_` = 国际宏观
- `9_` = 央行货币政策工具（OMO/MLF/SLF/国库定存）

```
前缀 38# → market = CN, exchange = null, assetType = MACRO
代码格式为 N_XXX（数字分类前缀 + 下划线 + 指标缩写）
```

## 交易所官方代码区间分配（2026-08-29 用户上传官方表确认）

用户从上交所、深交所官网下载了官方证券代码区间分配表，从指数公司官网下载了完整指数列表。以下是与现有 `market_classification.json` 配置的比对结果。

### 上交所代码区间（334 行官方表摘要）

官方表按"第 1 位 + 第 2-3 位"描述代码区间。以下仅列出与 K 级行情相关的区间（排除债券发行、申购配号、网络投票等非行情代码）：

| 第 1 位 | 第 2-3 位 | 业务定义 | 对应前缀 | 现有配置 | 差异 |
| --- | --- | --- | --- | --- | --- |
| 0 | 00 | 上证指数系列、中证指数系列 | `000` | ✅ indexPrefixes.SSE | 一致 |
| 1 | 10 | 可转换公司债券 | `110` | ✅ convertibleBondPrefixes.SSE | 一致 |
| 1 | 11 | 可转换公司债券 | `111` | ✅ | 一致 |
| 1 | 13 | 可转换公司债券 | `113` | ✅ | 一致 |
| 1 | 18 | 科创板可转换公司债券 | `118` | ✅ | 一致 |
| 1 | 26 | 分离交易的可转换公司债券 | `126` | ✅ | 一致 |
| 1 | 32 | 可交换公司债券 | `132` | ✅ exchangeableBondPrefixes.SSE | 一致 |
| 2 | 04 | 债券质押式回购 | `204` | ✅ pledgedRepoPrefixes.SSE | 一致 |
| 2 | 01-07 | 债券回购 | `201`-`207` | ✅ repoPrefixes.SSE | 一致 |
| 5 | 00-02,05,06 | 契约型封闭式基金/LOF | `500`-`506` | ✅ lofPrefixes.SSE | 一致 |
| 5 | 08 | 公募 REITs | `508` | ✅ reitPrefixes.SSE | 一致 |
| 5 | 10-19 | ETF | `510`-`519` | ✅ etfPrefixes.SSE | 一致 |
| 5 | 20 | 跨境 ETF | `520` | ✅ | 一致 |
| 5 | 26 | 跨境 ETF | `526` | ✅ | 一致 |
| 5 | 30 | 沪市指数 ETF | `530` | ✅ | 一致 |
| 5 | 50 | 基金 | `550` | ❌ 未列入 etfPrefixes | **遗漏**：`550` 是债券 ETF，应加入 etfPrefixes.SSE |
| 5 | 51 | 债券 ETF | `551` | ❌ 未列入 | **遗漏**：`551` 是债券 ETF，应加入 |
| 5 | 60-63 | 跨市场 ETF | `560`-`563` | ✅ | 一致 |
| 5 | 80 | 权证/ETF | `580` | ❌ 未列入 | **遗漏**：`580` 是 ETF，应加入 |
| 5 | 81 | 跨市场 ETF | `581` | ✅ | 一致 |
| 5 | 87-89 | 科创板 ETF | `587`-`589` | ✅ | 一致 |
| 6 | 00,01,03,05 | 主板 A 股 | `600`-`605` | ✅ mainBoardPrefixes.SSE | 一致 |
| 6 | 88 | 科创板股票 | `688` | ✅ starPrefixes | 一致 |
| 6 | 89 | 科创板存托凭证 | `689` | ✅ starPrefixes | 一致 |
| 9 | 00 | B 股 | `900` | ✅ mainBoardPrefixes.SSE | 一致 |
| 8 | 88 | 标准券 | `888` | ❌ 未处理 | 非行情代码，暂不处理 |

**上交所遗漏总结**：`550`、`551`、`580` 三个 ETF 前缀在官方表中存在但在 `market_classification.json` 的 `etfPrefixes.SSE` 中缺失。如果你的目录中暂时没有这些前缀的文件，不影响当前运行，但应该补全配置以防未来下载后误分类。

### 深交所代码区间（98 行官方表摘要）

| 第 1 位 | 第 2 位 | 第 3 位 | 业务定义 | 对应前缀 | 现有配置 | 差异 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 0-4 | 主板 A 股 | `000`-`004` | ✅ mainBoardPrefixes.SZSE | 一致 |
| 0 | 3 | 0-2 | 主板 A 股认购权证 | `030`-`032` | ❌ 未处理 | 权证，非行情，暂不处理 |
| 1 | 2 | 3 | 创业板可转换公司债券 | `123` | ✅ convertibleBondPrefixes.SZSE | 一致 |
| 1 | 2 | 4 | 主板/创业板定向可转债 | `124` | ✅ | 一致 |
| 1 | 2 | 7-8 | 主板可转换公司债券 | `127`-`128` | ✅ | 一致 |
| 1 | 2 | 0 | 可交换公司债券 | `120` | ✅ exchangeableBondPrefixes.SZSE | 一致 |
| 1 | 3 | 1 | 债券回购 | `131` | ✅ repoPrefixes.SZSE | 一致 |
| 1 | 3 | 1(8) | 通用质押式回购 | `1318` | ✅ pledgedRepoPrefixes.SZSE | 一致 |
| 1 | 5 | 8-9 | ETF | `158`-`159` | ✅ etfPrefixes.SZSE | 一致 |
| 1 | 6 | 0-9 | 开放式基金（LOF） | `160`-`169` | ✅ lofPrefixes.SZSE (`"16"`) | 一致 |
| 1 | 7 | 0-9 | 开放式基金 | `170`-`179` | — | 用户确认：此区间为备用，不代表已有 LOF，`16` 不够用时再启用，当前无需补入 |
| 1 | 8 | 0-1 | 不动产基金/REITs | `180`-`181` | ✅ reitPrefixes.SZSE | 一致 |
| 2 | 0 | 0-9 | B 股 | `200` | ✅ mainBoardPrefixes.SZSE | 一致 |
| 3 | 0 | 0-9 | 创业板股票 | `300`-`309` | ✅ chinextPrefixes (`300`/`301`/`302`) | **部分遗漏**：官方表显示 `300`-`309` 全区间为创业板，但配置只列了 `300`/`301`/`302` |
| 3 | 9 | 9 | 指数 | `399` | ✅ indexPrefixes.SZSE | 一致 |
| 9 | 7 | 0-1 | 交易系统转发国证指数 | `970`-`971` | ❌ 未处理 | **新发现**：见下文国证指数分析 |
| 9 | 8 | 0-9 | 交易系统转发国证指数 | `980`-`989` | ❌ 未处理 | **新发现**：见下文国证指数分析 |

**深交所遗漏总结**：
1. `300`-`309` 全区间都是创业板股票，但配置只列了 `300`/`301`/`302`，缺少 `303`-`309`。不过实际上目前创业板代码只分配到 `301xxx`，`302`-`309` 尚未启用，保留现有配置即可。
2. `170`-`179` 在官方表中标注为"开放式基金"，用户确认此区间为备用，当前不代表已有 LOF，`16` 不够用时再启用，无需补入配置。
3. `970`-`971` 和 `980`-`989` 是深交所交易系统转发的国证指数——这是一个重要发现，详见下文。

### 中证指数代码格式（2984 行官方指数列表）

中证指数公司官网列表包含 2984 条指数，代码前缀分布：

| 前缀 | 数量 | 代码格式 | 说明 |
| --- | --- | --- | --- |
| `931` | 851 | 6 位数字 | 中证指数主体系 |
| `932` | 589 | 6 位数字 | 中证指数主体系 |
| `H30` | 328 | H+5 位数字 | 中证跨市场指数（如 H30007 芯片产业） |
| `950` | 312 | 6 位数字 | 中证指数主体系 |
| `000` | 300 | 6 位数字 | 与上证系列共用前缀（如 000001 上证指数、000300 沪深 300） |
| `930` | 260 | 6 位数字 | 中证指数主体系 |
| `H11` | 119 | H+5 位数字 | 中证 AMAC 行业指数（如 H11030 AMAC 农林） |
| `L11` | 83 | L+5 位数字 | 中证 CN80 系列指数（如 L11150 CN80 能源） |
| `H50` | 69 | H+5 位数字 | 中证 180 行业指数（如 H50001 180 能源） |
| `399` | 44 | 6 位数字 | 与深证系列共用前缀 |
| `899` | 12 | 6 位数字 | 与北证系列共用前缀 |
| `CES` | 12 | CES+字母数字 | 中华交易服务跨境指数（如 CES100 中华港股通精选 100） |
| `H00` | 2 | H+5 位数字 | 特殊指数 |
| `990` | 1 | 6 位数字 | 特殊指数 |
| `SHH` | 1 | 字母+字母 | 沪港精明指数 |

**关键发现**：
- 中证指数在通达信 `ds/62#` 目录中只可能包含 6 位纯数字代码（`930`/`931`/`932`/`950`/`000`/`399`/`899` 前缀），因为通达信文件名格式限制。
- `H30`/`H11`/`H50`/`L11`/`CES` 等非纯数字代码不可能出现在通达信 `.day` 文件名中（文件名只支持 `NN#XXXXXX` 格式），这些指数如果要在通达信中下载，必然被映射为 6 位数字代码。
- `000` 前缀的中证指数（如 000300 沪深 300）与上证系列指数共用前缀，在 `sh/lday/` 和 `ds/62#` 中可能出现同一指数。这不是错误，是同一指数的不同下载渠道。

### 国证指数代码格式（1464 行官方指数列表）

国证指数公司官网列表包含 1464 条指数，代码前缀分布：

| 前缀 | 数量 | 代码格式 | 说明 |
| --- | --- | --- | --- |
| `399` | 304 | 6 位数字 | 国证主体系（如 399001 深证成指） |
| `CN2` | 237 | CN+字母数字 | 国证跨境/特殊指数（如 CN2001） |
| `980` | 163 | 6 位数字 | 湾区/港股通系列 |
| `983` | 110 | 6 位数字 | 内地/深港通系列 |
| `483` | 102 | 6 位数字 | 内地系列（R 版本） |
| `970` | 79 | 6 位数字 | 港币/美元版本 |
| `CN6` | 67 | CN+字母数字 | 跨境指数 |
| `480` | 59 | 6 位数字 | 湾区系列（R 版本） |
| `987` | 56 | 6 位数字 | 港股系列 |
| `470` | 51 | 6 位数字 | 创业板 ESG 系列（R 版本） |
| `CNB` | 45 | CN+字母数字 | 跨境债券指数 |
| `CN5` | 29 | CN+字母数字 | 跨境指数 |
| `988` | 24 | 6 位数字 | 现金流综合债系列 |
| `921`/`922`/`923` | 21/18/18 | 6 位数字 | 地方债系列（净价/全价/普通） |
| `978` | 12 | 6 位数字 | 创业板信用债系列 |
| `CES` | 12 | CES+字母数字 | 中华交易服务跨境指数 |
| `B10`/`B20`/`B30` | 6/6/6 | B+数字 | 深证地债系列 |

**关键发现**：
- 国证指数使用大量非 `399` 前缀的 6 位数字代码：`470`/`471`/`480`/`483`/`487`/`921`/`922`/`923`/`970`/`971`/`978`/`980`/`983`/`987`/`988`。这些在深交所官方代码分配表中已有说明：`97x` 和 `98x` 前缀是"交易系统转发国证指数"。
- `CN2`/`CN5`/`CN6`/`CNB` 等非纯数字代码不可能出现在通达信 `.day` 文件名中。
- 深交所代码分配表明确写明：`970`-`971` 和 `980`-`989` 是"交易系统转发国证指数"。这意味着这些前缀的国证指数会出现在 `sz/lday/` 目录中（而不是 `ds/102#`）。
- 现有 `market_classification.json` 的 `indexPrefixes.SZSE` 只列了 `399`，缺少 `470`/`480`/`483`/`487`/`921`/`922`/`923`/`970`/`971`/`978`/`980`/`983`/`987`/`988` 等国证指数前缀。

**重要：`tdx_local.py` 的 `_cn_classification` 函数当前只将 `399` 和 `980` 识别为深市指数（第 656 行），其他国证前缀全部落入默认的 `STOCK` 分类。这意味着 `sz970001.day` 会被误分类为深市主板股票而非国证指数。**

### 华证指数代码格式（727 行官方指数列表）

华证指数公司官网列表包含 727 条指数，代码前缀分布：

| 前缀 | 数量 | 代码格式 | 说明 |
| --- | --- | --- | --- |
| `995` | 250 | 6 位数字 | 华证主体系 |
| `993` | 108 | 6 位数字 | 华证增强系列 |
| `999` | 92 | 6 位数字 | 华证核心系列（如 999001 华证 A 指大盘） |
| `997` | 81 | 6 位数字 | 华证行业系列 |
| `T95` | 70 | T+5 位数字 | 全收益版本（如 T93411 沪深 300 增强全收益） |
| `992` | 64 | 6 位数字 | 华证风格系列 |
| `T93` | 33 | T+5 位数字 | 全收益版本 |
| `T99` | 22 | T+5 位数字 | 全收益版本 |
| `998` | 1 | 6 位数字 | 科技创新 1000 |

**关键发现**：
- 华证指数核心前缀是 `992`/`993`/`995`/`997`/`998`/`999`，全部是 6 位纯数字。
- **`999` 前缀**：华证指数大量使用 `999` 前缀（92 条），如 `999001` 华证 A 指大盘。你确认的 `999999`/`999998`/`999997` "上海指数"也是 `999` 前缀。这说明 `999` 前缀在沪市目录（`sh/lday/`）中同时包含上证系列指数和华证指数。
- `T` 开头的代码是全收益版本，不可能出现在通达信 `.day` 文件名中（因为通达信只支持 6 位数字代码）。
- 现有 `market_classification.json` 的 `indexPrefixes.SSE` 已包含 `999`，但这是作为"上证指数"处理的。如果需要区分华证指数和上证系列指数，不能只靠 `999` 前缀——两者共用前缀。需要用精确代码列表区分。

### 三家指数公司代码前缀交叉分析

| 代码前缀 | 中证 | 国证 | 华证 | 在哪个目录 |
| --- | --- | --- | --- | --- |
| `000` | ✅ 300 条 | — | — | `sh/lday/` 和 `ds/62#` |
| `399` | ✅ 44 条 | ✅ 304 条 | — | `sz/lday/` 和 `ds/102#` |
| `899` | ✅ 12 条 | — | — | `bj/lday/` |
| `930`-`932`, `950` | ✅ 大量 | — | — | `ds/62#` |
| `470`-`487`, `921`-`923`, `970`-`988` | — | ✅ 大量 | — | `sz/lday/`（深交所转发） |
| `992`, `993`, `995`, `997`, `998` | — | — | ✅ 大量 | `ds/69#` 或 `sh/lday/` |
| `999` | — | — | ✅ 92 条 | `sh/lday/`（与上证系列共用） |

**核心矛盾**：`000` 前缀同时被中证指数和上证系列指数使用，`399` 前缀同时被中证指数和国证指数使用，`999` 前缀同时被华证指数和上证系列指数使用。**仅靠前缀无法区分指数提供商**，必须用精确代码列表（allowlist）或 `indexProvider` 元数据字段。

### 对现有配置的修复建议

| 修复项 | 优先级 | 说明 |
| --- | --- | --- |
| `etfPrefixes.SSE` 补 `550`/`551`/`580` | 低 | 官方表有，当前目录未见，补全以防未来误分类 |
| `indexPrefixes.SZSE` 补国证前缀 | **高** | `980` 已在 `tdx_local.py` 但配置缺失；`470`/`480`/`483`/`487`/`921`/`922`/`923`/`970`/`971`/`978`/`983`/`987`/`988` 全部缺失 |
| `tdx_local.py` `_cn_classification` 补国证前缀 | **高** | 当前只认 `399` 和 `980` 为深市指数，其他国证前缀全部误分类为 STOCK |
| `lofPrefixes.SZSE` 确认是否补 `170`-`179` | ✅ 无需补入 | 用户确认：备用区间，当前无 LOF，`16` 不够用时再启用 |
| 指数提供商区分 | 中 | `000`/`399`/`999` 前缀被多家共用，需用 allowlist 或 `indexProvider` 字段区分 |
| `tdx_local.py` 和配置同步 `889`/`950`/`999` | ✅ 已修复 | 之前发现的不一致已在配置中补齐 |

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
