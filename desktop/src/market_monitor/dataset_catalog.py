"""Data Catalog：登记所有数据集的注册表（架构调整任务第四节）。

任何新数据（K 线、宏观、派生指标、策略结果）入库前必须先登记 dataset_id。
登记项包含市场、资产类型、频率、来源、更新周期、主键、字段、同步策略与质检规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


BAR_FIELDS = (
    "instrument_id",
    "symbol",
    "name",
    "trading_date",
    "bar_start",
    "bar_end",
    "period",
    "open",
    "high",
    "low",
    "close",
    "pct_change",
    "amplitude",
    "volume",
    "amount",
    "open_interest",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "raw_volume",
    "raw_amount",
    "price_scale",
    "volume_multiplier",
    "volume_unit",
    "normalization_method",
    "normalization_status",
    "normalization_version",
    "currency",
    "adjustment",
    "source",
    "source_period",
    "fetched_at",
    "data_version",
    "quality_status",
)


@dataclass(frozen=True)
class DatasetDefinition:
    """一份数据集登记项。"""

    dataset_id: str
    dataset_name: str
    market: str
    asset_type: str
    frequency: str
    source: str
    update_cycle: str
    primary_key: tuple[str, ...]
    fields: tuple[str, ...]
    sync_policy: str
    quality_rule: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "market": self.market,
            "asset_type": self.asset_type,
            "frequency": self.frequency,
            "source": self.source,
            "update_cycle": self.update_cycle,
            "primary_key": list(self.primary_key),
            "fields": list(self.fields),
            "sync_policy": self.sync_policy,
            "quality_rule": self.quality_rule,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "DatasetDefinition":
        required = ("dataset_id", "dataset_name", "market", "asset_type", "frequency", "source", "update_cycle", "sync_policy", "quality_rule")
        missing = [name for name in required if not document.get(name)]
        if missing:
            raise ValueError(f"dataset definition missing required fields: {sorted(missing)}")
        primary_key = tuple(str(item) for item in document.get("primary_key", ()))
        fields = tuple(str(item) for item in document.get("fields", ()))
        if not primary_key:
            raise ValueError(f"dataset {document['dataset_id']} must declare a primary_key")
        if not fields:
            raise ValueError(f"dataset {document['dataset_id']} must declare fields")
        return cls(
            dataset_id=str(document["dataset_id"]),
            dataset_name=str(document["dataset_name"]),
            market=str(document["market"]),
            asset_type=str(document["asset_type"]),
            frequency=str(document["frequency"]),
            source=str(document["source"]),
            update_cycle=str(document["update_cycle"]),
            primary_key=primary_key,
            fields=fields,
            sync_policy=str(document["sync_policy"]),
            quality_rule=str(document["quality_rule"]),
            description=str(document.get("description", "")),
        )


DEFAULT_DATASETS: tuple[DatasetDefinition, ...] = (
    DatasetDefinition(
        "CN_STOCK_BAR", "A股个股K线", "CN", "STOCK", "1m/5m/15m/30m/1d",
        "jqdata/tushare/akshare/baostock", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "所有 A 股个股最小周期 K 线，大周期由本地聚合",
    ),
    DatasetDefinition(
        "HK_STOCK_BAR", "港股个股K线", "HK", "STOCK", "1m/5m/15m/30m/1d",
        "akshare/jqdata", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "所有港股个股最小周期 K 线，大周期由本地聚合",
    ),
    DatasetDefinition(
        "STOCK_F10", "A/H 股 F10 公司基础资料", "CN/HK", "STOCK", "DAILY",
        "tencent/eastmoney-f10", "DAILY", ("code", "market"),
        ("code", "market", "name", "org_name", "total_market_cap_yi", "float_market_cap_yi",
         "pe", "pb", "industry_em", "industry_csrc", "org_profile", "business_scope",
         "reg_capital_wan", "emp_num", "listing_info", "source", "fetched_at"),
        "SYNC_SELECTED", "缺失字段保持 null；禁止 LLM 补全；记录 source 与 fetched_at",
        "所有 A/H 股上市公司 F10：公司概况、行业、总/流通市值、主营业务与经营范围",
    ),
    DatasetDefinition(
        "CN_ETF_BAR", "A股场内基金K线", "CN", "ETF", "1m/5m/15m/30m/1d",
        "jqdata/tushare/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "所有 A 股场内基金（ETF）最小周期 K 线",
    ),
    DatasetDefinition(
        "CN_INDEX_BAR", "A股指数K线", "CN", "INDEX", "1m/5m/15m/30m/1d",
        "jqdata/tushare/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "A 股指数最小周期 K 线",
    ),
    DatasetDefinition(
        "HK_INDEX_BAR", "港股指数K线", "HK", "INDEX", "1m/5m/15m/30m/1d",
        "akshare/jqdata", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "港股指数最小周期 K 线",
    ),
    DatasetDefinition(
        "GLOBAL_INDEX_BAR", "全球指数K线", "GLOBAL", "INDEX", "1d",
        "akshare/外部行情源", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "全球指数（美股、欧洲、亚太等）日线",
    ),
    DatasetDefinition(
        "FUTURE_CONTRACT_BAR", "期货固定合约K线", "CN", "FUTURE", "1m/5m/15m/30m/1d",
        "jqdata/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "国内期货具体合约（如 rb2610）K 线，与主力/加权/指数严格区分",
    ),
    DatasetDefinition(
        "FUTURE_MAIN_BAR", "期货主力合约K线", "CN", "FUTURE", "1m/5m/15m/30m/1d",
        "jqdata/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重；换月需标记 is_roll_day",
        "国内期货主力合约连续 K 线，按持仓量/成交量/近月选择",
    ),
    DatasetDefinition(
        "FUTURE_SECONDARY_BAR", "期货次连合约K线", "CN", "FUTURE", "5m/15m/30m/1h/1d",
        "通达信期货通本地缓存", "INCREMENTAL", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重；来源与主连/加权物理隔离",
        "国内期货次连（通达信期货通 L7）K线",
    ),
    DatasetDefinition(
        "FUTURE_WEIGHTED_BAR", "期货加权连续K线", "CN", "FUTURE", "1d",
        "jqdata/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重；加权权重与算法版本必须记录",
        "国内期货品种加权连续 K 线，按各合约持仓量加权合成",
    ),
    DatasetDefinition(
        "FUTURE_INDEX_BAR", "期货商品指数K线", "CN", "INDEX", "1d",
        "同花顺/期货通/文华/akshare", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "国内期货商品指数（工业品、化工、贵金属、农产品等）",
    ),
    DatasetDefinition(
        "MACRO_SERIES", "宏观数据序列", "GLOBAL", "MACRO", "DAILY/MONTHLY",
        "akshare/国家统计局/央行/外部数据源", "AS_AVAILABLE", ("series_id", "available_time"),
        ("series_id", "name", "frequency", "unit", "source", "available_time", "quality_status", "value", "definition", "calculation_method"),
        "SYNC_ALL", "缺失数据禁止填 0；记录 source/available_time/quality_status",
        "M0/M1/M2/DR007/CPI/PPI/PMI/国债收益率/美元指数/VIX 等宏观序列",
    ),
    DatasetDefinition(
        "DERIVED_METRIC", "派生指标（Gold 层）", "GLOBAL", "GENERAL", "AS_COMPUTED",
        "本地计算", "AS_COMPUTED", ("metric_id", "trading_date", "period", "metric_name"),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_SELECTED", "定义与计算方法必须登记，禁止无来源派生值",
        "涨跌幅、振幅、均线、ROC、波动率等 Gold 指标",
    ),
    DatasetDefinition(
        "STRATEGY_SIGNAL", "策略结果", "GLOBAL", "GENERAL", "AS_COMPUTED",
        "本地策略引擎", "AS_COMPUTED", ("signal_id",),
        ("signal_id", "strategy_id", "strategy_version", "instrument_id", "trading_date", "period", "signal", "reason", "risk_tags", "computed_at"),
        "SYNC_SELECTED", "必须引用已登记的策略定义与版本",
        "自定义策略扫描输出：命中标的、信号、原因与风险标签",
    ),
    DatasetDefinition(
        "FUTURES_BREADTH", "期货每日涨跌家数", "CN", "FUTURE", "DAILY",
        "本地计算（主力/加权/固定合约/商品指数 1d bars）", "DAILY", ("trading_day", "series_kind"),
        ("trading_day", "series_kind", "advances", "declines", "unchanged", "metric_definition", "calculation_method", "timestamp", "source"),
        "SYNC_ALL", "首个无前收盘的交易日不得计入；上涨/下跌/平盘按前收盘严格比较",
        "国内期货主力/加权合约每日上涨、下跌、平盘个数",
    ),
    DatasetDefinition(
        "FUTURES_LONG_SHORT_HEAT", "中国商品期货多空热度", "CN", "FUTURE", "DAILY",
        "本地计算（商品期货固定月份与加权合约 Silver 日线）", "DAILY", ("formula_version", "trade_date"),
        (
            "trade_date", "total_variety_count", "valid_variety_count", "missing_variety_count",
            "up_variety_count", "down_variety_count", "flat_variety_count",
            "fund_valid_variety_count", "fund_missing_variety_count", "up_fund", "down_fund",
            "flat_fund", "return_coverage", "fund_coverage", "breadth_score_daily",
            "fund_score_daily", "breadth_score_10d", "fund_score_10d", "divergence",
            "is_warmup", "data_quality_status", "formula_version", "source_cutoff",
            "calculation_method", "calculated_at",
        ),
        "SYNC_ALL",
        "排除中金所；缺失不补零；沉淀资金覆盖率未达阈值时资金热度保持 null；保留热身与质量状态",
        "商品期货品种宽度、沉淀资金及其 10 个有效交易日指数衰减热度；用户组合权重不入库",
    ),
    DatasetDefinition(
        "FUTURES_STRUCTURE_DAILY", "中国商品期货结构日度", "CN", "FUTURE", "DAILY",
        "本地计算（商品期货固定月份合约 Silver 日线）", "DAILY",
        ("chart_id", "direction", "formula_version", "trade_date", "member_key"),
        (
            "chart_id", "direction", "trade_date", "member_key", "member_name", "value",
            "input_row_count", "missing_row_count", "data_quality_status", "formula_version",
            "price_basis", "source", "calculated_at",
        ),
        "SYNC_ALL",
        "有效月份合约、质量 PASS、缺失不补零；价格相关图必须声明唯一 price_basis",
        "期货品种或席位结构的逐日真实成员值；当前只生产不依赖价格的品种单边持仓量。",
    ),
    DatasetDefinition(
        "FUTURES_STRUCTURE_BASELINE", "中国商品期货结构固定基准", "CN", "FUTURE", "AS_COMPUTED",
        "本地计算（最新完整交易日固定顺序）", "ON_CHANGE",
        ("chart_id", "direction", "formula_version"),
        (
            "chart_id", "direction", "baseline_version", "baseline_day", "threshold", "stack_order",
            "primary_members", "other_members", "formula_version", "price_basis", "source", "created_at",
        ),
        "SYNC_ONCE_PER_FORMULA",
        "基准日必须完整；固定顺序与其他集合不得随每日排名自动漂移",
        "每张期货结构图、指标与方向独立保存的固定堆叠顺序及其他成员集合。",
    ),
    DatasetDefinition(
        "FUTURES_MEMBER_POSITION_DAILY", "期货交易所会员持仓排名明细", "CN", "FUTURE", "DAILY",
        "交易所公开会员排名（经 akshare 适配）", "DAILY",
        ("trading_day", "exchange", "contract_code", "side", "rank", "source"),
        (
            "trading_day", "exchange", "contract_code", "product_code", "side", "rank", "member_key",
            "member_name", "position", "position_change", "source", "collected_at",
        ),
        "SYNC_ALL",
        "仅保存交易所实际公布的方向排名；会员未出现在另一方向排名时保持缺失，严禁按 0 推断。",
        "国内期货具体月份合约的会员多头/空头排名及增减，记录交易所、名次和来源覆盖边界。",
    ),
    DatasetDefinition(
        "FUTURES_OI_LEADERBOARD", "期货品种持仓龙虎榜", "CN", "FUTURE", "DAILY",
        "交易所会员持仓排名（akshare/交易所）", "DAILY", ("instrument_id", "trading_day"),
        ("instrument_id", "trading_day", "long_position", "long_position_change", "short_position", "short_position_change", "net_position", "net_position_change", "member_count", "source", "metric_definition", "calculation_method"),
        "SYNC_ALL", "会员持仓明细必须按品种+交易日聚合；净持仓=多头-空头",
        "国内期货品种加权合约汇总持仓龙虎榜：多头/空头/净持仓及增减",
    ),
    DatasetDefinition(
        "CN_MARGIN", "沪深京融资融券", "CN", "MARGIN", "DAILY",
        "沪深交易所/akshare", "DAILY", ("metric_id",),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_ALL", "缺失禁止填 0；记录口径、单位与来源",
        "沪、深、京三市融资余额/融资买入额/融券余额/融券净卖出/两融余额占流通市值比",
    ),
    DatasetDefinition(
        "A_SHARE_BREADTH", "A股每日涨跌与市值快照", "CN", "BREADTH", "DAILY",
        "akshare/腾讯全市场快照", "DAILY", ("metric_id",),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_ALL", "全市场快照统计口径必须注明来源；涨停/跌停不得按统一阈值近似",
        "每日上涨/下跌/平盘家数、沪深京总市值、当日成交额；涨停/跌停由 CN_ZT_POOL 权威池提供",
    ),
    DatasetDefinition(
        "HSGT_FLOW", "北向南向资金", "CN", "FLOW", "DAILY",
        "akshare/东财", "DAILY", ("metric_id",),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_ALL", "资金方向/板块必须记录在 metric_id 与定义中",
        "北向/南向当日净买额、历史累计净买额、持股市值等",
    ),
    DatasetDefinition(
        "CN_ZT_POOL", "涨停跌停池与连板高度", "CN", "POOL", "DAILY",
        "akshare/东财", "DAILY", ("metric_id",),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_ALL", "连板高度必须取自当日池记录的最大连板数",
        "涨停/跌停池家数与最大连板高度；昨日涨停今日接盘收益率待补算",
    ),
    DatasetDefinition(
        "FUTURE_GLOBAL_BAR", "外盘期货K线", "GLOBAL", "FUTURE", "1d",
        "akshare/外盘交易所", "DAILY", ("instrument_id", "period", "bar_start"),
        BAR_FIELDS, "FILTERED", "OHLC 有界、质量 PASS、按主键去重",
        "COMEX 黄金/白银/铜、NYMEX WTI 原油日线",
    ),
    DatasetDefinition(
        "USD_INDEX_VIX", "美元指数/VIX", "GLOBAL", "MACRO", "DAILY",
        "eastmoney/cboe/tencent", "DAILY", ("metric_id",),
        ("metric_id", "instrument_id", "trading_date", "period", "metric_name", "value", "definition", "calculation_method", "timestamp"),
        "SYNC_ALL", "记录 UTC 自然日对应交易日；来源必须注明",
        "美元指数 DXY 与 VIX 波动率指数",
    ),
)


def validate_dataset_definition(definition: DatasetDefinition) -> None:
    if not definition.dataset_id or not definition.dataset_id.isupper() or "_" not in definition.dataset_id:
        raise ValueError(f"dataset_id must be an UPPER_SNAKE identifier, got {definition.dataset_id!r}")
    if not definition.dataset_name.strip():
        raise ValueError("dataset_name must not be empty")
    if not definition.primary_key:
        raise ValueError("primary_key must not be empty")
    if not definition.fields:
        raise ValueError("fields must not be empty")
    if not definition.sync_policy.strip():
        raise ValueError("sync_policy must not be empty")
    if not definition.quality_rule.strip():
        raise ValueError("quality_rule must not be empty")


def dataset_index() -> dict[str, DatasetDefinition]:
    return {dataset.dataset_id: dataset for dataset in DEFAULT_DATASETS}
