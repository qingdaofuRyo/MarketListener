package com.marketmonitor.app.data

import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest

/** Limits allow the existing 110 MiB expanded packages without allocating a payload in memory. */
data class MarketPackageLimits(
    val maxEntries: Int = 128,
    val maxArchiveBytes: Long = 512L * 1024L * 1024L,
    val maxExpandedBytes: Long = 2L * 1024L * 1024L * 1024L,
    val maxManifestBytes: Long = 1024L * 1024L,
    val maxHtmlBytes: Long = 64L * 1024L * 1024L,
    val maxQualityReportBytes: Long = 16L * 1024L * 1024L,
    val maxCompressionRatio: Long = 200L,
    val reserveBytes: Long = 64L * 1024L * 1024L,
) {
    init {
        require(maxEntries > 0)
        require(listOf(maxArchiveBytes, maxExpandedBytes, maxManifestBytes, maxHtmlBytes, maxQualityReportBytes).all { it > 0 })
        require(maxCompressionRatio > 0 && reserveBytes >= 0)
    }

    fun entryLimit(name: String): Long = when (name) {
        "manifest.json" -> maxManifestBytes
        "signature.ed25519", "signature.ecdsa" -> 1024L
        "quality-report.json" -> maxQualityReportBytes
        "industry/industry-map.html", "industry/industry-atlas.html" -> maxHtmlBytes
        "payload.sqlite" -> maxExpandedBytes
        else -> 0L
    }

    /** Large highly compressible entries can consume the full extraction budget from a tiny ZIP. */
    fun hasAcceptableCompression(size: Long, compressedSize: Long): Boolean =
        size <= 1024L * 1024L ||
            (compressedSize > 0L && size <= compressedSize * maxCompressionRatio)
}

data class StreamDigest(val bytes: Long, val sha256: String)

class PackageSizeException(message: String) : IllegalArgumentException(message)
class PackageZipStructureException(message: String) : IllegalArgumentException(message)

object BoundedPackageIO {
    /** Download/copy and verification snapshot coexist until import; reserve space for both. */
    fun archiveCopyBudget(availableBytes: Long, limits: MarketPackageLimits): Long =
        minOf(limits.maxArchiveBytes, ((availableBytes - limits.reserveBytes).coerceAtLeast(0L)) / 2L)

    /** Reject huge central-directory inventories before ZipFile allocates metadata for every entry. */
    fun validateZipInventory(file: File, maxEntries: Int) {
        RandomAccessFile(file, "r").use { input ->
            val length = input.length()
            if (length < 22L) throw PackageZipStructureException("ZIP 缺少目录尾记录")
            val tail = ByteArray(minOf(length, 65_557L).toInt())
            input.seek(length - tail.size)
            input.readFully(tail)
            fun unsigned(offset: Int, size: Int): Long = (0 until size).fold(0L) { value, index -> value or ((tail[offset + index].toLong() and 255L) shl (8 * index)) }
            val offset = (tail.size - 22 downTo 0).firstOrNull { index ->
                unsigned(index, 4) == 0x06054b50L && index + 22 + unsigned(index + 20, 2) == tail.size.toLong()
            } ?: throw PackageZipStructureException("ZIP 目录尾记录无效")
            val entries = unsigned(offset + 10, 2)
            val directoryBytes = unsigned(offset + 12, 4)
            val directoryOffset = unsigned(offset + 16, 4)
            if (unsigned(offset + 4, 2) != 0L || unsigned(offset + 6, 2) != 0L || unsigned(offset + 8, 2) != entries || entries !in 1L..maxEntries.toLong()) {
                throw PackageZipStructureException("ZIP 分卷、ZIP64 或条目数量不受支持")
            }
            if (directoryBytes > 1024L * 1024L || directoryOffset + directoryBytes > length - tail.size + offset) {
                throw PackageZipStructureException("ZIP 中央目录越界或过大")
            }
        }
    }
    /** Check before writing, so an oversized chunk never extends a staging file beyond its budget. */
    fun copyAndHash(
        input: InputStream,
        output: OutputStream? = null,
        maxBytes: Long,
        deadlineNanos: Long = Long.MAX_VALUE,
        clock: () -> Long = System::nanoTime,
    ): StreamDigest {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(64 * 1024)
        var bytes = 0L
        while (true) {
            if (Thread.currentThread().isInterrupted) throw InterruptedException("行情包操作已取消")
            if (clock() > deadlineNanos) throw PackageSizeException("行情包传输超时")
            val count = input.read(buffer)
            if (count < 0) break
            if (count == 0) continue
            if (count.toLong() > maxBytes - bytes) throw PackageSizeException("行情包内容超过大小限额")
            output?.write(buffer, 0, count)
            digest.update(buffer, 0, count)
            bytes += count
        }
        return StreamDigest(bytes, digest.digest().joinToString("") { "%02x".format(it) })
    }

    fun readSmall(input: InputStream, maxBytes: Long): ByteArray = ByteArrayOutputStream().use { output ->
        copyAndHash(input, output, maxBytes)
        output.toByteArray()
    }
}

/** The mutable external/cache input is never reused after a private snapshot has been frozen. */
object PackageInputSnapshot {
    fun freeze(source: File, directory: File, availableBytes: Long, limits: MarketPackageLimits = MarketPackageLimits()): File {
        val size = source.length()
        val modified = source.lastModified()
        if (size <= 0 || size > limits.maxArchiveBytes) throw PackageSizeException("行情包压缩体积超过限额或为空")
        if (size > availableBytes || limits.reserveBytes > availableBytes - size) throw PackageSizeException("没有足够空间创建独立校验副本")
        check(directory.mkdirs() || directory.isDirectory) { "无法创建受控暂存区" }
        val snapshot = File.createTempFile("verify-", ".zip", directory)
        try {
            source.inputStream().use { input -> snapshot.outputStream().use { output ->
                val copied = BoundedPackageIO.copyAndHash(input, output, limits.maxArchiveBytes)
                require(copied.bytes == size && source.length() == size && source.lastModified() == modified) { "读取期间行情包已变化" }
            } }
            check(snapshot.setReadOnly()) { "无法保护独立校验副本" }
            return snapshot
        } catch (error: Exception) {
            snapshot.delete()
            throw error
        }
    }
}
