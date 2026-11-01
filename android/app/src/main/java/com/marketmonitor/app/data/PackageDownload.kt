package com.marketmonitor.app.data

import android.os.StatFs
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

object PackageDownloadPolicy {
    const val CONNECT_TIMEOUT_MS = 10_000
    const val READ_TIMEOUT_MS = 30_000
    const val TOTAL_TIMEOUT_MS = 300_000L
    private val DEBUG_HOSTS = setOf("localhost", "127.0.0.1", "10.0.2.2", "[::1]", "::1")

    fun packageUrl(rawUrl: String, debugBuild: Boolean): URL {
        val uri = URI(rawUrl.trim())
        require(uri.scheme in setOf("https", "http") && !uri.host.isNullOrBlank()) { "服务器地址必须为有效的 HTTPS 地址" }
        require(uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null) { "服务器地址不能包含账号、查询参数或片段" }
        require(uri.rawPath.isNullOrEmpty() || uri.rawPath == "/") { "服务器地址只填写协议、主机和端口" }
        if (uri.scheme == "http") {
            require(debugBuild && uri.host.lowercase() in DEBUG_HOSTS && uri.port == 8765) { "仅调试版允许本机模拟器调试地址的 HTTP 连接；正式版请使用 HTTPS 或离线包" }
        }
        return URI(uri.scheme, null, uri.host, uri.port, "/api/android-package", null, null).toURL()
    }

    fun validateResponse(status: Int, contentType: String?, contentLength: Long, maxBytes: Long) {
        require(status == HttpURLConnection.HTTP_OK) { if (status in 300..399) "同步包重定向被拒绝" else "同步包下载失败：HTTP $status" }
        val mediaType = contentType?.substringBefore(';')?.trim()?.lowercase()
        require(mediaType in setOf("application/zip", "application/octet-stream", "application/x-zip-compressed")) { "服务器未返回行情 ZIP 文件" }
        require(contentLength != 0L && contentLength <= maxBytes) { "同步包为空或超过下载限额" }
    }
}

/** Uses platform certificate validation, forbids redirects, and bounds bytes and total elapsed time. */
class PackageDownloader(private val limits: MarketPackageLimits = MarketPackageLimits()) {
    fun download(url: URL, target: File): StreamDigest {
        val maxDownloadBytes = BoundedPackageIO.archiveCopyBudget(
            StatFs(target.parentFile?.absolutePath ?: error("缺少下载暂存目录")).availableBytes, limits,
        )
        require(maxDownloadBytes > 0L) { "设备可用空间不足以下载和验证行情包" }
        val connection = url.openConnection() as HttpURLConnection
        connection.connectTimeout = PackageDownloadPolicy.CONNECT_TIMEOUT_MS
        connection.readTimeout = PackageDownloadPolicy.READ_TIMEOUT_MS
        connection.instanceFollowRedirects = false
        connection.setRequestProperty("Accept", "application/zip")
        connection.setRequestProperty("Accept-Encoding", "identity")
        val watchdog = Executors.newSingleThreadScheduledExecutor { task -> Thread(task, "market-package-download-deadline").apply { isDaemon = true } }
        val totalDeadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(PackageDownloadPolicy.TOTAL_TIMEOUT_MS)
        val timeout = watchdog.schedule({ connection.disconnect() }, PackageDownloadPolicy.TOTAL_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        try {
            PackageDownloadPolicy.validateResponse(connection.responseCode, connection.contentType, connection.contentLengthLong, maxDownloadBytes)
            val result = connection.inputStream.use { input -> target.outputStream().use { output ->
                BoundedPackageIO.copyAndHash(input, output, maxDownloadBytes, totalDeadline)
            } }
            require(result.bytes > 0L && (connection.contentLengthLong < 0 || result.bytes == connection.contentLengthLong)) { "同步包下载不完整" }
            return result
        } catch (error: Exception) {
            target.delete()
            throw error
        } finally {
            timeout.cancel(false)
            watchdog.shutdownNow()
            connection.disconnect()
        }
    }
}
