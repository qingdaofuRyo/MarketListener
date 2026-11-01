package com.marketmonitor.app

import android.net.Uri
import android.os.Bundle
import android.os.StatFs
import android.content.pm.ApplicationInfo
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.work.Data
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.ExistingWorkPolicy
import com.marketmonitor.app.data.BoundedPackageIO
import com.marketmonitor.app.data.DatabaseBoundary
import com.marketmonitor.app.data.ImportedMarketData
import com.marketmonitor.app.data.ImportedMarketDataReader
import com.marketmonitor.app.data.MarketPackageImportWorker
import com.marketmonitor.app.data.MarketPackageLimits
import com.marketmonitor.app.data.PackageDownloader
import com.marketmonitor.app.data.PackageDownloadPolicy
import com.marketmonitor.app.graph.GraphRepository
import com.marketmonitor.app.graph.GraphSearchState
import com.marketmonitor.app.graph.applyQuery
import com.marketmonitor.app.graph.loaded
import com.marketmonitor.app.graph.selectEntity
import com.marketmonitor.app.graph.selectRelationship
import com.marketmonitor.app.market.StoragePolicy
import com.marketmonitor.app.ui.app.MarketListenerApp
import com.marketmonitor.app.ui.market.MarketImportUiState
import com.marketmonitor.app.ui.market.stateForWork
import com.marketmonitor.app.ui.theme.ThemeRepository
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.atomic.AtomicLong

class MainActivity : ComponentActivity() {
    private var importState by mutableStateOf(MarketImportUiState())
    private var marketData by mutableStateOf<ImportedMarketData?>(null)
    private var graphRepository by mutableStateOf<GraphRepository?>(null)
    private var graphState by mutableStateOf(GraphSearchState())
    private var industryAtlasFile by mutableStateOf<File?>(null)
    private val importRequestSequence = AtomicLong()

    private val themeRepository: ThemeRepository by lazy { ThemeRepository(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        installSplashScreen()
        cleanupAbandonedPackageInputs()
        importState = restoredImportState()
        refreshMarketData()
        refreshGraphSnapshot()
        refreshIndustryHtml()
        enableEdgeToEdge()
        setContent {
            MarketListenerApp(
                themeRepository = themeRepository,
                importState = importState,
                marketData = marketData,
                onImport = ::enqueueImport,
                onSyncFromServer = ::enqueueSyncFromServer,
                activePackageId = marketData?.packageId,
                onCleanColdData = {
                    val coldRoot = DatabaseBoundary.coldDirectory(applicationContext)
                    val availableBytes = runCatching { StatFs(coldRoot.absolutePath).availableBytes }
                        .getOrDefault(Long.MAX_VALUE)
                    StoragePolicy.cleanup(
                        coldRoot = coldRoot.toPath(),
                        activePackageIds = setOfNotNull(marketData?.packageId),
                        availableBytes = availableBytes,
                        reserveBytes = 64L * 1024L * 1024L,
                    ).freedBytes
                },
                graphRepository = graphRepository,
                graphState = graphState,
                onGraphQueryChange = { keyword ->
                    graphState = graphState.applyQuery(graphRepository, keyword)
                },
                onGraphSelectEntity = { entityId ->
                    graphState = graphState.selectEntity(graphRepository, entityId)
                },
                onGraphSelectRelationship = { relationshipId ->
                    graphState = graphState.selectRelationship(graphRepository, relationshipId)
                },
                onGraphImport = ::enqueueGraphImport,
                industryAtlasFile = industryAtlasFile,
            )
        }
    }

    private fun enqueueImport(uri: Uri) {
        val sequence = importRequestSequence.incrementAndGet()
        importState = MarketImportUiState(dataStatus = "正在读取所选行情包")
        Thread {
            var target: File? = null
            try {
                val partial = newPackageInput("selected-")
                target = partial
                val limits = MarketPackageLimits()
                val budget = BoundedPackageIO.archiveCopyBudget(StatFs(partial.parentFile?.absolutePath ?: error("缺少暂存目录")).availableBytes, limits)
                require(budget > 0L) { "设备可用空间不足" }
                contentResolver.openInputStream(uri)?.use { input ->
                    partial.outputStream().use { output -> BoundedPackageIO.copyAndHash(input, output, budget) }
                } ?: throw IllegalArgumentException("无法读取所选文件")
                val complete = sealPackageInput(partial)
                target = null
                runOnUiThread { enqueuePackageFile(complete, sequence) }
            } catch (_: Exception) {
                target?.delete()
                runOnUiThread { if (sequence == importRequestSequence.get()) importState = MarketImportUiState(dataStatus = "读取行情包失败或超过大小限额") }
            }
        }.start()
    }

    private fun enqueueSyncFromServer(rawUrl: String) {
        val sequence = importRequestSequence.incrementAndGet()
        val url = try { PackageDownloadPolicy.packageUrl(rawUrl, applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) }
        catch (error: Exception) { importState = MarketImportUiState(dataStatus = error.message ?: "服务器地址无效"); return }
        importState = MarketImportUiState(dataStatus = "正在从电脑下载同步包")
        Thread {
            var target: File? = null
            try {
                val partial = newPackageInput("synced-")
                target = partial
                PackageDownloader().download(url, partial)
                val complete = sealPackageInput(partial)
                target = null
                runOnUiThread { enqueuePackageFile(complete, sequence) }
            } catch (error: Exception) {
                target?.delete()
                runOnUiThread {
                    if (sequence == importRequestSequence.get()) importState = MarketImportUiState(
                        dataStatus = "从电脑同步失败：${error.message ?: "未知错误"}",
                    )
                }
            }
        }.start()
    }

    private fun newPackageInput(prefix: String): File {
        val root = File(cacheDir, "market-package-inputs").apply { mkdirs() }
        return File.createTempFile(prefix, ".part", root)
    }

    private fun sealPackageInput(partial: File): File {
        val complete = File(partial.parentFile, partial.name.removeSuffix(".part") + ".zip")
        Files.move(partial.toPath(), complete.toPath(), StandardCopyOption.ATOMIC_MOVE)
        return complete
    }

    private fun cleanupAbandonedPackageInputs() {
        val cutoff = System.currentTimeMillis() - 24L * 60L * 60L * 1000L
        File(cacheDir, "market-package-inputs").listFiles()?.forEach { file ->
            if (file.isFile && file.extension == "part" && file.lastModified() < cutoff) file.delete()
        }
    }

    private fun enqueuePackageFile(target: File, sequence: Long) {
        if (sequence != importRequestSequence.get()) { target.delete(); return }
        importState = MarketImportUiState(dataStatus = "行情包已加入队列，正在验证")
        val request = OneTimeWorkRequestBuilder<MarketPackageImportWorker>()
            .setInputData(Data.Builder().putString("package_path", target.path).build())
            .build()
        val workManager = WorkManager.getInstance(this)
        try {
            workManager.enqueueUniqueWork("market-package-import", ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        } catch (_: Exception) {
            target.delete()
            importState = MarketImportUiState(dataStatus = "行情包暂时无法加入验证队列")
            return
        }
        workManager.getWorkInfoByIdLiveData(request.id).observe(this) { info ->
            if (info != null && sequence == importRequestSequence.get()) {
                importState = stateForWork(info)
                if (info.state == WorkInfo.State.SUCCEEDED) {
                    refreshMarketData()
                    refreshIndustryHtml()
                }
            }
        }
    }

    private fun restoredImportState(): MarketImportUiState {
        val preferences = getSharedPreferences("market-package", MODE_PRIVATE)
        val packageId = preferences.getString("active", null) ?: return MarketImportUiState()
        val cutoff = preferences.getString("active_cutoff", null) ?: "未记录"
        return MarketImportUiState(
            dataStatus = "已启用已验证行情包：$packageId",
            cutoff = cutoff,
            sourceAndQuality = "签名、哈希和载荷校验已通过",
            hasImportedMarketData = true,
        )
    }

    private fun refreshMarketData() {
        Thread {
            val snapshot = try {
                ImportedMarketDataReader(this).readActive()
            } catch (_: Exception) {
                null
            }
            runOnUiThread { marketData = snapshot }
        }.start()
    }

    private fun enqueueGraphImport(uri: Uri) {
        Thread {
            try {
                val target = File(filesDir, "graph-snapshot.json")
                contentResolver.openInputStream(uri)?.use { input ->
                    target.outputStream().use(input::copyTo)
                } ?: throw IllegalArgumentException("无法读取所选文件")
                runOnUiThread { refreshGraphSnapshot() }
            } catch (_: Exception) {
                runOnUiThread { graphState = graphState.copy(error = "导入图谱快照失败") }
            }
        }.start()
    }

    private fun refreshGraphSnapshot() {
        Thread {
            val repository = try {
                val file = File(filesDir, "graph-snapshot.json")
                if (file.isFile) GraphRepository.fromJson(file.readText()) else null
            } catch (_: Exception) {
                null
            }
            runOnUiThread {
                graphRepository = repository
                graphState = graphState.loaded(repository)
            }
        }.start()
    }

    private fun refreshIndustryHtml() {
        Thread {
            val activePackageId = getSharedPreferences("market-package", MODE_PRIVATE)
                .getString("active", null)
            val packageRoot = activePackageId?.let {
                File(DatabaseBoundary.coldDirectory(this), "packages/$it/industry")
            }
            fun copyAsset(name: String): File? {
                val target = File(filesDir, name)
                val source = packageRoot?.let { File(it, name) }
                return try {
                    if (source != null && source.isFile) {
                        source.copyTo(target, overwrite = true)
                        target
                    } else {
                        null
                    }
                } catch (_: Exception) {
                    null
                }
            }
            val atlasFile = copyAsset("industry-atlas.html")
            runOnUiThread {
                industryAtlasFile = atlasFile
            }
        }.start()
    }
}
