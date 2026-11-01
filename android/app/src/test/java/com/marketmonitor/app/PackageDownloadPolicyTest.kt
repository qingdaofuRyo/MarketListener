package com.marketmonitor.app

import com.marketmonitor.app.data.PackageDownloadPolicy
import com.marketmonitor.app.data.BoundedPackageIO
import com.marketmonitor.app.data.MarketPackageLimits
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class PackageDownloadPolicyTest {
    @Test
    fun releaseRequiresHttpsAndDebugOnlyAllowsTheLocalPort() {
        assertThrows(IllegalArgumentException::class.java) {
            PackageDownloadPolicy.packageUrl("http://127.0.0.1:8765", false)
        }
        assertThrows(IllegalArgumentException::class.java) {
            PackageDownloadPolicy.packageUrl("http://example.com:8765", true)
        }
        assertEquals("http://10.0.2.2:8765/api/android-package",
            PackageDownloadPolicy.packageUrl("http://10.0.2.2:8765", true).toString())
        assertEquals("https://example.com/api/android-package",
            PackageDownloadPolicy.packageUrl("https://example.com", false).toString())
    }

    @Test
    fun redirectsOrOversizeDownloadsAreRejectedBeforeBodyRead() {
        assertThrows(IllegalArgumentException::class.java) {
            PackageDownloadPolicy.validateResponse(302, "application/zip", 100, 1_000)
        }
        assertThrows(IllegalArgumentException::class.java) {
            PackageDownloadPolicy.validateResponse(200, "text/html", 100, 1_000)
        }
        assertThrows(IllegalArgumentException::class.java) {
            PackageDownloadPolicy.validateResponse(200, "application/zip", 1_001, 1_000)
        }
    }

    @Test
    fun archiveCopyBudgetKeepsSpaceForTheVerificationSnapshot() {
        val limits = MarketPackageLimits(maxArchiveBytes = 100L, reserveBytes = 10L)
        assertEquals(100L, BoundedPackageIO.archiveCopyBudget(210L, limits))
        assertEquals(50L, BoundedPackageIO.archiveCopyBudget(110L, limits))
        assertEquals(0L, BoundedPackageIO.archiveCopyBudget(10L, limits))
    }
}
