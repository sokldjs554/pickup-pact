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

data class OrderWorkload(
    val units: Int,
    val totalAmount: Int
)

object MenuWorkloadPolicy {
    private data class MenuSpec(val units: Int, val price: Int)

    private val specBySku = mapOf(
        "americano" to MenuSpec(1, 4500),
        "cafe-latte" to MenuSpec(2, 5000),
        "vanilla-latte" to MenuSpec(2, 5500),
        "cold-brew" to MenuSpec(1, 5200),
        "morning-americano" to MenuSpec(1, 4300),
        "flat-white" to MenuSpec(2, 5300),
        "ham-sandwich" to MenuSpec(3, 6800),
        "on-americano" to MenuSpec(1, 4200),
        "peach-iced-tea" to MenuSpec(1, 4000),
        "matcha-latte" to MenuSpec(2, 5600)
    )

    fun evaluate(items: List<PickupItem>): OrderWorkload {
        require(items.isNotEmpty()) { "at least one item is required" }
        var units = 0
        var total = 0
        for (item in items) {
            val spec = specBySku[item.sku]
                ?: throw IllegalArgumentException("unknown menu sku: ${item.sku}")
            units += spec.units * item.quantity
            total += spec.price * item.quantity
        }
        return OrderWorkload(units = units, totalAmount = total)
    }

    fun units(items: List<PickupItem>): Int = evaluate(items).units
}
