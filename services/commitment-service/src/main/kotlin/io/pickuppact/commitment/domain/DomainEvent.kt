package io.pickuppact.commitment.domain

import java.time.Instant
import java.util.UUID

data class DomainEvent(
    val eventId: String = UUID.randomUUID().toString(),
    val aggregateId: String,
    val eventType: String,
    val occurredAt: Instant,
    val correlationId: String,
    val causationId: String? = null,
    val schemaVersion: Int = 1,
    val payload: Map<String, Any?> = emptyMap(),
)
