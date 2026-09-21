package io.pickuppact.commitment

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.scheduling.annotation.EnableScheduling

@EnableScheduling
@SpringBootApplication
class CommitmentApplication

fun main(args: Array<String>) {
    runApplication<CommitmentApplication>(*args)
}
