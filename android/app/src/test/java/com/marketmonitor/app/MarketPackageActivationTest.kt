package com.marketmonitor.app

import com.marketmonitor.app.data.PackageActivation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class MarketPackageActivationTest {
    @Test
    fun failedPreferenceCommitRetainsNewPayloadAndPreviousActiveDirectory() {
        val root = Files.createTempDirectory("market-package-activation-").toFile()
        try {
            val oldActive = root.resolve("old-package").apply { mkdirs() }
            oldActive.resolve("payload.sqlite").writeText("old")
            val staging = root.resolve("staging").apply { mkdirs() }
            staging.resolve("payload.sqlite").writeText("new")
            val published = root.resolve("new-package")

            assertThrows(IllegalStateException::class.java) {
                PackageActivation.publish(staging, published) { false }
            }
            assertFalse(staging.exists())
            assertEquals("new", published.resolve("payload.sqlite").readText())
            assertEquals("old", oldActive.resolve("payload.sqlite").readText())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun successfulCommitPublishesOneCompleteDirectory() {
        val root = Files.createTempDirectory("market-package-activation-").toFile()
        try {
            val staging = root.resolve("staging").apply { mkdirs() }
            staging.resolve("payload.sqlite").writeText("valid")
            val published = root.resolve("package")

            PackageActivation.publish(staging, published) { true }

            assertTrue(published.isDirectory)
            assertFalse(staging.exists())
            assertEquals("valid", published.resolve("payload.sqlite").readText())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun retryAfterFailedCommitAcceptsOnlyIdenticalPublishedPackage() {
        val root = Files.createTempDirectory("market-package-retry-").toFile()
        try {
            val oldActive = root.resolve("old-package").apply { mkdirs() }
            oldActive.resolve("payload.sqlite").writeText("old")
            val published = root.resolve("new-package")
            val firstStaging = root.resolve("staging-first").apply { mkdirs() }
            firstStaging.resolve("payload.sqlite").writeText("new")
            firstStaging.resolve("manifest.json").writeText("signed-manifest")

            assertThrows(IllegalStateException::class.java) {
                PackageActivation.publish(firstStaging, published) { false }
            }
            val retryStaging = root.resolve("staging-retry").apply { mkdirs() }
            retryStaging.resolve("payload.sqlite").writeText("new")
            retryStaging.resolve("manifest.json").writeText("signed-manifest")
            var persisted = false
            PackageActivation.publish(retryStaging, published) { persisted = true; true }

            assertTrue(persisted)
            assertEquals("new", published.resolve("payload.sqlite").readText())
            assertEquals("old", oldActive.resolve("payload.sqlite").readText())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun retryDoesNotOverwriteDifferentOrExtraExistingContent() {
        val root = Files.createTempDirectory("market-package-retry-").toFile()
        try {
            val published = root.resolve("package").apply { mkdirs() }
            published.resolve("payload.sqlite").writeText("older")
            val staging = root.resolve("staging").apply { mkdirs() }
            staging.resolve("payload.sqlite").writeText("new")
            assertThrows(IllegalStateException::class.java) {
                PackageActivation.publish(staging, published) { true }
            }
            assertEquals("older", published.resolve("payload.sqlite").readText())

            published.resolve("payload.sqlite").writeText("new")
            published.resolve("unexpected.txt").writeText("extra")
            assertThrows(IllegalStateException::class.java) {
                PackageActivation.publish(staging, published) { true }
            }
            assertTrue(staging.isDirectory)
        } finally {
            root.deleteRecursively()
        }
    }
}
