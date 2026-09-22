package io.pickuppact.fulfillment.api;

import io.pickuppact.fulfillment.application.MerchantFulfillmentService;
import io.pickuppact.fulfillment.domain.MerchantOrder;
import io.pickuppact.fulfillment.infra.MerchantFulfillmentRepository;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/merchant")
public class MerchantFulfillmentController {
    private final MerchantFulfillmentService service;

    public MerchantFulfillmentController(MerchantFulfillmentService service) {
        this.service = service;
    }

    @GetMapping("/orders/{orderId}")
    public MerchantOrder order(@PathVariable UUID orderId) {
        return service.get(orderId);
    }

    @GetMapping("/stores/{storeId}/deliveries")
    public List<MerchantFulfillmentRepository.Delivery> deliveries(
            @PathVariable String storeId,
            @RequestParam(defaultValue = "0") long afterSequence,
            @RequestParam(defaultValue = "50") int limit
    ) {
        return service.pendingDeliveries(storeId, afterSequence, limit);
    }

    @PostMapping("/deliveries/{sequence}/ack")
    public Map<String, Object> acknowledge(@PathVariable long sequence) {
        service.acknowledge(sequence);
        return Map.of("acknowledged", true, "deliverySequence", sequence);
    }

    @PostMapping("/orders/{orderId}/accept")
    public MerchantOrder accept(@PathVariable UUID orderId) {
        return service.accept(orderId);
    }

    @PostMapping("/orders/{orderId}/start")
    public MerchantOrder start(@PathVariable UUID orderId) {
        return service.start(orderId);
    }

    @PostMapping("/orders/{orderId}/ready")
    public MerchantOrder ready(@PathVariable UUID orderId) {
        return service.ready(orderId);
    }

    @PostMapping("/orders/{orderId}/pickup")
    public MerchantOrder pickup(@PathVariable UUID orderId) {
        return service.pickup(orderId);
    }

    @GetMapping("/orders/{orderId}/effects")
    public List<MerchantFulfillmentRepository.Effect> effects(@PathVariable UUID orderId) {
        return service.effects(orderId);
    }

    @GetMapping("/orders/{orderId}/anomalies")
    public List<MerchantFulfillmentRepository.Anomaly> anomalies(@PathVariable UUID orderId) {
        return service.anomalies(orderId);
    }
}
