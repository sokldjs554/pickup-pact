package io.pickuppact.commitment.application

import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component

@Component
class StoreCapacityPolicy(
    @Value("\${pickup.capacity.default-units:40}")
    private val defaultCapacity: Int,
    @Value("\${pickup.capacity.store-units:}")
    rawOverrides: String
) {
    private val overrides: Map<String, Int> = rawOverrides
        .split(",")
        .map { it.trim() }
        .filter { it.isNotEmpty() }
        .associate { entry ->
            val parts = entry.split("=", limit = 2)
            require(parts.size == 2 && parts[0].isNotBlank()) {
                "invalid pickup.capacity.store-units entry: $entry"
            }
            val value = parts[1].toInt()
            require(value > 0) { "store capacity must be positive: $entry" }
            parts[0] to value
        }

    init {
        require(defaultCapacity > 0) { "default pickup capacity must be positive" }
    }

    fun capacityFor(storeId: String): Int =
        overrides[storeId] ?: defaultCapacity
}
