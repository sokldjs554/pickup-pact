import io.pickuppact.commitment.domain.CapacityLease
import io.pickuppact.commitment.domain.CommitmentStatus
import io.pickuppact.commitment.domain.PaymentAuthorization
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupSlot
import java.time.Instant

fun main() {
    val now = Instant.parse("2026-09-18T03:00:00Z")
    val draft = PickupCommitment(
        id = "order-smoke",
        slot = PickupSlot("store-1", now.plusSeconds(1800), 2),
    )

    check(runCatching { draft.confirm(now) }.isFailure)

    val held = draft.hold(CapacityLease("lease-1", now.plusSeconds(90)))
    val paid = held.authorizePayment(PaymentAuthorization("auth-1", now.plusSeconds(60)))
    val confirmed = paid.confirm(now)
    check(confirmed.status == CommitmentStatus.CONFIRMED)
    check(confirmed.promiseRevision == 1L)

    val expiredPayment = held.authorizePayment(PaymentAuthorization("auth-expired", now.minusSeconds(1)))
    check(runCatching { expiredPayment.confirm(now) }.isFailure)

    val cancelled = confirmed.cancel()
    check(cancelled.status == CommitmentStatus.CANCELLED)
    check(runCatching { cancelled.cancel() }.isFailure)
}
