package com.marketmonitor.app.ui.market

import androidx.work.WorkInfo
import com.marketmonitor.app.data.ImportedInstrument
import com.marketmonitor.app.data.MarketPackageImportWorker

/** Import/sync status surfaced by the market page. */
data class MarketImportUiState(
    val dataStatus: String = "尚未导入已验证行情包",
    val cutoff: String = "暂无已验证数据截止时间",
    val sourceAndQuality: String = "导入成功后显示校验结果",
    val hasImportedMarketData: Boolean = false,
)

fun stateForWork(info: WorkInfo): MarketImportUiState = when (info.state) {
    WorkInfo.State.ENQUEUED -> MarketImportUiState(dataStatus = "行情包已加入队列，等待验证")
    WorkInfo.State.RUNNING -> MarketImportUiState(dataStatus = "正在验证签名、哈希和数据载荷")
    WorkInfo.State.SUCCEEDED -> MarketImportUiState(
        dataStatus = "行情包已验证并导入：${info.outputData.getString(MarketPackageImportWorker.RESULT_PACKAGE_ID) ?: "未命名"}",
        cutoff = info.outputData.getString(MarketPackageImportWorker.RESULT_DATA_CUTOFF) ?: "未记录",
        sourceAndQuality = "签名、哈希和载荷校验已通过",
        hasImportedMarketData = true,
    )
    WorkInfo.State.FAILED -> {
        val errorCode = info.outputData.getString(MarketPackageImportWorker.RESULT_ERROR)
        val detail = info.outputData.getString(MarketPackageImportWorker.RESULT_ERROR_DETAIL)
        val detailSuffix = detail?.takeIf { it.isNotBlank() }?.let { "：$it" } ?: ""
        MarketImportUiState(dataStatus = "导入失败：${validationErrorText(errorCode)}$detailSuffix")
    }
    WorkInfo.State.CANCELLED -> MarketImportUiState(dataStatus = "导入任务已取消")
    WorkInfo.State.BLOCKED -> MarketImportUiState(dataStatus = "导入任务被阻塞")
}

fun validationErrorText(value: String?): String = when (value) {
    "SIGNATURE" -> "签名校验未通过"
    "HASH" -> "文件哈希校验未通过"
    "SCHEMA" -> "行情包结构版本不受支持"
    "SPACE" -> "设备可用空间不足"
    "DUPLICATE" -> "该行情包已导入"
    "DOWNGRADE" -> "行情包版本不兼容或早于当前数据"
    "UNSUPPORTED_DELTA" -> "当前版本不支持增量行情包，请导入完整包"
    "PAYLOAD" -> "行情数据载荷校验未通过"
    else -> "行情包结构无效"
}

fun preferredPeriod(instrument: ImportedInstrument?): String = when {
    instrument == null -> ""
    "1d" in instrument.candlesByPeriod -> "1d"
    else -> instrument.candlesByPeriod.keys.firstOrNull().orEmpty()
}
