package io.pickuppact.commitment.infra

import io.pickuppact.commitment.application.CapacityAvailability
import io.pickuppact.commitment.application.CapacityLeasePort
import org.springframework.beans.factory.annotation.Value
import org.springframework.context.annotation.Profile
import org.springframework.data.redis.core.ReactiveStringRedisTemplate
import org.springframework.data.redis.core.script.DefaultRedisScript
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

@Component
@Profile("redis")
class ReactiveRedisCapacityLease(
    private val redis: ReactiveStringRedisTemplate,
    @Value("\${pickup.capacity.default-units:40}") private val defaultCapacity: Long
) : CapacityLeasePort {
    private val acquireScript = DefaultRedisScript<Long>(
        """
        local current = tonumber(redis.call('GET', KEYS[1]) or '0')
        local requested = tonumber(ARGV[1])
        local capacity = tonumber(ARGV[2])
        local requested_ttl = tonumber(ARGV[3])

        if current + requested > capacity then
          return -1
        end

        local updated = redis.call('INCRBY', KEYS[1], requested)
        local current_ttl = redis.call('PTTL', KEYS[1])
        if current_ttl < requested_ttl then
          redis.call('PEXPIRE', KEYS[1], requested_ttl)
        end

        redis.call('PSETEX', KEYS[2], requested_ttl, requested)
        return updated
        """.trimIndent(),
        Long::class.java
    )

    private val releaseScript = DefaultRedisScript<Long>(
        """
        if redis.call('EXISTS', KEYS[2]) == 0 then
          return tonumber(redis.call('GET', KEYS[1]) or '0')
        end

        local units = tonumber(redis.call('GET', KEYS[2]) or '0')
        redis.call('DEL', KEYS[2])

        local current = tonumber(redis.call('GET', KEYS[1]) or '0')
        local updated = current - units
        if updated <= 0 then
          redis.call('DEL', KEYS[1])
          return 0
        end

        redis.call('SET', KEYS[1], updated, 'KEEPTTL')
        return updated
        """.trimIndent(),
        Long::class.java
    )

    private fun slotKey(storeId: String, pickupAt: Instant): String {
        val slot = pickupAt.epochSecond / SLOT_SECONDS
        return "pickup:capacity:$storeId:$slot"
    }

    override fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String> {
        require(units > 0) { "units must be positive" }
        val capacityKey = slotKey(storeId, pickupAt)
        val leaseKey = "pickup:lease:${UUID.randomUUID()}"
        val args = listOf(
            units.toString(),
            defaultCapacity.toString(),
            (ttlSeconds * 1000).toString()
        )

        return redis.execute(acquireScript, listOf(capacityKey, leaseKey), args)
            .single()
            .flatMap { updated ->
                if (updated < 0) Mono.error(IllegalStateException("pickup slot capacity exceeded"))
                else Mono.just("$capacityKey|$leaseKey")
            }
    }

    override fun release(token: String): Mono<Void> {
        val parts = token.split("|")
        if (parts.size != 2) return Mono.error(IllegalArgumentException("invalid lease token"))
        return redis.execute(releaseScript, listOf(parts[0], parts[1]), emptyList()).then()
    }

    override fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability> =
        redis.opsForValue()
            .get(slotKey(storeId, pickupAt))
            .defaultIfEmpty("0")
            .map { value ->
                CapacityAvailability(
                    capacityUnits = defaultCapacity.toInt(),
                    reservedUnits = value.toIntOrNull()?.coerceAtLeast(0) ?: 0
                )
            }

    private companion object {
        const val SLOT_SECONDS = 300L
    }
}
