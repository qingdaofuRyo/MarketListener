"""Verified SHFE-hosted contract-page locations for INE futures products.

INE and SHFE publish the same INE futures contract pages on their respective
official sites.  The INE site may respond with an HTML access challenge even
when its HTTP status is 200.  These SHFE-hosted pages contain the product's
``pageList`` contract records and are independently checked after parsing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlparse


# Each location was probed on 2026-10-06: HTTP 200, nonempty pageList, and a
# first contract record with the matching ContractSymbol and INE listing body.
_INE_SHFE_PATHS = {
    "INE.SC": "energyandchemical/sc_f/",
    "INE.LU": "energyandchemical/lu_f/",
    "INE.NR": "energyandchemical/nr_f/",
    "INE.BC": "metal/nonferrousmetal/bc_f/",
    "INE.EC": "index_f/ec_f/",
}
_SHFE_ORIGIN = "https://www.shfe.com.cn/products/futures/"
_INE_LISTING_BODY = "上海国际能源交易中心"


def ine_official_contract_urls(product_keys: Iterable[str]) -> dict[str, str]:
    """Return independently verified official mirror URLs for known INE keys.

    Unknown products remain absent so the caller reports a genuine coverage
    gap instead of guessing a path from a source symbol.
    """
    return {
        key: _SHFE_ORIGIN + _INE_SHFE_PATHS[key]
        for key in sorted(set(product_keys) & _INE_SHFE_PATHS.keys())
    }


def validate_ine_official_contract_url(product_key: str, url: str) -> None:
    """Reject nonofficial or wrong-product mirrors before fetching a page."""
    expected = ine_official_contract_urls((product_key,)).get(product_key)
    parsed = urlparse(url)
    if expected is None or url != expected or parsed.scheme != "https" or parsed.hostname != "www.shfe.com.cn":
        raise ValueError(f"unverified INE contract source for {product_key}")


def validate_ine_official_contract_fields(product_key: str, fields: Mapping[str, str]) -> None:
    """Require both the product code and INE listing body in parsed terms.

    This is intentionally stricter than a successful HTTP response or an HTML
    ``pageList`` match: the SHFE site also hosts its own futures products.
    """
    if product_key not in _INE_SHFE_PATHS:
        raise ValueError(f"unknown INE contract product {product_key}")
    code = product_key.split(".", 1)[1]
    source_code = fields.get("交易代码", "").strip().upper()
    listing_body = fields.get("上市机构") or fields.get("上市交易所") or ""
    if source_code != code or _INE_LISTING_BODY not in listing_body:
        raise ValueError(f"INE contract terms do not match {product_key}")


__all__ = (
    "ine_official_contract_urls",
    "validate_ine_official_contract_url",
    "validate_ine_official_contract_fields",
)
