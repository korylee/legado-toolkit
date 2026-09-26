package io.legado.app.service

import org.junit.Test

class RuntimeSnapshotTest {
    @Test
    fun capture_refresh_runtime() {
        ServiceJson.writeRuntimeSnapshot("refresh")
    }
}
