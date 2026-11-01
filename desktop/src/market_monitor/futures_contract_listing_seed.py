"""Curated exchange evidence for a futures *product's* first trading date.

This is deliberately separate from a contract month's listing date and from the
publication date of a contract specification.  An entry is usable only when the
archived official source at ``data_root/bronze/futures_contract_listing`` has
the reviewed SHA-256.  No network request or fallback date is made here.

The matching raw PDFs/HTML were retrieved from exchange or regulator hosts on
2026-10-06.  ``verified_listing_evidence`` can be used by the contract-info
writer to insert PASS evidence, preserving a MONTH date where a historical
exchange source identifies only the month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal


DatePrecision = Literal["DAY", "MONTH", "YEAR"]


@dataclass(frozen=True)
class OfficialSource:
    url: str
    filename: str
    digest: str


@dataclass(frozen=True)
class ListingDate:
    value: str
    precision: DatePrecision
    source_id: str
    note: str


SOURCES: dict[str, OfficialSource] = {
    "CZCE_RI_HISTORY": OfficialSource(
        "https://www.czce.com.cn/cn/administrator/AdminArticle/editor/UploadWord/200969164357210.pdf",
        "czce_ri_listing.pdf",
        "50dc058658c12a23e1d65731aec7ed4631641eacbd2d519abf5610827a8777dc",
    ),
    "CZCE_CY_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2018/06/29/1505472870185096-1531036138963055.pdf",
        "czce_cy_listing.pdf",
        "95dbfadb08059c8fb3fee20acc78e7e0700f69ed3fb3668d2d617ccf954f223e",
    ),
    "CZCE_PF_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2020/10/19/1597089064856174-1597089064867882.pdf",
        "czce_pf_listing.pdf",
        "0f27826e9c66dbaca8e09681e52d29d1526a4243d3b58afb0d1414f641c946af",
    ),
    "CZCE_PK_GUIDE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2023/01/10/1655832962337700-1655832962348705.pdf",
        "czce_pk_listing.pdf",
        "0a6e6c9d36e5b6238dcbe923e4192b9a0af28e877e83220d56c4d866506b0aec",
    ),
    "CZCE_RAPESEED_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2012/12/26/1356346927698038-1356346927700227.pdf",
        "czce_rm_listing.pdf",
        "6305df760204236c2be57b97f0de29dc307ed3bd5d6110c1f78c6b998ef1b752",
    ),
    "CZCE_SA_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2019/12/02/1572879248889610-1572879248906593.pdf",
        "czce_sa_listing.pdf",
        "93bffa80ac0eda98c5977bc04e34faf45f22db19b825cfb30e716ab036480db2",
    ),
    "CZCE_UR_GUIDE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2021/08/31/1605596823505488-1605596823688619.pdf",
        "czce_ur_listing.pdf",
        "cfacd39e5b4070d894b954264f1ca7912320a1bfa051110ffa562157f438ef64",
    ),
    "CZCE_FERROALLOY_GUIDE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2022/06/16/1655814940249480-1655814940260434.pdf",
        "czce_sf_listing.pdf",
        "516ac03de7cfbe4eaa0e0c866ccb166bed0b22c88cf2204a5070f405ae6fbd9d",
    ),
    "CZCE_2008_ANNUAL": OfficialSource(
        "https://www.czce.com.cn/cn/administrator/AdminArticle/editor/UploadWord/200932015541251.pdf",
        "czce_cf_listing.pdf",
        "ce1ee936222aab2dc43492733ced97d4ea7de7426ccc2630f9cef32165217c6e",
    ),
    "CZCE_TA_GUIDE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2021/12/30/1640081938754265-1640081938763919.pdf",
        "czce_ta_listing.pdf",
        "c2587ff43680125c22be7da7ac9849ff31baa76f7d45dc5d224e19a9dd47a5c5",
    ),
    "CZCE_JR_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2013/11/14/1383304259473565-1383304259475608.pdf",
        "czce_jr_listing.pdf",
        "c4f93ebca17b1e4a315bd5f0e1945d032153ba95166c1f1fda79c5f1952fc7b7",
    ),
    "CZCE_ZC_NOTICE": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2013/10/08/1380292484924686-1380292484926515.pdf",
        "czce_zc_listing.pdf",
        "345a45ea64c3cc9be43800799f83b530a3775a9f3f49dcece7ee4bf547bd456c",
    ),
    "CZCE_AP_LISTING": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2018/06/29/1531036139417403-1531036139469793.pdf",
        "czce_ap_listing.pdf",
        "2983e8eca0c5aaa88c5cb07893b0e4a607751ccc453744d5946dec57c554e1d3",
    ),
    "CZCE_CJ_LISTING": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2019/04/24/1545643298723057-1545643298748458.pdf",
        "czce_cj_listing.pdf",
        "c6da5837d425e772969815564123a272ac3d9f48b0a6b83439696a7a8998b654",
    ),
    "CZCE_OI_LISTING": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2023/01/11/1655833051801069-1655833051835824.pdf",
        "czce_oi_listing.pdf",
        "40850bacc65d4fa7392dd0382296c62e0b908f7cb22818b548a1ebdfe65c64c8",
    ),
    "CZCE_MA_LISTING": OfficialSource(
        "https://www.czce.com.cn/cn/rootfiles/2014/06/13/1402482218856803-1402482218858971.pdf",
        "czce_ma_listing.pdf",
        "31b7386cbd99da23907205f7c056f643592e5e06418353dba79aa64c28215b2b",
    ),
    "CZCE_SR_LISTING": OfficialSource(
        "https://www.czce.com.cn/cn/administrator/AdminArticle/editor/UploadWord/200969164458798.pdf",
        "czce_sr_listing.pdf",
        "aa5e239b87ace0d83864b6d99ff28816038e0240f3e408fd97cd2e29fc4c5c0c",
    ),
    "CSRC_2024_ANNUAL": OfficialSource(
        "https://www.csrc.gov.cn/csrc/c100024/c7624098/7624098/files/"
        "%E4%B8%AD%E5%9B%BD%E8%AF%81%E5%88%B8%E7%9B%91%E7%9D%A3%E7%AE%A1%E7%90%86"
        "%E5%A7%94%E5%91%98%E4%BC%9A%E5%B9%B4%E6%8A%A5%EF%BC%882024%E5%B9%B4%EF%BC%89.pdf",
        "csrc_2024_annual_report.pdf",
        "09ac5d95d8de8715c97980cab820b9cd4c1ad5ad3095f8fbc3da9ebe32dfd318",
    ),
    "CFFEX_2024_PFMI": OfficialSource(
        "http://www.cffex.com.cn/u/cms/www/202405/31111942nrka.pdf",
        "cffex_2024_pfm_disclosure.pdf",
        "060deac17f43863bdf16d154e523eebed9582ec0394cba778e6beb6e033e5c4d",
    ),
    "SHFE_2024_INTERNATIONAL": OfficialSource(
        "https://www.shfe.com.cn/services/marketresearch/magazine/202408/W020240814418785769089.pdf",
        "shfe_2024_international_listings.pdf",
        "c11a2618f2c6c280e102df43a0626ba919257f3bade6539aeca9c1df39545f57",
    ),
    "SHFE_CU_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/20171222/1513912218224.pdf",
        "shfe_cu_1993.pdf",
        "d56b77066a6bf9891adfcb55d6daf886fe5321252b3522d27942073206aca290",
    ),
    "SHFE_AL_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/services/activity/shforums/2023-528/Main/YS-ligf526.pdf",
        "shfe_al_1993.pdf",
        "daa7a3d9b342ffc327d532fda922f836dbf2281fd67060a1d4c5f46001bc97c6",
    ),
    "SHFE_ZN_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/dir_20110705/96946_20110705.pdf",
        "shfe_zn_2007.pdf",
        "5885e59fc58e51c1481dca3aa3dfdc99791535b66fafecfbf4ddc3dc56100b39",
    ),
    "SHFE_PB_NOTICE": OfficialSource(
        "https://www.shfe.com.cn/docview/docview_12185742.htm",
        "shfe_pb_2011.html",
        "7233c0f8de9c11d321672e0b53d7171ae07b45b897ba52e47ebf47b4c1759f47",
    ),
    "SHFE_NI_SN_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/content/lxjy/main.html",
        "shfe_ni_sn_2015.html",
        "5483d9f46e2e106840015ad549b66aa64d456cf669bb6c332f24a6c28b2d1de9",
    ),
    "SHFE_AU_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/jrysp/48/17.pdf",
        "shfe_au_2008.pdf",
        "ab1ee70a8b4994d88f7d2af37a2a3184867c9e7ef4dc71b06cb0fc8920d69a92",
    ),
    "SHFE_AG_NOTICE": OfficialSource(
        "https://www.shfe.com.cn/docview/docview_2447617.htm",
        "shfe_ag_2012.html",
        "3e103e858c31861516cd340d4b2dea704c36d60d221b67949ada8ae58bddccd6",
    ),
    "SHFE_RB_WR_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/dir_20110517/1388_20110517.pdf",
        "shfe_rb_wr_2009.pdf",
        "3dc3ab2ca666d2e56439e71e8696db1503e4a01ab6c55d5bf572113f9e9001f9",
    ),
    "SHFE_HC_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/20170414/1492159499780.pdf",
        "shfe_hc_2014.pdf",
        "704c02edd27cb171121e7db943f2422693c45572a1444a433614e3f9db9e1d72",
    ),
    "SHFE_SS_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/20191211/1576053320817.pdf",
        "shfe_ss_2019.pdf",
        "a98d9567a8e9fc77d0331b7e1a6554a918580fc5ebd9da9de05f055f20c930cc",
    ),
    "SHFE_FU_NEWS": OfficialSource(
        "https://www.shfe.com.cn/docview/docview_47263039.htm",
        "shfe_fu_2004.html",
        "593acf12bc7c49259606e694663b75f3bc3a82ec2b146f26a8efaa5a41702145",
    ),
    "SHFE_BU_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/publicnotice/medium/202510/t20251024_829328.html",
        "shfe_bu_2013.html",
        "5d77b9fdf2aafef9b7bc58d5fce9a4bed430ff70719de0cbbec49813c046de38",
    ),
    "SHFE_RU_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/products/futures/energyandchemical/nr_f/manual/202303/"
        "P020240320710981066273.pdf",
        "shfe_ru_new.pdf",
        "4d0730ebcb906df9e61d7a845652595c9e3b61022ecfb460dd7cda1f30b12b70",
    ),
    "SHFE_SP_HISTORY": OfficialSource(
        "https://www.shfe.com.cn/upload/20190815/1565851082945.pdf",
        "shfe_sp_2018.pdf",
        "28c18b82fa7ff66ef74804a815d3f845c17a32594583d5ebbd917244aa43d8d0",
    ),
    "SHFE_AO_NOTICE": OfficialSource(
        "https://www.shfe.com.cn/publicnotice/notice/202306/t20230606_800293.html",
        "shfe_ao_2023.html",
        "bb7a956d93a65d49929c3131904d3e8b1b5ff200b3b774e84df8cebf1f9dfed3",
    ),
    "SHFE_BR_NEWS": OfficialSource(
        "https://www.shfe.com.cn/content/2023_BR/new.html",
        "shfe_br_2023.html",
        "7734ecb3aeac1430f2e1867f97381b941518211af1ea76a0d862b9f21997949e",
    ),
    "SHFE_AD_NOTICE": OfficialSource(
        "https://www.shfe.com.cn/publicnotice/notice/202505/t20250526_827862.html",
        "shfe_ad_2025.html",
        "e446696b10f69224bbb55123b3aabf6ad4b40ea2bd48a498065020c5415395df",
    ),
    "SHFE_OP_NOTICE": OfficialSource(
        "https://www.shfe.com.cn/publicnotice/notice/202508/t20250818_828697.html",
        "shfe_op_2025.html",
        "ae33f736381305006fc31e4f6353744b0c7a09205678f3d3904ed8e2662b2fd6",
    ),
}


LISTING_DATES: dict[str, ListingDate] = {
    "CZCE.RI": ListingDate("2009-04-20", "DAY", "CZCE_RI_HISTORY", "早籼稻期货首次上市；原品种使用ER代码，现行RI代码另有变更"),
    "CZCE.CY": ListingDate("2017-08-18", "DAY", "CZCE_CY_NOTICE", "郑商所棉纱期货上市通告"),
    "CZCE.PF": ListingDate("2020-10-12", "DAY", "CZCE_PF_NOTICE", "郑商所短纤期货上市公告"),
    "CZCE.PK": ListingDate("2021-02-01", "DAY", "CZCE_PK_GUIDE", "郑商所花生期货官方品种指南"),
    "CZCE.RM": ListingDate("2012-12-28", "DAY", "CZCE_RAPESEED_NOTICE", "郑商所菜籽粕期货上市通告"),
    "CZCE.RS": ListingDate("2012-12-28", "DAY", "CZCE_RAPESEED_NOTICE", "郑商所油菜籽期货上市通告"),
    "CZCE.SA": ListingDate("2019-12-06", "DAY", "CZCE_SA_NOTICE", "郑商所纯碱期货上市公告"),
    "CZCE.UR": ListingDate("2019-08-09", "DAY", "CZCE_UR_GUIDE", "郑商所尿素期货官方投教材料"),
    "CZCE.SF": ListingDate("2014-08-08", "DAY", "CZCE_FERROALLOY_GUIDE", "郑商所铁合金期货官方投教材料"),
    "CZCE.SM": ListingDate("2014-08-08", "DAY", "CZCE_FERROALLOY_GUIDE", "郑商所铁合金期货官方投教材料"),
    "CZCE.WH": ListingDate("2003-03-28", "DAY", "CZCE_2008_ANNUAL", "郑商所2008年报列明优质强筋小麦上市日"),
    "CZCE.CF": ListingDate("2004-06-01", "DAY", "CZCE_2008_ANNUAL", "郑商所2008年报列明棉花期货上市日"),
    "CZCE.TA": ListingDate("2006-12-18", "DAY", "CZCE_TA_GUIDE", "郑商所PTA期货官方投教材料"),
    "CZCE.JR": ListingDate("2013-11-18", "DAY", "CZCE_JR_NOTICE", "郑商所粳稻期货上市通告"),
    "CZCE.ZC": ListingDate("2013-09-26", "DAY", "CZCE_ZC_NOTICE", "郑商所动力煤期货上市通告；当时源合约代码TC"),
    "CZCE.AP": ListingDate("2017-12-22", "DAY", "CZCE_AP_LISTING", "郑商所苹果期货上市公告"),
    "CZCE.CJ": ListingDate("2019-04-30", "DAY", "CZCE_CJ_LISTING", "郑商所红枣期货上市公告"),
    "CZCE.OI": ListingDate("2007-06-08", "DAY", "CZCE_OI_LISTING", "郑商所菜油期货官方品种指南"),
    "CZCE.MA": ListingDate("2011-10-28", "DAY", "CZCE_MA_LISTING", "郑商所甲醇合约修改说明列明上市日"),
    "CZCE.SR": ListingDate("2006-01-06", "DAY", "CZCE_SR_LISTING", "郑商所白糖期货官方产品材料"),
    "GFEX.PS": ListingDate("2024-12-26", "DAY", "CSRC_2024_ANNUAL", "证监会2024年年报第75页列明多晶硅期货上市日"),
    "CFFEX.IF": ListingDate("2010-04-16", "DAY", "CFFEX_2024_PFMI", "沪深300股指期货"),
    "CFFEX.IH": ListingDate("2015-04-16", "DAY", "CFFEX_2024_PFMI", "上证50股指期货"),
    "CFFEX.IC": ListingDate("2015-04-16", "DAY", "CFFEX_2024_PFMI", "中证500股指期货"),
    "CFFEX.IM": ListingDate("2022-07-22", "DAY", "CFFEX_2024_PFMI", "中证1000股指期货"),
    "CFFEX.TF": ListingDate("2013-09-06", "DAY", "CFFEX_2024_PFMI", "5年期国债期货"),
    "CFFEX.T": ListingDate("2015-03-20", "DAY", "CFFEX_2024_PFMI", "10年期国债期货"),
    "CFFEX.TS": ListingDate("2018-08-17", "DAY", "CFFEX_2024_PFMI", "2年期国债期货"),
    "CFFEX.TL": ListingDate("2023-04-21", "DAY", "CFFEX_2024_PFMI", "30年期国债期货"),
    "INE.SC": ListingDate("2018-03-26", "DAY", "SHFE_2024_INTERNATIONAL", "原油期货"),
    "INE.NR": ListingDate("2019-08-12", "DAY", "SHFE_2024_INTERNATIONAL", "20号胶期货"),
    "INE.LU": ListingDate("2020-06-22", "DAY", "SHFE_2024_INTERNATIONAL", "低硫燃料油期货"),
    "INE.BC": ListingDate("2020-11-19", "DAY", "SHFE_2024_INTERNATIONAL", "国际铜期货"),
    "INE.EC": ListingDate("2023-08-18", "DAY", "SHFE_2024_INTERNATIONAL", "集运指数（欧线）期货"),
    "SHFE.CU": ListingDate("1993-03", "MONTH", "SHFE_CU_HISTORY", "上海金属交易所推出一等铜标准合约交易"),
    "SHFE.AL": ListingDate("1993-11-26", "DAY", "SHFE_AL_HISTORY", "铝期货标准合约；不是1992年铝远期合约"),
    "SHFE.ZN": ListingDate("2007-03-26", "DAY", "SHFE_ZN_HISTORY", "锌期货上市"),
    "SHFE.PB": ListingDate("2011-03-24", "DAY", "SHFE_PB_NOTICE", "铅期货上市交易通知"),
    "SHFE.NI": ListingDate("2015-03-27", "DAY", "SHFE_NI_SN_HISTORY", "镍期货上市"),
    "SHFE.SN": ListingDate("2015-03-27", "DAY", "SHFE_NI_SN_HISTORY", "锡期货上市"),
    "SHFE.AU": ListingDate("2008-01-09", "DAY", "SHFE_AU_HISTORY", "黄金期货挂牌上市"),
    "SHFE.AG": ListingDate("2012-05-10", "DAY", "SHFE_AG_NOTICE", "白银期货上市交易通知"),
    "SHFE.RB": ListingDate("2009-03-27", "DAY", "SHFE_RB_WR_HISTORY", "螺纹钢期货上市"),
    "SHFE.WR": ListingDate("2009-03-27", "DAY", "SHFE_RB_WR_HISTORY", "线材期货上市"),
    "SHFE.HC": ListingDate("2014-03-21", "DAY", "SHFE_HC_HISTORY", "热轧卷板期货上市"),
    "SHFE.SS": ListingDate("2019-09-25", "DAY", "SHFE_SS_HISTORY", "不锈钢期货上市"),
    "SHFE.FU": ListingDate("2004-08-25", "DAY", "SHFE_FU_NEWS", "燃料油期货上市"),
    "SHFE.BU": ListingDate("2013-10-09", "DAY", "SHFE_BU_HISTORY", "石油沥青期货上市"),
    "SHFE.RU": ListingDate("1993-03", "MONTH", "SHFE_RU_HISTORY", "天然橡胶期货于1993年3月在上海上市"),
    "SHFE.SP": ListingDate("2018-11-27", "DAY", "SHFE_SP_HISTORY", "纸浆期货上市"),
    "SHFE.AO": ListingDate("2023-06-19", "DAY", "SHFE_AO_NOTICE", "氧化铝期货上市交易通知"),
    "SHFE.BR": ListingDate("2023-07-28", "DAY", "SHFE_BR_NEWS", "丁二烯橡胶期货日盘上市；期权当晚上市"),
    "SHFE.AD": ListingDate("2025-06-10", "DAY", "SHFE_AD_NOTICE", "铸造铝合金期货上市交易通知"),
    "SHFE.OP": ListingDate("2025-09-10", "DAY", "SHFE_OP_NOTICE", "胶版印刷纸期货上市交易通知"),
}


PENDING_OFFICIAL_LISTINGS = frozenset({
    "CFFEX.IZ", "GFEX.SI", "GFEX.LC", "GFEX.PT", "GFEX.PD",
})


def verified_listing_evidence(data_root: Path) -> dict[str, dict[str, str]]:
    """Return only listings backed by intact archived exchange source bytes.

    The returned shape is suitable for the existing listing-evidence writer;
    callers must persist ``precision`` rather than assume every date is a day.
    """
    raw_root = Path(data_root) / "bronze" / "futures_contract_listing"
    verified_sources: dict[str, tuple[OfficialSource, str]] = {}
    for source_id, source in SOURCES.items():
        path = raw_root / source.filename
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if sha256(payload).hexdigest() != source.digest:
            continue
        if source.filename.endswith(".pdf") and not payload.startswith(b"%PDF"):
            continue
        if source.filename.endswith(".html") and b"<html" not in payload[:500].lower() \
                and b"<head" not in payload[:500].lower():
            continue
        retrieved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        verified_sources[source_id] = (source, retrieved)

    result: dict[str, dict[str, str]] = {}
    for key, listing in LISTING_DATES.items():
        verified = verified_sources.get(listing.source_id)
        if verified is None:
            continue
        source, retrieved = verified
        result[key] = {
            "day": listing.value,
            "precision": listing.precision,
            "url": source.url,
            "sha": source.digest,
            "retrievedAt": retrieved,
            "note": listing.note,
            "rawPath": str(Path("bronze") / "futures_contract_listing" / source.filename),
        }
    return result
