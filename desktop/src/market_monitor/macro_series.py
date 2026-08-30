"""宏观数据序列模块：口径登记、行归一与派生指标。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class MacroSeriesDefinition:
    """一条宏观序列的口径登记。

    任何入库的宏观数值都必须引用登记项，保证 M1/M2/DR007/CPI/PPI/PMI
    等指标有可审计的口径、单位、频率、来源与计算方法。
    """

    series_id: str
    name: str
    frequency: str
    unit: str
    source: str
    definition: str
    calculation_method: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "name": self.name,
            "frequency": self.frequency,
            "unit": self.unit,
            "source": self.source,
            "definition": self.definition,
            "calculation_method": self.calculation_method,
            "aliases": list(self.aliases),
        }


DEFAULT_MACRO_SERIES: tuple[MacroSeriesDefinition, ...] = (
    MacroSeriesDefinition(
        "M1_MONEY_SUPPLY", "M1 货币供应量同比", "MONTHLY", "%",
        "中国人民银行/akshare", "狭义货币 M1 期末余额同比增速",
        "官方公布同比增速，禁止由余额自行推算；缺失禁止填 0",
    ),
    MacroSeriesDefinition(
        "M0_MONEY_SUPPLY", "M0 货币供应量同比", "MONTHLY", "%",
        "中国人民银行/akshare", "流通中现金 M0 期末余额同比增速",
        "官方公布同比增速，禁止由余额自行推算；缺失禁止填 0",
    ),
    MacroSeriesDefinition(
        "M2_MONEY_SUPPLY", "M2 货币供应量同比", "MONTHLY", "%",
        "中国人民银行/akshare", "广义货币 M2 期末余额同比增速",
        "官方公布同比增速；口径变动时记录 definition_version",
    ),
    MacroSeriesDefinition(
        "CN_IMPORT_USD_YOY", "中国以美元计进口同比", "MONTHLY", "%",
        "海关总署/金十数据/akshare", "中国以美元计价进口额同比增速",
        "采用来源公布值；日期为来源公布日，不能在缺少统计期字段时伪造月末观察期",
    ),
    MacroSeriesDefinition(
        "CN_EXPORT_USD_YOY", "中国以美元计出口同比", "MONTHLY", "%",
        "海关总署/金十数据/akshare", "中国以美元计价出口额同比增速",
        "采用来源公布值；日期为来源公布日，不能在缺少统计期字段时伪造月末观察期",
    ),
    MacroSeriesDefinition(
        "CN_TRADE_BALANCE_USD", "中国以美元计贸易差额", "MONTHLY", "亿美元",
        "海关总署/金十数据/akshare", "中国以美元计价货物贸易差额",
        "采用来源公布的亿美元数值；日期为来源公布日",
    ),
    MacroSeriesDefinition(
        "CN_RETAIL_SALES_YOY", "社会消费品零售总额同比", "MONTHLY", "%",
        "国家统计局/东方财富/akshare", "社会消费品零售总额当月同比增速",
        "采用来源当月同比字段；缺失不以累计同比代替",
    ),
    MacroSeriesDefinition(
        "CN_RETAIL_SALES_MOM", "社会消费品零售总额环比", "MONTHLY", "%",
        "国家统计局/东方财富/akshare", "社会消费品零售总额当月环比增速",
        "采用来源当月环比字段；禁止由累计额反推",
    ),
    MacroSeriesDefinition(
        "CN_FOREX_RESERVES", "中国外汇储备", "MONTHLY", "亿美元",
        "国家外汇管理局/金十数据/akshare", "中国官方外汇储备余额",
        "采用来源公布的亿美元数值；日期为来源公布日",
    ),
    MacroSeriesDefinition(
        "CN_ELECTRICITY_CONSUMPTION", "中国全社会用电量（年内累计）", "MONTHLY", "亿千瓦时",
        "国家能源局（新浪财经 / AkShare 转发）", "截至当月的全社会用电量年内累计值",
        "来源原始数值以万千瓦时计，除以 10000 转为亿千瓦时；不得将累计值当作单月值",
    ),
    MacroSeriesDefinition(
        "DR007", "银行间 7 天质押式回购利率", "DAILY", "%",
        "中国外汇交易中心/akshare", "存款类金融机构质押式回购加权利率 DR007",
        "按交易日官方发布值，不使用盘中估算",
    ),
    MacroSeriesDefinition(
        "CPI", "居民消费价格指数同比", "MONTHLY", "%",
        "国家统计局/akshare", "CPI 当月同比",
        "官方公布同比；环比单独登记 CPI_MOM",
    ),
    MacroSeriesDefinition(
        "PPI", "工业生产者出厂价格指数同比", "MONTHLY", "%",
        "国家统计局/akshare", "PPI 当月同比",
        "官方公布同比；环比单独登记 PPI_MOM",
    ),
    MacroSeriesDefinition(
        "CPI_PPI_SPREAD", "CPI-PPI 剪刀差", "MONTHLY", "%",
        "本地派生（CPI、PPI）", "CPI 同比减 PPI 同比",
        "derived = CPI - PPI；任一输入缺失则不产出",
    ),
    MacroSeriesDefinition(
        "PMI_MANUFACTURING", "制造业 PMI", "MONTHLY", "指数",
        "国家统计局/akshare", "中国制造业采购经理指数",
        "官方公布值，荣枯线 50",
    ),
    MacroSeriesDefinition(
        "PMI_CAIXIN_MANUFACTURING", "财新制造业 PMI", "MONTHLY", "指数",
        "财新/S&P Global/akshare", "财新中国制造业采购经理指数",
        "财新公布值，荣枯线 50；与国家统计局口径分开登记",
    ),
    MacroSeriesDefinition(
        "PMI_CAIXIN_SERVICES", "财新服务业 PMI", "MONTHLY", "指数",
        "财新/S&P Global/akshare", "财新中国服务业经营活动指数",
        "财新公布值，荣枯线 50；与国家统计局非制造业商务活动指数口径分开登记",
    ),
    MacroSeriesDefinition(
        "PMI_SERVICES", "服务业 PMI", "MONTHLY", "指数",
        "国家统计局/akshare", "中国非制造业商务活动指数",
        "官方公布值，荣枯线 50",
    ),
    MacroSeriesDefinition(
        "CN10Y_YIELD", "中国 10 年期国债收益率", "DAILY", "%",
        "中国债券信息网/akshare", "中债 10 年期国债到期收益率",
        "按交易日收盘估值，禁止与盘中瞬时值混用",
    ),
    MacroSeriesDefinition(
        "USD_INDEX", "美元指数", "DAILY", "指数",
        "ICE/akshare", "美元指数 DXY",
        "按交易日收盘值；使用 UTC 自然日对应交易日",
    ),
    MacroSeriesDefinition(
        "US10Y_YIELD", "美国 10 年期国债收益率", "DAILY", "%",
        "FRED/akshare", "美国 10 年期国债收益率",
        "按交易日收盘值",
    ),
    MacroSeriesDefinition(
        "VIX", "VIX 波动率指数", "DAILY", "指数",
        "CBOE/akshare", "芝加哥期权交易所波动率指数",
        "按交易日收盘值",
    ),
    MacroSeriesDefinition(
        "FED_FUNDS_RATE", "美联储利率（目标区间上限）", "DAILY", "%",
        "美联储/akshare", "联邦基金目标利率区间上限",
        "决议日更新，非决议日沿用上一值并标记 as_of_date",
    ),
    MacroSeriesDefinition(
        "US_NONFARM_PAYROLLS_SA", "美国季调后非农就业人口变动", "MONTHLY", "千人",
        "BLS/金十数据/akshare", "美国非农就业人口月度变动（季调后）",
        "来源值以万人表示时乘以 10 转为千人；日期为来源公布日",
    ),
    MacroSeriesDefinition(
        "GOLD_SILVER_RATIO", "金银比", "DAILY", "比值",
        "本地派生（黄金、白银）", "黄金价格 / 白银价格",
        "derived = AU / AG；使用同一交易日同一币种价格",
    ),
    MacroSeriesDefinition(
        "GOLD_OIL_RATIO", "金油比（WTI）", "DAILY", "比值",
        "本地派生（黄金、WTI 原油）", "黄金价格 / WTI 原油价格",
        "derived = AU / WTI；使用同一交易日同一币种价格",
    ),
    MacroSeriesDefinition(
        "BTC_USD", "比特币价格", "DAILY", "USD",
        "外部行情源/akshare", "BTC/USD 收盘价",
        "按交易日收盘价；24 小时市场以 UTC 日切",
    ),
    MacroSeriesDefinition(
        "ETH_USD", "以太坊价格", "DAILY", "USD",
        "外部行情源/akshare", "ETH/USD 收盘价",
        "按交易日收盘价；24 小时市场以 UTC 日切",
    ),
)


@dataclass(frozen=True)
class MacroPoint:
    """一条已归一的宏观观测值。"""

    series_id: str
    name: str
    frequency: str
    unit: str
    source: str
    available_time: str
    value: float
    definition: str
    calculation_method: str
    quality_status: str = "PASS"
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "name": self.name,
            "frequency": self.frequency,
            "unit": self.unit,
            "source": self.source,
            "available_time": self.available_time,
            "value": self.value,
            "definition": self.definition,
            "calculation_method": self.calculation_method,
            "quality_status": self.quality_status,
            "fetched_at": self.fetched_at,
        }


def macro_series_index() -> dict[str, MacroSeriesDefinition]:
    return {series.series_id: series for series in DEFAULT_MACRO_SERIES}


def normalise_macro_point(
    series_id: str,
    *,
    available_time: str | date,
    value: float,
    source: str | None = None,
    quality_status: str = "PASS",
    now: datetime | None = None,
) -> MacroPoint:
    """把来源行归一为 MacroPoint；未登记口径的序列直接拒绝。"""

    definition = macro_series_index().get(series_id)
    if definition is None:
        raise ValueError(f"unknown macro series_id: {series_id}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("macro value must be finite")
    if quality_status not in {"PASS", "SUSPECT", "FAILED"}:
        raise ValueError("quality_status must be PASS/SUSPECT/FAILED")
    available = available_time.isoformat() if isinstance(available_time, date) else str(available_time)
    if not available.strip():
        raise ValueError("available_time must not be blank")
    return MacroPoint(
        series_id=definition.series_id,
        name=definition.name,
        frequency=definition.frequency,
        unit=definition.unit,
        source=source or definition.source,
        available_time=available,
        value=float(value),
        definition=definition.definition,
        calculation_method=definition.calculation_method,
        quality_status=quality_status,
        fetched_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    )


def derive_series(
    inputs: Mapping[str, Sequence[MacroPoint]],
    *,
    derived_series_id: str,
    formula: str,
    now: datetime | None = None,
) -> list[MacroPoint]:
    """按公式派生新序列（如 CPI-PPI、金银比），输入缺失的日期不产出。

    formula 支持 "A-B" 与 "A/B" 两种二元运算，A/B 使用序列 id 占位。
    """

    if formula not in {"A-B", "A/B"}:
        raise ValueError("formula must be A-B or A/B")
    if len(inputs) != 2:
        raise ValueError("derive_series requires exactly two input series")
    left_id, right_id = tuple(inputs)
    left_index = {point.available_time: point for point in inputs[left_id]}
    right_index = {point.available_time: point for point in inputs[right_id]}
    output: list[MacroPoint] = []
    for available_time in sorted(set(left_index) & set(right_index)):
        left = left_index[available_time]
        right = right_index[available_time]
        if left.quality_status != "PASS" or right.quality_status != "PASS":
            continue
        if formula == "A-B":
            value = left.value - right.value
        else:
            if right.value == 0:
                continue
            value = left.value / right.value
        output.append(
            normalise_macro_point(
                derived_series_id,
                available_time=available_time,
                value=value,
                source="local-derived",
                now=now,
            )
        )
    return output
