# 上期所与上期能源 2023 早期交易单位原件核验

本记录补充 `Official_Daily_Evidence_20261003.md`。不读取或写入生产库。所有证据为实际下载的官网 PDF；注册表仅扩展已核验的交易单位，不采用手册中的旧保证金率、涨跌幅或交割日期作为每日参数。

## 原件和验证位置

PDF 页码从1开始，表示文件实际页面而非页内印刷页码。原件、提取文本、SHA清单位于忽略的 `data_control/bronze/official_daily_evidence/`。

| 原件 | 产品及明确单位 | PDF 页码 | SHA256 |
| --- | --- | --- | --- |
| [上期所有色金属手册2019](https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/manual/201812/P020240320681343651143.pdf) | CU/AL/ZN/PB各5吨；SN/NI各1吨 | 29/30/31/32/33/34 | `f6118cffcb14b05bd909dde0462c0fcf492c44f65e5bb9c4c56f4dbd79ae04b8` |
| [贵金属手册2018](https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/ag_f/manual/201807/P020240320678993906997.pdf) | AG15千克 | 45 | `37459f2a3516e2cf576a727d4d3fa90d54fe4e917b322c3c9fc054679ebf1543` |
| [能源系列手册2016](https://www.shfe.com.cn/products/futures/energyandchemical/bu_f/manual/201612/P020240320671515046775.pdf) | BU10吨 | 8 | `2ef7d4ce005498e9b5f6609413d74a7d09c325c5cd2ce155964771cf790d215a` |
| [燃料油手册2018](https://www.shfe.com.cn/products/futures/energyandchemical/fu_f/manual/202206/P020240320705452680030.pdf) | FU10吨 | 27 | `d9d8ecc6413748d488a7b2c61aca5b51897c8db28fb3a641d1c211daa00e6459` |
| [钢材手册2018](https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/rb_f/manual/201612/P020240320671556848025.pdf) | RB/WR/HC各10吨 | 25/26/28 | `75ae0352f333af1331be5ddb47ca3331a1957a7006798ad18c7c59dacbd05314` |
| [纸浆手册2018](https://www.shfe.com.cn/products/futures/energyandchemical/sp_f/manual/201811/P020240320680677010005.pdf) | SP10吨 | 15 | `802ee146013ddb44345aae9ec2b64b7665f1e2ed473bf70dc52f4c8ba1f140df` |
| [不锈钢手册2019](https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/ss_f/manual/201909/P020240320684500937834.pdf) | SS5吨 | 18 | `407372dddc3b959286961ce998cd06c0b8e423e29814c333b04f882f0ef8354a` |
| [原油操作手册2022](https://www.shfe.com.cn/upload/20220830/1661841151270.pdf) | SC1000桶 | 69 | `5678be465315cd9155c50aa9b8e8e4c28c85591d38392c4b709080bd58d76c5a` |
| [天然橡胶期权手册2020](https://www.shfe.com.cn/upload/20201023/1603414826433.pdf) | 明确标的天然橡胶期货合约为10吨；未将期权行导入行情 | 7 | `88b401f71914fdb155c75558d6906549081949645780429a3ddb8b14495f129a` |

官网介绍页将这些文件明确标为2016、2018、2019、2020或2022版，PDF标准合约条文列出上述单位。下载地址中 `P020240320` 属网站迁移后的文件路径，不作为2024生效证明。注册表将上述16个品种的本任务范围起始日设置为2023-01-01；它们与2023-06-19之后带日期的官方日报备注及后续官方合约文本单位一致。

## 不可混用的旧规定

能源系列2016手册第9页燃料油交易单位为50吨，2018版燃料油手册第27页已为10吨。本次 FU 使用2018版原件和后续日期备注交叉核对，绝不因“官网文件”身份而选择更早的50吨值。手册中保证金、交割品级与涨跌停板可能早已调整，不扩大这些字段的有效期。

不同交易所单位和报价必须配对：AG按元/千克计价使用15千克，SC按元/桶使用1000桶。名义规模与保证金沉淀资金不同；本次乘数不提供任何保证金率。

新上市的 AO、BR、AD、OP、EC 不从上述老手册继承2023-01-01。AU、LU、NR、BC 已有更早官方合约文本证据，仍沿用其原有证据，不重复覆盖。
