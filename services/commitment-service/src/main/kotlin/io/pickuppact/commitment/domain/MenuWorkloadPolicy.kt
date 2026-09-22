package io.pickuppact.commitment.domain

data class PickupItem(
    val sku: String,
    val quantity: Int
) {
    init {
        require(sku.isNotBlank()) { "sku must not be blank" }
        require(quantity in 1..20) { "quantity must be between 1 and 20" }
    }
}

object MenuWorkloadPolicy {
    private val unitsBySku = mapOf(
        "americano" to 1,
        "cafe-latte" to 2,
        "vanilla-latte" to 2,
        "cold-brew" to 1,
        "morning-americano" to 1,
        "flat-white" to 2,
        "ham-sandwich" to 3,
        "on-americano" to 1,
        "peach-iced-tea" to 1,
        "matcha-latte" to 2
    )

    fun units(items: List<PickupItem>): Int {
        require(items.isNotEmpty()) { "at least one item is required" }
        return items.sumOf { item ->
            val perItem = unitsBySku[item.sku]
                ?: throw IllegalArgumentException("unknown menu sku: ${item.sku}")
            perItem * item.quantity
        }
    }
}
