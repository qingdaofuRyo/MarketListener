/** Shared column-major order. Missing observations retain their original slot. */
export const quoteFieldGroups = [
  ['开', '收'], ['高', '低'], ['涨幅', '振幅'], ['涨跌', '结'],
  ['量', '额'], ['持仓量', '沉淀资金'], ['总市值', '流通市值'],
] as const;
