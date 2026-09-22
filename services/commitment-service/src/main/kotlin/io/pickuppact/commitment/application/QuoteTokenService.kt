package io.pickuppact.commitment.application

import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Instant
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

data class QuoteClaims(
    val storeId: String,
    val units: Int,
    val expiresAt: Instant
)

@Component
class QuoteTokenService(
    @Value("\${pickup.quote-secret:local-dev-quote-secret-change-me}")
    private val secret: String
) {
    fun issue(storeId: String, units: Int, expiresAt: Instant): String {
        require(storeId.isNotBlank()) { "storeId must not be blank" }
        require(units > 0) { "units must be positive" }
        require(expiresAt.isAfter(Instant.now())) { "quote must expire in the future" }

        val payload = listOf(storeId, units.toString(), expiresAt.epochSecond.toString()).joinToString("|")
        val encoded = Base64.getUrlEncoder().withoutPadding()
            .encodeToString(payload.toByteArray(StandardCharsets.UTF_8))
        return "$encoded.${signature(encoded)}"
    }

    fun verify(token: String, now: Instant = Instant.now()): QuoteClaims {
        val parts = token.split(".")
        require(parts.size == 2) { "invalid quote token" }
        val expected = signature(parts[0])
        require(
            MessageDigest.isEqual(
                expected.toByteArray(StandardCharsets.UTF_8),
                parts[1].toByteArray(StandardCharsets.UTF_8)
            )
        ) { "invalid quote signature" }

        val payload = String(
            Base64.getUrlDecoder().decode(parts[0]),
            StandardCharsets.UTF_8
        )
        val values = payload.split("|")
        require(values.size == 3) { "invalid quote payload" }
        val claims = QuoteClaims(
            storeId = values[0],
            units = values[1].toInt(),
            expiresAt = Instant.ofEpochSecond(values[2].toLong())
        )
        require(claims.expiresAt.isAfter(now)) { "quote has expired" }
        return claims
    }

    private fun signature(encodedPayload: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(StandardCharsets.UTF_8), "HmacSHA256"))
        return Base64.getUrlEncoder().withoutPadding()
            .encodeToString(mac.doFinal(encodedPayload.toByteArray(StandardCharsets.UTF_8)))
    }
}
