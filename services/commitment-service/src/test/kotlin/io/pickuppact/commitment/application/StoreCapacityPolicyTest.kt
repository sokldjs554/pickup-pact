package io.pickuppact.commitment.application

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class StoreCapacityPolicyTest {
    @Test
    fun storeOverrideWinsAndUnknownStoreUsesDefault() {
        val policy = StoreCapacityPolicy(
            defaultCapacity = 40,
            rawOverrides = "gangnam=48,seolleung=36"
        )

        assertEquals(48, policy.capacityFor("gangnam"))
        assertEquals(36, policy.capacityFor("seolleung"))
        assertEquals(40, policy.capacityFor("other"))
    }

    @Test
    fun malformedOverrideIsRejected() {
        assertThrows(IllegalArgumentException::class.java) {
            StoreCapacityPolicy(defaultCapacity = 40, rawOverrides = "broken")
        }
    }
}
