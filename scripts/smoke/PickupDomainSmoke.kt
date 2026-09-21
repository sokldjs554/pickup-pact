import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import java.time.Instant
import java.util.UUID

fun main() {
    val pickupAt = Instant.parse("2026-09-18T03:30:00Z")
    val held = PickupCommitment(
        id = UUID.fromString("00000000-0000-0000-0000-000000000001"),
        storeId = "store-1",
        pickupAt = pickupAt,
        units = 2,
        leaseToken = "lease-1",
        paymentAuthorized = true,
        state = CommitmentState.HELD
    )

    val confirmed = held.confirm(Instant.parse("2026-09-18T03:00:00Z"))
    check(confirmed.state == CommitmentState.CONFIRMED)
    check(confirmed.version == 1L)

    val unpaid = held.copy(paymentAuthorized = false)
    check(runCatching { unpaid.confirm(Instant.parse("2026-09-18T03:00:00Z")) }.isFailure)

    val cancelled = confirmed.cancel()
    check(cancelled.state == CommitmentState.CANCELLED)
    check(runCatching { cancelled.cancel() }.isFailure)
}
