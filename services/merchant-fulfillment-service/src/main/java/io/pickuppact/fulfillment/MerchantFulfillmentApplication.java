package io.pickuppact.fulfillment;

import java.time.Clock;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class MerchantFulfillmentApplication {
    public static void main(String[] args) {
        SpringApplication.run(MerchantFulfillmentApplication.class, args);
    }

    @Bean
    Clock fulfillmentClock() {
        return Clock.systemUTC();
    }
}
