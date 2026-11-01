package com.marketmonitor.app

import com.marketmonitor.app.data.MarketPackageVerifier
import com.marketmonitor.app.data.PackageValidationError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.io.File
import java.nio.charset.StandardCharsets
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.Signature
import java.util.Base64
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

class MarketPackageVerifierTest {
    private val keys: KeyPair = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
    private val pem = "-----BEGIN PUBLIC KEY-----\n${Base64.getEncoder().encodeToString(keys.public.encoded)}\n-----END PUBLIC KEY-----\n"
    private val payload = "synthetic-payload".toByteArray(StandardCharsets.UTF_8)
    private val report = "{}".toByteArray(StandardCharsets.UTF_8)

    private fun sha(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }

    private fun manifest(schema: Int = 1, payloadBytes: ByteArray = payload): ByteArray = """{
      "package_id":"synthetic-package-1","schema_version":$schema,
      "generated_at":"2026-10-10T10:00:00+00:00","minimum_app_version":"0.1.0","package_type":"FULL",
      "source_run_summaries":[],"partitions":[{"partition_id":"synthetic-partition",
      "data_cutoff":"2026-10-09T15:00:00+08:00","files":[
        {"name":"payload.sqlite","bytes":${payloadBytes.size},"row_count":0,"sha256":"${sha(payloadBytes)}"},
        {"name":"quality-report.json","bytes":${report.size},"row_count":0,"sha256":"${sha(report)}"}
      ]}]}
    """.toByteArray(StandardCharsets.UTF_8)

    private fun archive(manifest: ByteArray, payloadBytes: ByteArray = payload,
                        extra: Pair<String, ByteArray>? = null, ed25519Signature: ByteArray? = null): File {
        val file = File.createTempFile("synthetic-market-package", ".zip")
        file.deleteOnExit()
        val signature = Signature.getInstance("Ed25519").run {
            initSign(keys.private); update(manifest); sign()
        }
        ZipOutputStream(file.outputStream()).use { output ->
            listOf("manifest.json" to manifest, "signature.ed25519" to (ed25519Signature ?: signature),
                   "payload.sqlite" to payloadBytes, "quality-report.json" to report).plus(
                listOfNotNull(extra)
            ).forEach { (name, bytes) ->
                output.putNextEntry(ZipEntry(name)); output.write(bytes); output.closeEntry()
            }
        }
        return file
    }

    @Test
    fun acceptsCompleteSignedPackageAndRejectsReplayOrOldSchema() {
        val verifier = MarketPackageVerifier(pem, "0.1.0")
        val file = archive(manifest())
        val accepted = verifier.verify(file, Long.MAX_VALUE, emptySet())
        assertNotNull("valid package error=${accepted.error}: ${accepted.errorDetail}", accepted.verified)
        assertEquals(PackageValidationError.DUPLICATE,
            verifier.verify(file, Long.MAX_VALUE, setOf("synthetic-package-1")).error)
        assertEquals(PackageValidationError.SCHEMA,
            verifier.verify(archive(manifest(schema = 0)), Long.MAX_VALUE, emptySet()).error)
    }

    @Test
    fun rejectsUndeclaredOrChangedContentEvenWithAValidManifestSignature() {
        val verifier = MarketPackageVerifier(pem, "0.1.0")
        val signed = manifest()
        assertEquals(PackageValidationError.STRUCTURE,
            verifier.verify(archive(signed, extra = "industry/industry-atlas.html" to "unlisted".toByteArray()),
                Long.MAX_VALUE, emptySet()).error)
        assertEquals(PackageValidationError.HASH,
            verifier.verify(archive(signed, payloadBytes = "changed".toByteArray()),
                Long.MAX_VALUE, emptySet()).error)
    }

    @Test
    fun rejectsTinyZipThatExpandsFarBeyondItsCompressedSize() {
        val expanded = ByteArray(2 * 1024 * 1024)
        val result = MarketPackageVerifier(pem, "0.1.0")
            .verify(archive(manifest(payloadBytes = expanded), payloadBytes = expanded), Long.MAX_VALUE, emptySet())
        assertEquals(PackageValidationError.SPACE, result.error)
    }

    @Test
    fun rejectsDuplicateJsonKeysAndAValidatedDeltaWithoutAnOverlayReader() {
        val verifier = MarketPackageVerifier(pem, "0.1.0")
        val duplicateKey = String(manifest(), StandardCharsets.UTF_8)
            .replace("\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1")
            .toByteArray(StandardCharsets.UTF_8)
        assertEquals(PackageValidationError.SCHEMA,
            verifier.verify(archive(duplicateKey), Long.MAX_VALUE, emptySet()).error)

        val delta = String(manifest(), StandardCharsets.UTF_8)
            .replace("\"package_type\":\"FULL\"", "\"package_type\":\"DELTA\",\"base_package_id\":\"base-1\"")
            .toByteArray(StandardCharsets.UTF_8)
        assertEquals(PackageValidationError.UNSUPPORTED_DELTA,
            verifier.verify(archive(delta), Long.MAX_VALUE, setOf("base-1"), activePackageId = "base-1").error)
    }

    @Test
    fun rejectsOlderCutoffOrPackageGenerationTime() {
        val verifier = MarketPackageVerifier(pem, "0.1.0")
        val file = archive(manifest())
        assertEquals(PackageValidationError.DOWNGRADE,
            verifier.verify(file, Long.MAX_VALUE, emptySet(), activeCutoff = "2026-10-10T15:00:00+08:00").error)
        assertEquals(PackageValidationError.DOWNGRADE,
            verifier.verify(file, Long.MAX_VALUE, emptySet(), activeGeneratedAt = "2026-10-11T10:00:00+00:00").error)
    }

    @Test
    fun acceptsPinnedEcdsaSignatureWhenEd25519SignatureCannotVerify() {
        val ecdsaKeys = KeyPairGenerator.getInstance("EC").apply { initialize(256) }.generateKeyPair()
        val ecdsaPem = "-----BEGIN PUBLIC KEY-----\n${Base64.getEncoder().encodeToString(ecdsaKeys.public.encoded)}\n-----END PUBLIC KEY-----\n"
        val signed = manifest()
        val ecdsaSignature = Signature.getInstance("SHA256withECDSA").run {
            initSign(ecdsaKeys.private); update(signed); sign()
        }
        val file = archive(signed, extra = "signature.ecdsa" to ecdsaSignature, ed25519Signature = ByteArray(64))

        assertNotNull(MarketPackageVerifier(pem, "0.1.0", ecdsaPublicKeyPem = ecdsaPem)
            .verify(file, Long.MAX_VALUE, emptySet()).verified)
        assertEquals(PackageValidationError.SIGNATURE,
            MarketPackageVerifier(pem, "0.1.0").verify(file, Long.MAX_VALUE, emptySet()).error)
    }
}
