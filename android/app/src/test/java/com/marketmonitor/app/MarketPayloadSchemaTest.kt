package com.marketmonitor.app

import com.marketmonitor.app.data.MarketPayloadSchema
import org.junit.Assert.assertThrows
import org.junit.Test
import java.nio.file.Files
import java.sql.Connection
import java.sql.DriverManager

class MarketPayloadSchemaTest {
    private fun inspect(vararg statements: String): Map<String, Set<String>> {
        val file = Files.createTempFile("synthetic-market-payload-", ".sqlite")
        try {
            DriverManager.getConnection("jdbc:sqlite:${file.toAbsolutePath()}").use { connection ->
                statements.forEach { connection.createStatement().use { statement -> statement.execute(it) } }
                connection.createStatement().use { statement ->
                    statement.executeQuery("PRAGMA quick_check").use { result ->
                        check(result.next() && result.getString(1) == "ok")
                    }
                }
                return payloadTables(connection)
            }
        } finally {
            Files.deleteIfExists(file)
        }
    }

    private fun payloadTables(connection: Connection): Map<String, Set<String>> {
        val tables = mutableMapOf<String, Set<String>>()
        connection.createStatement().use { statement ->
            statement.executeQuery(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('instruments', 'bars', 'gold_metrics')",
            ).use { names ->
                while (names.next()) {
                    val name = names.getString(1)
                    connection.createStatement().use { definition ->
                        definition.executeQuery("PRAGMA table_info($name)").use { columns ->
                            val found = mutableSetOf<String>()
                            while (columns.next()) found += columns.getString(2)
                            tables[name] = found
                        }
                    }
                }
            }
        }
        return tables
    }

    @Test
    fun acceptsProducerSchemaAndLegacyPayloadWithoutMetrics() {
        val instruments = "CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY, instrument_json TEXT NOT NULL)"
        val bars = "CREATE TABLE bars (instrument_id TEXT NOT NULL, period TEXT NOT NULL, " +
            "bar_open_time TEXT NOT NULL, bar_json TEXT NOT NULL)"
        val metrics = "CREATE TABLE gold_metrics (metric_id TEXT, instrument_id TEXT, trading_date TEXT, " +
            "period TEXT, metric_name TEXT, value DOUBLE, definition TEXT, calculation_method TEXT, timestamp TEXT)"

        MarketPayloadSchema.validate(inspect(instruments, bars, metrics))
        MarketPayloadSchema.validate(inspect(instruments, bars))
    }

    @Test
    fun rejectsStructurallyValidSqliteMissingReaderTablesOrColumns() {
        val bars = "CREATE TABLE bars (instrument_id TEXT, period TEXT, bar_open_time TEXT, bar_json TEXT)"
        val instruments = "CREATE TABLE instruments (instrument_id TEXT, instrument_json TEXT)"
        assertThrows(IllegalStateException::class.java) {
            MarketPayloadSchema.validate(inspect(bars))
        }
        assertThrows(IllegalStateException::class.java) {
            MarketPayloadSchema.validate(inspect(instruments, "CREATE TABLE bars (bar_json TEXT)"))
        }
        assertThrows(IllegalStateException::class.java) {
            MarketPayloadSchema.validate(inspect(instruments, bars, "CREATE TABLE gold_metrics (metric_id TEXT)"))
        }
    }

    @Test
    fun aViewCannotImpersonateTheRequiredInstrumentsTable() {
        val tables = inspect(
            "CREATE TABLE bars (instrument_id TEXT, period TEXT, bar_open_time TEXT, bar_json TEXT)",
            "CREATE VIEW instruments AS SELECT instrument_id, bar_json AS instrument_json FROM bars",
        )
        assertThrows(IllegalStateException::class.java) { MarketPayloadSchema.validate(tables) }
    }
}
