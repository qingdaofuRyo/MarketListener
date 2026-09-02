from market_monitor.market_classification import (
    UNCLASSIFIED_CATEGORY,
    classify_market,
    market_category_options,
    matches_market_category,
    night_session,
    split_zero_prefix_symbols,
)


def instrument(symbol: str, *, market: str = "CN", exchange: str = "", asset_type: str = "") -> dict[str, str]:
    return {"symbol": symbol, "market": market, "exchange": exchange, "assetType": asset_type}


def test_ambiguous_000001_prefers_explicit_fields_then_allowlist() -> None:
    assert classify_market(instrument("000001", exchange="SSE", asset_type="INDEX")) == "exchange-index"
    assert classify_market(instrument("000001", exchange="SZSE", asset_type="STOCK")) == "a-sz"
    assert classify_market(instrument("000001")) == "exchange-index"  # documented index allowlist


def test_zero_prefix_split_uses_exchange_and_type_as_second_check() -> None:
    result = split_zero_prefix_symbols(
        [
            instrument("000001", exchange="SH", asset_type="INDEX"),
            instrument("000001", exchange="SZ", asset_type="STOCK"),
            instrument("600519", exchange="SH", asset_type="STOCK"),
            instrument("399001", exchange="SZ", asset_type="INDEX"),
            instrument("000001", exchange="SH", asset_type="STOCK"),
            instrument("000001", exchange="SZ", asset_type="INDEX"),
        ]
    )
    assert [item["symbol"] for item in result["indexes"]] == ["000001"]
    assert [item["symbol"] for item in result["stocks"]] == ["000001"]


def test_explicit_stock_or_index_type_overrides_exchange_prefix_ambiguity() -> None:
    assert classify_market(instrument("000001", exchange="SSE", asset_type="STOCK")) == "a-sh"
    assert classify_market(instrument("000001", exchange="SZSE", asset_type="INDEX")) == "exchange-index"
    assert classify_market(instrument("881001", exchange="SSE", asset_type="INDEX")) == "tdx-index"
    assert classify_market(instrument("881001", exchange="SSE")) == "tdx-index"


def test_bond_repo_fund_and_reit_code_ranges() -> None:
    cases = [
        (instrument("110075", exchange="SSE"), "a-convertible"),
        (instrument("126001", exchange="SSE"), "a-convertible"),
        (instrument("113001", exchange="SSE"), "a-convertible"),
        (instrument("121001", exchange="SZSE"), "a-convertible"),
        (instrument("124001", exchange="SZSE"), "a-convertible"),
        (instrument("123127", exchange="SZSE"), "a-convertible"),
        (instrument("132001", exchange="SSE"), "a-exchangeable"),
        (instrument("120001", exchange="SZSE"), "a-exchangeable"),
        (instrument("201001", exchange="SSE"), "a-other-repo"),
        (instrument("204001", exchange="SSE"), "a-pledged-repo"),
        (instrument("207001", exchange="SSE"), "a-other-repo"),
        (instrument("131810", exchange="SZSE"), "a-pledged-repo"),
        (instrument("131910", exchange="SZSE"), "a-other-repo"),
        (instrument("501001", exchange="SSE"), "a-lof"),
        (instrument("160123", exchange="SZSE"), "a-lof"),
        (instrument("508000", exchange="SSE"), "a-reit"),
        (instrument("180101", exchange="SZSE"), "a-reit"),
        (instrument("181001", exchange="SZSE"), "a-reit"),
        (instrument("158001", exchange="SZSE"), "a-etf"),
        (instrument("526001", exchange="SSE"), "a-etf"),
        (instrument("550001", exchange="SSE"), "a-etf"),
        (instrument("580001", exchange="SSE"), "a-etf"),
        (instrument("589001", exchange="SSE"), "a-etf"),
        (instrument("880001", exchange="SSE"), "tdx-index"),
        (instrument("881048", exchange="SSE"), "tdx-index"),
    ]
    for item, expected in cases:
        assert classify_market(item) == expected


def test_a_share_boundaries_use_etf_index_and_board_priority() -> None:
    cases = [
        (instrument("000300", exchange="SSE", asset_type="INDEX"), "exchange-index"),
        (instrument("399006", exchange="SZSE", asset_type="INDEX"), "exchange-index"),
        (instrument("889001", exchange="SSE"), "exchange-index"),
        (instrument("950001", exchange="SSE"), "exchange-index"),
        (instrument("999001", exchange="SSE"), "exchange-index"),
        (instrument("470001", exchange="SZSE", asset_type="INDEX"), "exchange-index"),
        (instrument("970001", exchange="SZSE", asset_type="INDEX"), "exchange-index"),
        (instrument("988001", exchange="SZSE", asset_type="INDEX"), "exchange-index"),
        (instrument("588000", exchange="SSE", asset_type="ETF"), "a-etf"),
        (instrument("159915", exchange="SZSE", asset_type="ETF"), "a-etf"),
        (instrument("688001", exchange="SSE", asset_type="STOCK"), "a-star"),
        (instrument("920001", exchange="BSE", asset_type="STOCK"), "a-bse"),
    ]
    for item, expected in cases:
        assert classify_market(item) == expected


def test_hong_kong_and_futures_boundaries() -> None:
    assert classify_market(instrument("00700", market="HK", exchange="HKEX", asset_type="STOCK")) == "hk-stock"
    assert classify_market(instrument("HSI", market="HK", exchange="HKEX", asset_type="INDEX")) == "hk-index"
    assert classify_market(instrument("IF2612", exchange="CFFEX", asset_type="FUTURE")) == "cn-future-cffex"
    rb = instrument("rb2510", exchange="SHFE", asset_type="FUTURE")
    assert classify_market(rb) == "cn-future-shfe"
    assert night_session(rb) == "21:00-23:00"
    assert classify_market(instrument("rb000", exchange="SHFE", asset_type="FUTURE")) == "cn-future-index"
    assert classify_market(instrument("USDJPY", market="GLOBAL", exchange="BASIC_FX", asset_type="FX_RATE")) == "global-fx"
    assert classify_market(instrument("2_CPI", market="CN", exchange="TDX_MACRO", asset_type="MACRO")) == "cn-macro"
    assert classify_market({
        **instrument("V050C0", exchange="TDX_OPTION_VOLATILITY", asset_type="INDEX"),
        "seriesKind": "OPTION_VOLATILITY_INDEX",
    }) == "cn-future-index"


def test_legacy_stock_key_remains_compatible() -> None:
    assert matches_market_category(instrument("600519", exchange="SSE", asset_type="STOCK"), "cn-stock")


def test_indexes_use_administrator_categories() -> None:
    assert classify_market(instrument("000300", exchange="CSI", asset_type="INDEX")) == "csi-index"
    assert classify_market(instrument("399001", exchange="CNI", asset_type="INDEX")) == "cni-index"
    assert classify_market(instrument("993411", exchange="HUAZHENG", asset_type="INDEX")) == "huazheng-index"
    assert matches_market_category(instrument("000300", exchange="CSI", asset_type="INDEX"), "a-index")


def test_futures_use_explicit_trading_venue_categories() -> None:
    domestic = {
        "SHFE": "cn-future-shfe", "INE": "cn-future-ine", "DCE": "cn-future-dce",
        "CZCE": "cn-future-czce", "CFFEX": "cn-future-cffex", "GFEX": "cn-future-gfex",
    }
    symbols = {"SHFE": "RB2610", "INE": "SC2610", "DCE": "M2610", "CZCE": "CF701", "CFFEX": "IF2610", "GFEX": "SI2610"}
    for exchange, expected in domestic.items():
        assert classify_market(instrument(symbols[exchange], exchange=exchange, asset_type="FUTURE")) == expected
    for exchange, expected in {"COMEX": "future-comex", "NYMEX": "future-nymex", "CBOT": "future-cbot"}.items():
        assert classify_market(instrument("GC00W", market="GLOBAL", exchange=exchange, asset_type="FUTURE")) == expected
    assert matches_market_category(instrument("RB2610", exchange="SHFE", asset_type="FUTURE"), "cn-future-commodity")


def test_b_shares_are_separate_from_a_share_categories() -> None:
    sh_b = instrument("900901", exchange="SSE", asset_type="B_SHARE")
    sz_b = instrument("200002", exchange="SZSE", asset_type="B_SHARE")
    assert classify_market(sh_b) == "b-sh"
    assert classify_market(sz_b) == "b-sz"
    assert not matches_market_category(sh_b, "cn-stock")


def test_legacy_index_key_includes_tongdaxin_board_indexes() -> None:
    assert matches_market_category(instrument("880001", exchange="SSE"), "cn-index")
    assert matches_market_category(instrument("881048", exchange="SSE"), "cn-index")


def test_options_are_excluded_while_convertible_bonds_are_classified() -> None:
    assert classify_market(instrument("rb-2510-C-3800", exchange="SHFE", asset_type="OPTION")) == UNCLASSIFIED_CATEGORY
    assert classify_market(instrument("10008155", exchange="SSE", asset_type="OPTION")) == UNCLASSIFIED_CATEGORY
    assert classify_market(instrument("113001", exchange="SSE", asset_type="BOND")) == "a-convertible"


def test_unclassified_is_not_a_public_market_category() -> None:
    assert UNCLASSIFIED_CATEGORY not in {item["id"] for item in market_category_options()}
    assert "other" not in {item["id"] for item in market_category_options()}


def test_night_session_only_includes_products_that_actually_trade_overnight() -> None:
    assert night_session(instrument("rb2510", exchange="SHFE", asset_type="FUTURE")) == "21:00-23:00"
    assert night_session(instrument("br2510", exchange="SHFE", asset_type="FUTURE")) == "21:00-23:00"
    assert night_session(instrument("ad2510", exchange="SHFE", asset_type="FUTURE")) == "21:00-01:00"
    assert night_session(instrument("pr510", exchange="CZCE", asset_type="FUTURE")) == "21:00-23:00"
    assert night_session(instrument("be2609", exchange="DCE", asset_type="FUTURE")) == "21:00-23:00"
    assert night_session(instrument("si2510", exchange="GFEX", asset_type="FUTURE")) is None
    assert night_session(instrument("pk510", exchange="CZCE", asset_type="FUTURE")) is None
    assert night_session(instrument("ap510", exchange="CZCE", asset_type="FUTURE")) is None
