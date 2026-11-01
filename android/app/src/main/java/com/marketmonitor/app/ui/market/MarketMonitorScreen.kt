package com.marketmonitor.app.ui.market

import android.content.res.Configuration
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.dp
import com.marketmonitor.app.data.ImportedInstrument
import com.marketmonitor.app.data.ImportedMarketData
import com.marketmonitor.app.data.WatchlistRepository
import com.marketmonitor.app.market.MarketOverview
import com.marketmonitor.app.ui.chart.Sparkline
import com.marketmonitor.app.ui.chart.TradingChartView
import com.marketmonitor.app.ui.theme.MarketTheme
import com.marketmonitor.app.ui.theme.MarketType
import com.marketmonitor.app.ui.theme.changeColor
import kotlinx.coroutines.launch

/** Modern, dense market page: compact status line + interactive K-line center. */
@Composable
fun MarketMonitorScreen(
    state: MarketImportUiState,
    marketData: ImportedMarketData?,
    onImport: (Uri) -> Unit,
    onSyncFromServer: (String) -> Unit,
    watchlistRepository: WatchlistRepository,
    activePackageId: String?,
    onCleanColdData: () -> Long,
) {
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) onImport(uri)
    }
    var syncDialogOpen by remember { mutableStateOf(false) }
    var cleanedBytes by remember { mutableStateOf<Long?>(null) }
    val overview = remember(marketData) { MarketOverview.compute(marketData) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = MarketTheme.dimensions.spacingMedium)
            .padding(top = MarketTheme.dimensions.spacingSmall, bottom = MarketTheme.dimensions.spacingLarge),
        verticalArrangement = Arrangement.spacedBy(MarketTheme.dimensions.spacingSmall),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(MarketTheme.dimensions.spacingSmall),
        ) {
            Button(
                onClick = { picker.launch(arrayOf("application/zip", "application/octet-stream")) },
                modifier = Modifier.weight(1f),
            ) {
                Text("导入行情包")
            }
            OutlinedButton(
                onClick = { syncDialogOpen = true },
                modifier = Modifier.weight(1f),
            ) {
                Text("从电脑同步")
            }
        }
        CompactStatusLine(
            status = state.dataStatus,
            cutoff = state.cutoff,
            quality = state.sourceAndQuality,
        )
        HorizontalDivider(color = MarketTheme.colorScheme.outlineVariant)
        val overviewText = if (overview.packageId == null) {
            "无已导入行情数据（不显示为零或正常）"
        } else {
            val staleText = if (overview.stale) "数据可能已陈旧" else "数据在阈值内"
            "标的 ${overview.instruments.size} · K线 ${overview.totalCandles} · 异常 ${overview.anomalyCount} · $staleText"
        }
        Text(
            text = overviewText,
            style = MarketTheme.typography.bodySmall,
            color = if (overview.anomalyCount > 0 || overview.packageId == null) {
                MarketTheme.colors.warning
            } else {
                MarketTheme.colorScheme.onSurfaceVariant
            },
        )
        MarketKlinePanel(
            marketData = marketData,
            hasImportedMarketData = state.hasImportedMarketData,
            watchlistRepository = watchlistRepository,
        )
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = {
                cleanedBytes = onCleanColdData()
            }) {
                Text("清理冷数据")
            }
            cleanedBytes?.let {
                Text(
                    text = "已释放 ${it / 1024} KB",
                    style = MarketTheme.typography.labelSmall,
                    color = MarketTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }

    if (syncDialogOpen) {
        SyncDialog(
            onConfirm = { url ->
                syncDialogOpen = false
                onSyncFromServer(url)
            },
            onDismiss = { syncDialogOpen = false },
        )
    }
}

@Composable
private fun CompactStatusLine(status: String, cutoff: String, quality: String) {
    Text(
        text = listOf(status, cutoff, quality).joinToString(" · "),
        style = MarketTheme.typography.bodySmall,
        color = MarketTheme.colorScheme.onSurfaceVariant,
        maxLines = 3,
    )
}

@Composable
private fun MarketKlinePanel(
    marketData: ImportedMarketData?,
    hasImportedMarketData: Boolean,
    watchlistRepository: WatchlistRepository,
) {
    val configuration = LocalConfiguration.current
    val chartHeight = if (configuration.orientation == Configuration.ORIENTATION_LANDSCAPE) {
        MarketTheme.dimensions.chartHeightLarge
    } else {
        MarketTheme.dimensions.chartHeight
    }
    val instruments = marketData?.instruments.orEmpty()
    val scope = rememberCoroutineScope()
    var watchlistIds by remember { mutableStateOf<List<String>>(emptyList()) }
    LaunchedEffect(marketData?.packageId) { watchlistIds = watchlistRepository.all() }
    var selectedInstrumentId by remember(marketData?.packageId) { mutableStateOf(instruments.firstOrNull()?.instrumentId) }
    val selectedInstrument = instruments.firstOrNull { it.instrumentId == selectedInstrumentId } ?: instruments.firstOrNull()
    var selectedPeriod by remember(selectedInstrument?.instrumentId) {
        mutableStateOf(preferredPeriod(selectedInstrument))
    }
    val periods = selectedInstrument?.candlesByPeriod?.keys.orEmpty().toList()
    val activePeriod = selectedPeriod.takeIf { it in periods } ?: periods.firstOrNull().orEmpty()
    val candles = selectedInstrument?.candlesByPeriod?.get(activePeriod).orEmpty()

    if (instruments.isNotEmpty() && selectedInstrument != null) {
        InstrumentSelector(instruments, selectedInstrument.instrumentId) { selectedInstrumentId = it }
        val inWatchlist = selectedInstrument.instrumentId in watchlistIds
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = {
                scope.launch {
                    if (inWatchlist) {
                        watchlistRepository.remove(selectedInstrument.instrumentId)
                    } else {
                        watchlistRepository.add(selectedInstrument.instrumentId)
                    }
                    watchlistIds = watchlistRepository.all()
                }
            }) {
                Text(if (inWatchlist) "移出自选" else "加入自选")
            }
            Text(
                text = "自选（${watchlistIds.size}）",
                style = MarketTheme.typography.labelSmall,
                color = MarketTheme.colorScheme.onSurfaceVariant,
            )
        }
        val watchlistRows = watchlistIds.mapNotNull { id ->
            instruments.firstOrNull { it.instrumentId == id }?.let(::quoteRowFor)
        }
        if (watchlistRows.isNotEmpty()) {
            Column(
                modifier = Modifier.fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(MarketTheme.dimensions.spacingSmall),
            ) {
                watchlistRows.forEach { row ->
                    QuoteRowView(row)
                }
            }
        } else {
            Text(
                text = "暂无自选：从上方标的选择器加入自选",
                style = MarketTheme.typography.bodySmall,
                color = MarketTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (periods.isNotEmpty()) {
            TabRow(
                selectedTabIndex = periods.indexOf(activePeriod).coerceAtLeast(0),
                containerColor = MarketTheme.colorScheme.surface,
                contentColor = MarketTheme.colorScheme.primary,
            ) {
                periods.forEach { period ->
                    Tab(
                        selected = period == activePeriod,
                        onClick = { selectedPeriod = period },
                        text = { Text(period, style = MarketTheme.typography.labelSmall) },
                    )
                }
            }
        }
        val sources = candles.map { it.source }.distinct().joinToString("、")
        val quality = candles.map { it.qualityStatus }.distinct().joinToString("、")
        Text(
            text = "${selectedInstrument.label} · $activePeriod · ${candles.size} 根",
            style = MarketTheme.typography.labelMedium,
            color = MarketTheme.colorScheme.onSurfaceVariant,
        )
        if (candles.isNotEmpty()) {
            Text(
                text = "来源：$sources；质量：$quality",
                style = MarketTheme.typography.labelSmall,
                color = MarketTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    val emptyMessage = if (hasImportedMarketData) {
        "行情包已导入，但当前标的没有可显示的 K 线"
    } else {
        "尚未导入行情数据"
    }
    TradingChartView(
        candles = candles,
        emptyMessage = emptyMessage,
        modifier = Modifier.fillMaxWidth(),
        height = chartHeight,
    )
}

@Composable
private fun QuoteRowView(row: QuoteRow) {
    val priceColor = if (row.latestPrice != null) {
        MarketTheme.colorScheme.onSurface
    } else {
        MarketTheme.colorScheme.onSurfaceVariant
    }
    val pctColor = if (row.changePct != null) {
        changeColor(row.changePct!!)
    } else {
        MarketTheme.colorScheme.onSurfaceVariant
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(MarketTheme.dimensions.spacingMedium),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = row.label,
                style = MarketTheme.typography.bodyMedium,
                color = MarketTheme.colorScheme.onSurface,
                maxLines = 1,
            )
            Text(
                text = row.code,
                style = MarketType.code,
                color = MarketTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
        }
        Column(horizontalAlignment = Alignment.End) {
            Text(
                text = row.latestPrice?.let(::formatQuotePrice) ?: "暂无数据",
                style = MarketType.numeric,
                color = priceColor,
                maxLines = 1,
            )
            Text(
                text = row.changePct?.let(::formatQuoteChangePct) ?: "暂无数据",
                style = MarketType.numericSmall,
                color = pctColor,
                maxLines = 1,
            )
        }
        Sparkline(
            points = row.sparklinePoints,
            modifier = Modifier
                .width(64.dp)
                .height(MarketTheme.dimensions.sparklineHeight),
            color = if (row.changePct != null) changeColor(row.changePct!!) else null,
        )
    }
}

@Composable
private fun InstrumentSelector(
    instruments: List<ImportedInstrument>,
    selectedInstrumentId: String,
    onSelect: (String) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    val selected = instruments.firstOrNull { it.instrumentId == selectedInstrumentId } ?: instruments.first()
    Box {
        TextButton(onClick = { expanded = true }) {
            Text(
                text = "标的：${selected.label}",
                style = MarketTheme.typography.labelLarge,
                maxLines = 1,
            )
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            instruments.forEach { instrument ->
                DropdownMenuItem(
                    text = { Text(instrument.label) },
                    onClick = {
                        onSelect(instrument.instrumentId)
                        expanded = false
                    },
                )
            }
        }
    }
}

@Composable
private fun SyncDialog(
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var serverUrl by remember { mutableStateOf("https://") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("从电脑同步", style = MarketTheme.typography.titleMedium) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(MarketTheme.dimensions.spacingSmall)) {
                Text(
                    "同步需要可验证证书的 HTTPS 地址；也可直接导入电脑生成的签名离线包。",
                    style = MarketTheme.typography.bodySmall,
                    color = MarketTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedTextField(
                    value = serverUrl,
                    onValueChange = { serverUrl = it },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                    label = { Text("电脑后端地址") },
                    placeholder = { Text("https://<电脑地址>") },
                )
            }
        },
        confirmButton = {
            Button(onClick = { onConfirm(serverUrl) }) {
                Text("下载并导入同步包")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        },
    )
}
