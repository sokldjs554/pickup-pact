package io.pickuppact.commitment.infra

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
    private val acquireScript = DefaultRedisScript(
        """
        local current = tonumber(redis.call('GET', KEYS[1]) or '0')
        local requested = tonumber(ARGV[1])
        local capacity = tonumber(ARGV[2])
        if current + requested > capacity then return -1 end
        local updated = redis.call('INCRBY', KEYS[1], requested)
        redis.call('PEXPIRE', KEYS[1], ARGV[3])
        return updated
        """.trimIndent(),
        Long::class.java
    )

    private val releaseScript = DefaultRedisScript(
        """
        local current = tonumber(redis.call('GET', KEYS[1]) or '0')
        local units = tonumber(ARGV[1])
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

    override fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String> {
        val slot = pickupAt.epochSecond / 300
        val key = "pickup:capacity:" + storeId + ":" + slot
        return redis.execute(
            acquireScript,
            listOf(key),
            units.toString(),
            defaultCapacity.toString(),
            (ttlSeconds * 1000).toString()
        ).single()
            .flatMap { updated ->
                if (updated < 0) Mono.error(IllegalStateException("pickup slot capacity exceeded"))
                else Mono.just(key + "|" + units + "|" + UUID.randomUUID())
            }
    }

    override fun release(token: String): Mono<Void> {
        val parts = token.split("|")
        if (parts.size != 3) return Mono.error(IllegalArgumentException("invalid lease token"))
        return redis.execute(releaseScript, listOf(parts[0]), parts[1]).then()
    }
}
