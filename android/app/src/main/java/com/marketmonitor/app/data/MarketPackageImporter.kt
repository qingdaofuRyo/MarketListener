package com.marketmonitor.app.data

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.os.StatFs
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.JsonParser
import com.fasterxml.jackson.core.JsonProcessingException
import com.fasterxml.jackson.core.JsonToken
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.nio.file.Path
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.GeneralSecurityException
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.time.OffsetDateTime
import java.util.Base64
import java.util.UUID
import java.util.concurrent.locks.ReentrantLock
import java.util.zip.ZipException
import java.util.zip.ZipFile
import kotlin.concurrent.withLock

enum class PackageValidationError { SIGNATURE, HASH, SCHEMA, SPACE, DUPLICATE, DOWNGRADE, STRUCTURE, PAYLOAD, UNSUPPORTED_DELTA }

data class VerifiedMarketPackage(
    val packageId: String,
    val dataCutoff: String,
    val generatedAt: String = "",
    val expandedBytes: Long = 0L,
)
data class PackageValidationResult(
    val verified: VerifiedMarketPackage? = null,
    val error: PackageValidationError? = null,
    val errorDetail: String? = null,
)

private class PackageCheckException(val error: PackageValidationError, message: String) : IllegalArgumentException(message)
private data class DeclaredFile(val name: String, val bytes: Long, val sha256: String)

class MarketPackageVerifier(
    private val publicKeyPem: String,
    private val appVersion: String,
    private val ecdsaPublicKeyPem: String? = null,
    private val limits: MarketPackageLimits = MarketPackageLimits(),
) {
    fun verify(
        packageFile: File,
        availableBytes: Long,
        importedPackageIds: Set<String>,
        activeCutoff: String? = null,
        activeGeneratedAt: String? = null,
        activePackageId: String? = null,
    ): PackageValidationResult = verifyArchive(packageFile, availableBytes, importedPackageIds, activeCutoff, activeGeneratedAt, activePackageId, null)

    /** Hashing and extraction use one ZIP handle on the importer's private immutable snapshot. */
    internal fun verifyAndExtract(
        packageFile: File,
        availableBytes: Long,
        importedPackageIds: Set<String>,
        activeCutoff: String?,
        activeGeneratedAt: String?,
        activePackageId: String?,
        destination: File,
    ): PackageValidationResult = verifyArchive(packageFile, availableBytes, importedPackageIds, activeCutoff, activeGeneratedAt, activePackageId, destination)

    private fun verifyArchive(
        packageFile: File,
        availableBytes: Long,
        importedPackageIds: Set<String>,
        activeCutoff: String?,
        activeGeneratedAt: String?,
        activePackageId: String?,
        destination: File?,
    ): PackageValidationResult = try {
        checkPackage(packageFile.isFile && packageFile.length() in 1..limits.maxArchiveBytes, PackageValidationError.SPACE, "行情包为空或压缩体积超过限额")
        BoundedPackageIO.validateZipInventory(packageFile, limits.maxEntries)
        ZipFile(packageFile).use { archive ->
            val names = mutableSetOf<String>()
            val entries = archive.entries()
            var entryCount = 0
            while (entries.hasMoreElements()) {
                checkPackage(++entryCount <= limits.maxEntries, PackageValidationError.STRUCTURE, "压缩包条目过多")
                val entry = entries.nextElement()
                checkPackage(!entry.isDirectory && entry.name in ALLOWED_ENTRIES, PackageValidationError.STRUCTURE, "压缩包含不允许的条目")
                checkPackage(names.add(entry.name), PackageValidationError.STRUCTURE, "压缩包条目重复")
                checkPackage(entry.size >= 0 && entry.size <= limits.entryLimit(entry.name), PackageValidationError.SPACE, "压缩包条目体积超过限额")
                checkPackage(limits.hasAcceptableCompression(entry.size, entry.compressedSize), PackageValidationError.SPACE, "压缩包条目压缩比异常")
            }
            checkPackage("manifest.json" in names && names.any { it in SIGNATURE_ENTRIES }, PackageValidationError.STRUCTURE, "缺少清单或受信任签名")
            val manifest = archive.getInputStream(archive.getEntry("manifest.json")).use { BoundedPackageIO.readSmall(it, limits.maxManifestBytes) }
            checkPackage(verifySignatures(manifest, archive, names), PackageValidationError.SIGNATURE, "签名验证未通过（Ed25519/ECDSA）")
            val json = parseManifest(manifest)
            checkKeys(json, MANIFEST_KEYS)
            checkPackage(integer(json, "schema_version") == 1L, PackageValidationError.SCHEMA, "不支持的 schema_version")
            val packageId = text(json, "package_id")
            checkPackage(SAFE_ID.matches(packageId), PackageValidationError.STRUCTURE, "package_id 不是安全的目录标识")
            checkPackage(packageId !in importedPackageIds, PackageValidationError.DUPLICATE, "该行情包已导入")
            val minimumVersion = text(json, "minimum_app_version")
            checkPackage(VERSION.matches(minimumVersion) && VERSION.matches(appVersion), PackageValidationError.SCHEMA, "App 版本格式无效")
            checkPackage(compareVersions(minimumVersion, appVersion) <= 0, PackageValidationError.DOWNGRADE, "行情包要求更高版本的 App")
            val generatedAt = text(json, "generated_at")
            val generated = timestamp(generatedAt)
            val packageType = if (json.has("package_type")) text(json, "package_type") else "FULL"
            checkPackage(packageType in setOf("FULL", "DELTA"), PackageValidationError.SCHEMA, "未知行情包类型")
            if (packageType == "DELTA") {
                val base = text(json, "base_package_id")
                checkPackage(SAFE_ID.matches(base) && base == activePackageId && base in importedPackageIds, PackageValidationError.STRUCTURE, "增量包的基包未激活或不匹配")
                // The current reader consumes one complete payload and has no safe overlay implementation.
                throw PackageCheckException(PackageValidationError.UNSUPPORTED_DELTA, "当前 Android 只支持完整行情包，增量包不能覆盖基包")
            }
            checkPackage(!json.has("base_package_id"), PackageValidationError.SCHEMA, "完整行情包不能声明增量基包")
            val summaries = json.getJSONArray("source_run_summaries")
            for (index in 0 until summaries.length()) {
                val summary = summaries.getJSONObject(index)
                checkKeys(summary, SUMMARY_KEYS)
                text(summary, "run_id"); text(summary, "provider")
                checkPackage(text(summary, "status") in SOURCE_STATUSES, PackageValidationError.SCHEMA, "来源状态无效")
            }
            val partitions = json.getJSONArray("partitions")
            checkPackage(partitions.length() > 0, PackageValidationError.SCHEMA, "清单缺少数据分区")
            val partitionIds = mutableSetOf<String>()
            val declared = linkedMapOf<String, DeclaredFile>()
            var cutoff = ""
            var cutoffInstant: java.time.Instant? = null
            var expandedBytes = manifest.size.toLong()
            for (index in 0 until partitions.length()) {
                val partition = partitions.getJSONObject(index)
                checkKeys(partition, PARTITION_KEYS)
                checkPackage(partitionIds.add(text(partition, "partition_id")), PackageValidationError.SCHEMA, "分区标识重复")
                val value = text(partition, "data_cutoff")
                val instant = timestamp(value)
                if (cutoffInstant == null || instant > cutoffInstant) { cutoff = value; cutoffInstant = instant }
                val files = partition.getJSONArray("files")
                checkPackage(files.length() > 0, PackageValidationError.SCHEMA, "分区缺少文件声明")
                for (fileIndex in 0 until files.length()) {
                    val expected = files.getJSONObject(fileIndex)
                    checkKeys(expected, FILE_KEYS)
                    val name = text(expected, "name")
                    checkPackage(name in CONTENT_ENTRIES && !declared.containsKey(name), PackageValidationError.STRUCTURE, "文件声明越界或重复")
                    val bytes = integer(expected, "bytes")
                    checkPackage(bytes > 0 && bytes <= limits.entryLimit(name), PackageValidationError.SPACE, "文件声明体积超过限额")
                    checkPackage(integer(expected, "row_count") >= 0, PackageValidationError.SCHEMA, "文件行数无效")
                    val sha = text(expected, "sha256")
                    checkPackage(HASH.matches(sha), PackageValidationError.SCHEMA, "SHA-256 格式无效")
                    checkPackage(bytes <= limits.maxExpandedBytes - expandedBytes, PackageValidationError.SPACE, "总解压体积超过限额")
                    expandedBytes += bytes
                    declared[name] = DeclaredFile(name, bytes, sha)
                }
            }
            checkPackage(declared.keys.containsAll(REQUIRED_CONTENT), PackageValidationError.STRUCTURE, "缺少载荷或质量报告的签名声明")
            checkPackage(names - SIGNATURE_ENTRIES - "manifest.json" == declared.keys, PackageValidationError.STRUCTURE, "压缩包与签名清单不闭合")
            checkPackage(expandedBytes <= availableBytes && limits.reserveBytes <= availableBytes - expandedBytes, PackageValidationError.SPACE, "设备可用空间不足以解压并保留安全余量")
            checkPackage(cutoffInstant != null && cutoffInstant <= generated, PackageValidationError.SCHEMA, "数据截止时间晚于包生成时间")
            if (!activeCutoff.isNullOrBlank()) checkPackage(cutoffInstant!! >= timestamp(activeCutoff), PackageValidationError.DOWNGRADE, "行情包数据截止早于当前包")
            if (!activeGeneratedAt.isNullOrBlank()) checkPackage(generated >= timestamp(activeGeneratedAt), PackageValidationError.DOWNGRADE, "行情包生成时间早于当前包")
            for (expected in declared.values) {
                val entry = archive.getEntry(expected.name) ?: throw PackageCheckException(PackageValidationError.HASH, "签名清单中的数据文件缺失")
                checkPackage(entry.size == expected.bytes, PackageValidationError.HASH, "数据文件长度与签名清单不一致")
                val target = destination?.let { root -> File(root, expected.name).also { file ->
                    checkPackage(file.canonicalPath.startsWith(root.canonicalPath + File.separator), PackageValidationError.STRUCTURE, "抽取路径越界")
                    checkPackage(file.parentFile?.mkdirs() == true || file.parentFile?.isDirectory == true, PackageValidationError.PAYLOAD, "无法创建暂存目录")
                } }
                val digest = archive.getInputStream(entry).use { input ->
                    if (target == null) BoundedPackageIO.copyAndHash(input, maxBytes = expected.bytes)
                    else target.outputStream().use { output -> BoundedPackageIO.copyAndHash(input, output, expected.bytes) }
                }
                checkPackage(digest.bytes == expected.bytes && digest.sha256 == expected.sha256, PackageValidationError.HASH, "数据文件哈希校验未通过")
            }
            destination?.let { File(it, "manifest.json").writeBytes(manifest) }
            PackageValidationResult(VerifiedMarketPackage(packageId, cutoff, generatedAt, expandedBytes))
        }
    } catch (error: PackageCheckException) {
        PackageValidationResult(error = error.error, errorDetail = error.message)
    } catch (_: PackageSizeException) {
        PackageValidationResult(error = PackageValidationError.SPACE, errorDetail = "行情包内容超过大小限额")
    } catch (_: GeneralSecurityException) {
        PackageValidationResult(error = PackageValidationError.SIGNATURE, errorDetail = "签名验证失败或验签算法不可用")
    } catch (_: JSONException) {
        PackageValidationResult(error = PackageValidationError.SCHEMA, errorDetail = "签名清单字段或类型无效")
    } catch (_: JsonProcessingException) {
        PackageValidationResult(error = PackageValidationError.SCHEMA, errorDetail = "签名清单 JSON 无效或包含重复字段")
    } catch (_: ZipException) {
        PackageValidationResult(error = PackageValidationError.STRUCTURE, errorDetail = "行情压缩包损坏")
    } catch (_: Exception) {
        PackageValidationResult(error = PackageValidationError.STRUCTURE, errorDetail = "行情包结构、编码或时间无效")
    }

    private fun parseManifest(bytes: ByteArray): JSONObject {
        val value = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
        // Android JSONObject may accept repeated keys; validate the signed JSON token stream first.
        JsonFactory().createParser(value).use { parser ->
            parser.enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
            checkPackage(parser.nextToken() == JsonToken.START_OBJECT, PackageValidationError.SCHEMA, "清单根节点必须为对象")
            parser.skipChildren()
            checkPackage(parser.nextToken() == null, PackageValidationError.SCHEMA, "清单不能包含尾随内容")
        }
        return JSONObject(value)
    }

    private fun verifySignatures(manifest: ByteArray, archive: ZipFile, names: Set<String>): Boolean {
        fun verify(name: String, pem: String, algorithm: String, signatureAlgorithm: String): Boolean = try {
            val signature = archive.getInputStream(archive.getEntry(name)).use { BoundedPackageIO.readSmall(it, 1024) }
            val encoded = pem.lines().filterNot { it.startsWith("---") }.joinToString("").trim()
            val key = KeyFactory.getInstance(algorithm).generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(encoded)))
            Signature.getInstance(signatureAlgorithm).run { initVerify(key); update(manifest); verify(signature) }
        } catch (_: GeneralSecurityException) { false } catch (_: IllegalArgumentException) { false }
        if ("signature.ed25519" in names && verify("signature.ed25519", publicKeyPem, "Ed25519", "Ed25519")) return true
        return "signature.ecdsa" in names && !ecdsaPublicKeyPem.isNullOrBlank() && verify("signature.ecdsa", ecdsaPublicKeyPem, "EC", "SHA256withECDSA")
    }

    private fun text(json: JSONObject, name: String): String {
        val value = json.opt(name)
        checkPackage(value is String && value.isNotBlank(), PackageValidationError.SCHEMA, "清单文本字段无效：$name")
        return value as String
    }
    private fun integer(json: JSONObject, name: String): Long {
        val value = json.opt(name)
        checkPackage(value is Int || value is Long, PackageValidationError.SCHEMA, "清单整数字段无效：$name")
        return (value as Number).toLong()
    }
    private fun checkKeys(json: JSONObject, allowed: Set<String>) {
        checkPackage(json.keys().asSequence().all { it in allowed }, PackageValidationError.SCHEMA, "清单存在未知字段")
    }
    private fun timestamp(value: String): java.time.Instant = try { OffsetDateTime.parse(value).toInstant() } catch (_: Exception) {
        throw PackageCheckException(PackageValidationError.SCHEMA, "清单时间格式无效")
    }
    private fun checkPackage(condition: Boolean, error: PackageValidationError, detail: String) {
        if (!condition) throw PackageCheckException(error, detail)
    }

    companion object {
        private val CONTENT_ENTRIES = setOf("quality-report.json", "payload.sqlite", "industry/industry-map.html", "industry/industry-atlas.html")
        private val REQUIRED_CONTENT = setOf("quality-report.json", "payload.sqlite")
        private val SIGNATURE_ENTRIES = setOf("signature.ed25519", "signature.ecdsa")
        private val ALLOWED_ENTRIES = CONTENT_ENTRIES + SIGNATURE_ENTRIES + "manifest.json"
        private val MANIFEST_KEYS = setOf("package_id", "schema_version", "generated_at", "minimum_app_version", "package_type", "base_package_id", "partitions", "source_run_summaries")
        private val PARTITION_KEYS = setOf("partition_id", "data_cutoff", "files")
        private val FILE_KEYS = setOf("name", "bytes", "row_count", "sha256")
        private val SUMMARY_KEYS = setOf("run_id", "provider", "status")
        private val SOURCE_STATUSES = setOf("PASS", "FAILED", "PARTIAL_FAILURE", "BLOCKED", "UNSUPPORTED")
        private val SAFE_ID = Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
        private val VERSION = Regex("[0-9]{1,9}\\.[0-9]{1,9}\\.[0-9]{1,9}")
        private val HASH = Regex("[a-f0-9]{64}")
    }
}

class MarketPackageImporter(private val context: Context) {
    fun importPackage(packageFile: File): PackageValidationResult = IMPORT_LOCK.withLock {
        val limits = MarketPackageLimits()
        val root = DatabaseBoundary.coldDirectory(context)
        val staging = File(root, "staging-${UUID.randomUUID()}")
        val inputs = File(context.cacheDir, "market-package-inputs").apply { mkdirs() }
        var immutableInput: File? = null
        try {
            immutableInput = PackageInputSnapshot.freeze(packageFile, inputs, StatFs(context.filesDir.path).availableBytes, limits)
            val preferences = context.getSharedPreferences("market-package", Context.MODE_PRIVATE)
            val imported = preferences.getStringSet("imported", emptySet()).orEmpty().toSet()
            val verifier = MarketPackageVerifier(
                context.resources.openRawResource(com.marketmonitor.app.R.raw.market_package_public_key).bufferedReader().use { it.readText() },
                "0.1.0",
                ecdsaPublicKeyPem = runCatching {
                    context.resources.openRawResource(com.marketmonitor.app.R.raw.market_package_ecdsa_public_key).bufferedReader().use { it.readText() }
                }.getOrNull(),
                limits = limits,
            )
            check(staging.mkdirs()) { "无法创建独立抽取目录" }
            val result = verifier.verifyAndExtract(
                immutableInput, StatFs(context.filesDir.path).availableBytes, imported,
                preferences.getString("active_cutoff", null), preferences.getString("active_generated_at", null),
                preferences.getString("active", null), staging,
            )
            val verified = result.verified ?: return@withLock result
            validatePayload(File(staging, "payload.sqlite"))
            val target = File(File(root, "packages"), verified.packageId)
            check(target.parentFile?.mkdirs() == true || target.parentFile?.isDirectory == true) { "无法创建行情包目录" }
            PackageActivation.publish(staging, target) {
                preferences.edit()
                    .putString("active", verified.packageId)
                    .putString("active_cutoff", verified.dataCutoff)
                    .putString("active_generated_at", verified.generatedAt)
                    .putStringSet("imported", imported + verified.packageId)
                    .commit()
            }
            result
        } catch (error: Exception) {
            PackageValidationResult(
                error = if (error is PackageSizeException) PackageValidationError.SPACE else PackageValidationError.PAYLOAD,
                errorDetail = if (error is PackageSizeException) error.message else "行情包抽取、载荷校验或原子激活失败",
            )
        } finally {
            staging.deleteRecursively()
            immutableInput?.setWritable(true)
            immutableInput?.delete()
        }
    }

    private fun validatePayload(file: File) {
        SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY).use { database ->
            check(database.rawQuery("PRAGMA quick_check", null).use { cursor -> cursor.moveToFirst() && cursor.getString(0) == "ok" }) { "SQLite 完整性校验失败" }
            check(database.rawQuery("PRAGMA foreign_key_check", null).use { !it.moveToFirst() }) { "外键校验失败" }
            val tables = mutableMapOf<String, Set<String>>()
            database.rawQuery(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('instruments', 'bars', 'gold_metrics')",
                null,
            ).use { cursor ->
                while (cursor.moveToNext()) {
                    val name = cursor.getString(0)
                    val columns = mutableSetOf<String>()
                    database.rawQuery("PRAGMA table_info($name)", null).use { definition ->
                        while (definition.moveToNext()) columns += definition.getString(1)
                    }
                    tables[name] = columns
                }
            }
            MarketPayloadSchema.validate(tables)
            check(database.rawQuery("SELECT count(*) FROM bars", null).use { it.moveToFirst() && it.getLong(0) >= 0 }) { "行情数据表 bars 不可用" }
        }
    }

    companion object { private val IMPORT_LOCK = ReentrantLock() }
}

/** Match the columns actually selected by ImportedMarketDataReader before publishing a package. */
internal object MarketPayloadSchema {
    private val required = mapOf(
        "instruments" to setOf("instrument_id", "instrument_json"),
        "bars" to setOf("instrument_id", "period", "bar_open_time", "bar_json"),
    )
    private val optional = mapOf(
        "gold_metrics" to setOf(
            "metric_id", "instrument_id", "trading_date", "period", "metric_name", "value",
            "definition", "calculation_method", "timestamp",
        ),
    )

    fun validate(tables: Map<String, Set<String>>) {
        for ((name, columns) in required) {
            check(name in tables) { "行情包缺少 $name 数据表" }
            check(tables.getValue(name).containsAll(columns)) { "行情包 $name 数据表缺少读取字段" }
        }
        for ((name, columns) in optional) {
            if (name in tables) check(tables.getValue(name).containsAll(columns)) { "行情包 $name 数据表缺少读取字段" }
        }
    }
}

/** A failed preference write has an uncertain durable outcome; the published files must remain readable. */
internal object PackageActivation {
    fun publish(staging: File, target: File, persist: () -> Boolean) {
        if (target.exists()) {
            // commit() may report failure after the directory move. Only resume an exact verified copy.
            check(samePackageContents(staging, target)) { "同名行情包目录与已验签载荷不一致" }
        } else {
            Files.move(staging.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE)
        }
        check(persist()) { "行情包激活记录写入失败" }
    }

    private fun samePackageContents(staging: File, target: File): Boolean {
        val source = packageEntries(staging) ?: return false
        val existing = packageEntries(target) ?: return false
        if (source.keys != existing.keys) return false
        return source.all { (name, sourcePath) ->
            val targetPath = existing.getValue(name)
            if (Files.isDirectory(sourcePath)) Files.isDirectory(targetPath)
            else Files.isRegularFile(targetPath) && Files.size(sourcePath) == Files.size(targetPath) &&
                sourcePath.toFile().inputStream().use { BoundedPackageIO.copyAndHash(it, maxBytes = Files.size(sourcePath)).sha256 } ==
                targetPath.toFile().inputStream().use { BoundedPackageIO.copyAndHash(it, maxBytes = Files.size(targetPath)).sha256 }
        }
    }

    private fun packageEntries(root: File): Map<String, Path>? {
        val rootPath = root.toPath()
        if (!Files.isDirectory(rootPath) || Files.isSymbolicLink(rootPath)) return null
        val entries = linkedMapOf<String, Path>()
        Files.walk(rootPath).use { paths ->
            for (path in paths) {
                if (path == rootPath) continue
                if (entries.size >= 8 || Files.isSymbolicLink(path) ||
                    (!Files.isDirectory(path) && !Files.isRegularFile(path))) return null
                entries[rootPath.relativize(path).toString().replace(File.separatorChar, '/')] = path
            }
        }
        return entries
    }
}

class MarketPackageImportWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val path = inputData.getString("package_path")
            ?: return Result.failure(workDataOf(RESULT_ERROR to PackageValidationError.STRUCTURE.name, RESULT_ERROR_DETAIL to "缺少 package_path"))
        val file = File(path)
        val inputRoot = File(applicationContext.cacheDir, "market-package-inputs")
        if (!file.canonicalPath.startsWith(inputRoot.canonicalPath + File.separator)) {
            return Result.failure(workDataOf(RESULT_ERROR to PackageValidationError.STRUCTURE.name, RESULT_ERROR_DETAIL to "导入路径不属于受控暂存区"))
        }
        val result = try { MarketPackageImporter(applicationContext).importPackage(file) } finally { file.delete() }
        val verified = result.verified
        return if (verified != null) Result.success(workDataOf(RESULT_PACKAGE_ID to verified.packageId, RESULT_DATA_CUTOFF to verified.dataCutoff))
        else Result.failure(workDataOf(RESULT_ERROR to (result.error ?: PackageValidationError.STRUCTURE).name, RESULT_ERROR_DETAIL to (result.errorDetail ?: "未知错误")))
    }

    companion object {
        const val RESULT_PACKAGE_ID = "package_id"
        const val RESULT_DATA_CUTOFF = "data_cutoff"
        const val RESULT_ERROR = "validation_error"
        const val RESULT_ERROR_DETAIL = "validation_error_detail"
    }
}

private fun compareVersions(left: String, right: String): Int {
    val leftParts = left.split('.').map(String::toInt)
    val rightParts = right.split('.').map(String::toInt)
    for (index in leftParts.indices) {
        val comparison = leftParts[index].compareTo(rightParts[index])
        if (comparison != 0) return comparison
    }
    return 0
}
