package io.pickuppact.commitment.api

import org.springframework.http.HttpStatus
import org.springframework.web.bind.annotation.ExceptionHandler
import org.springframework.web.bind.annotation.ResponseStatus
import org.springframework.web.bind.annotation.RestControllerAdvice

@RestControllerAdvice
class CommitmentExceptionHandler {
    @ExceptionHandler(NoSuchElementException::class)
    @ResponseStatus(HttpStatus.NOT_FOUND)
    fun notFound(exception: NoSuchElementException): Map<String, String> = mapOf(
        "error" to "commitment_not_found",
        "message" to (exception.message ?: "commitment not found"),
    )

    @ExceptionHandler(IllegalArgumentException::class)
    @ResponseStatus(HttpStatus.BAD_REQUEST)
    fun badRequest(exception: IllegalArgumentException): Map<String, String> = mapOf(
        "error" to "invalid_commitment_request",
        "message" to (exception.message ?: "invalid commitment request"),
    )

    @ExceptionHandler(IllegalStateException::class)
    @ResponseStatus(HttpStatus.CONFLICT)
    fun conflict(exception: IllegalStateException): Map<String, String> = mapOf(
        "error" to "commitment_state_conflict",
        "message" to (exception.message ?: "commitment state conflict"),
    )
}
