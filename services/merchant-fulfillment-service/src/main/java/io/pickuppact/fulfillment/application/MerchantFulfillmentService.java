package io.pickuppact.fulfillment.application;

import com.fasterxml.jackson.databind.JsonNode;
import io.pickuppact.fulfillment.domain.FulfillmentAnomalyCode;
import io.pickuppact.fulfillment.domain.FulfillmentState;
import io.pickuppact.fulfillment.domain.MerchantOrder;
import io.pickuppact.fulfillment.infra.MerchantFulfillmentRepository;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class MerchantFulfillmentService {
    public enum IntakeResult { APPLIED, DUPLICATE_NOOP, IGNORED }

    private final MerchantFulfillmentRepository repository;
    private final Clock clock;

    public MerchantFulfillmentService(MerchantFulfillmentRepository repository, Clock clock) {
        this.repository = repository;
        this.clock = clock;
    }

    @Transactional
    public IntakeResult handleCommitmentEvent(
            String eventId,
            UUID orderId,
            String eventType,
            Instant occurredAt,
            JsonNode payload
    ) {
        if (!isRelevant(eventType)) return IntakeResult.IGNORED;
        if (!repository.recordInbox(eventId, orderId, eventType, payload)) {
            return IntakeResult.DUPLICATE_NOOP;
        }

        return switch (eventType) {
            case "CommitmentConfirmed" -> {
                receiveConfirmed(orderId, occurredAt, payload);
                yield IntakeResult.APPLIED;
            }
            case "CommitmentCancelled" -> {
                applyCancellation(orderId, occurredAt);
                yield IntakeResult.APPLIED;
            }
            case "PickupRescheduled" -> {
                applyReschedule(orderId, occurredAt, payload);
                yield IntakeResult.APPLIED;
            }
            default -> IntakeResult.IGNORED;
        };
    }

    @Transactional
    public MerchantOrder accept(UUID orderId) {
        MerchantOrder current = required(orderId);
        if (current.state() == FulfillmentState.ACCEPTED) return current;
        Instant now = clock.instant();
        MerchantOrder updated = current.accept(now);
        repository.update(updated);
        repository.appendOutbox(
                orderId,
                "MerchantOrderAccepted",
                Map.of("store_id", updated.storeId(), "accepted_at", now.toString()),
                now
        );
        return updated;
    }

    @Transactional
    public MerchantOrder start(UUID orderId) {
        MerchantOrder current = required(orderId);
        Instant now = clock.instant();
        var transition = current.start(now);
        repository.update(transition.order());
        repository.appendOutbox(
                orderId,
                "PreparationStarted",
                Map.of(
                        "store_id", transition.order().storeId(),
                        "started_at", now.toString(),
                        "pickup_at", transition.order().pickupAt().toString()
                ),
                now
        );
        if (transition.anomaly() != null) {
            recordAnomaly(
                    transition.order(),
                    transition.anomaly(),
                    "preparation started after target ready time",
                    now
            );
        }
        return transition.order();
    }

    @Transactional
    public MerchantOrder ready(UUID orderId) {
        MerchantOrder current = required(orderId);
        Instant now = clock.instant();
        var transition = current.ready(now);
        repository.update(transition.order());
        repository.appendOutbox(
                orderId,
                "OrderReady",
                Map.of(
                        "store_id", transition.order().storeId(),
                        "ready_at", now.toString(),
                        "pickup_at", transition.order().pickupAt().toString(),
                        "quality", transition.quality().name()
                ),
                now
        );
        if (transition.anomaly() != null) {
            String detail = switch (transition.anomaly()) {
                case READY_TOO_EARLY -> "order became ready earlier than the freshness window";
                case READY_LATE -> "order became ready after the Pickup Pact guarantee deadline";
                default -> "fulfillment timing anomaly";
            };
            recordAnomaly(transition.order(), transition.anomaly(), detail, now);
        }
        return transition.order();
    }

    @Transactional
    public MerchantOrder pickup(UUID orderId) {
        MerchantOrder current = required(orderId);
        Instant now = clock.instant();
        MerchantOrder updated = current.pickup(now);
        repository.update(updated);
        repository.appendOutbox(
                orderId,
                "MerchantPickupCompleted",
                Map.of("store_id", updated.storeId(), "picked_up_at", now.toString()),
                now
        );
        return updated;
    }

    public MerchantOrder get(UUID orderId) {
        return required(orderId);
    }

    public List<MerchantFulfillmentRepository.Delivery> pendingDeliveries(
            String storeId,
            long afterSequence,
            int limit
    ) {
        if (storeId == null || storeId.isBlank()) throw new IllegalArgumentException("storeId is required");
        if (afterSequence < 0) throw new IllegalArgumentException("afterSequence must be non-negative");
        if (limit < 1 || limit > 100) throw new IllegalArgumentException("limit must be between 1 and 100");
        return repository.pendingDeliveries(storeId, afterSequence, limit);
    }

    @Transactional
    public void acknowledge(long sequence) {
        if (sequence <= 0) throw new IllegalArgumentException("delivery sequence must be positive");
        repository.acknowledge(sequence);
    }

    public List<MerchantFulfillmentRepository.Effect> effects(UUID orderId) {
        required(orderId);
        return repository.effects(orderId);
    }

    public List<MerchantFulfillmentRepository.Anomaly> anomalies(UUID orderId) {
        required(orderId);
        return repository.anomalies(orderId);
    }

    private void receiveConfirmed(UUID orderId, Instant occurredAt, JsonNode payload) {
        if (repository.find(orderId).isPresent()) return;

        String storeId = requiredText(payload, "store_id");
        Instant pickupAt = Instant.parse(requiredText(payload, "pickup_at"));
        int capacityUnits = payload.path("capacity_units").asInt(0);
        if (capacityUnits <= 0) throw new IllegalArgumentException("capacity_units must be positive");

        MerchantOrder order = MerchantOrder.receive(orderId, storeId, pickupAt, capacityUnits);
        repository.insert(order);

        Map<String, Object> delivery = Map.of(
                "order_id", orderId.toString(),
                "store_id", storeId,
                "pickup_at", pickupAt.toString(),
                "capacity_units", capacityUnits,
                "earliest_start_at", order.window().earliestStartAt().toString(),
                "target_ready_at", order.window().targetReadyAt().toString(),
                "latest_ready_at", order.window().latestReadyAt().toString()
        );
        repository.enqueueDelivery(orderId, storeId, "ORDER_AVAILABLE", delivery);
        repository.recordEffect(orderId, "NEW_ORDER_NOTIFICATION", delivery);
        repository.recordEffect(orderId, "POS_PRINT", delivery);
        repository.appendOutbox(orderId, "MerchantOrderReceived", delivery, occurredAt);
    }

    private void applyCancellation(UUID orderId, Instant occurredAt) {
        var maybeOrder = repository.find(orderId);
        if (maybeOrder.isEmpty()) {
            // A commitment can be cancelled while still HELD, before the merchant
            // context ever receives CommitmentConfirmed. That is a valid no-op here,
            // not a poison event that should block the Kafka partition.
            return;
        }
        MerchantOrder current = maybeOrder.get();
        var transition = current.requestCancellation(occurredAt);
        if (transition.order().version() != current.version()) {
            repository.update(transition.order());
        }

        if (transition.anomaly() == null) {
            repository.enqueueDelivery(
                    orderId,
                    current.storeId(),
                    "ORDER_CANCELLED",
                    Map.of("order_id", orderId.toString(), "cancelled_at", occurredAt.toString())
            );
            repository.appendOutbox(
                    orderId,
                    "MerchantOrderCancelled",
                    Map.of("store_id", current.storeId(), "cancelled_at", occurredAt.toString()),
                    occurredAt
            );
        } else {
            recordAnomaly(
                    transition.order(),
                    transition.anomaly(),
                    "cancellation arrived after preparation had already started",
                    occurredAt
            );
            repository.enqueueDelivery(
                    orderId,
                    current.storeId(),
                    "CANCELLATION_REVIEW",
                    Map.of("order_id", orderId.toString(), "cancelled_at", occurredAt.toString())
            );
        }
    }

    private void applyReschedule(UUID orderId, Instant occurredAt, JsonNode payload) {
        MerchantOrder current = required(orderId);
        Instant pickupAt = Instant.parse(requiredText(payload, "pickup_at"));

        if (current.state() == FulfillmentState.RECEIVED || current.state() == FulfillmentState.ACCEPTED) {
            MerchantOrder updated = current.reschedule(pickupAt);
            repository.update(updated);
            repository.enqueueDelivery(
                    orderId,
                    updated.storeId(),
                    "PICKUP_TIME_CHANGED",
                    Map.of(
                            "order_id", orderId.toString(),
                            "pickup_at", pickupAt.toString(),
                            "earliest_start_at", updated.window().earliestStartAt().toString(),
                            "target_ready_at", updated.window().targetReadyAt().toString()
                    )
            );
            repository.appendOutbox(
                    orderId,
                    "MerchantPickupScheduleChanged",
                    Map.of("store_id", updated.storeId(), "pickup_at", pickupAt.toString()),
                    occurredAt
            );
            return;
        }

        recordAnomaly(
                current,
                FulfillmentAnomalyCode.RESCHEDULE_AFTER_PREPARATION,
                "pickup time changed after preparation started; manual decision required",
                occurredAt
        );
        repository.enqueueDelivery(
                orderId,
                current.storeId(),
                "SCHEDULE_CHANGE_REVIEW",
                Map.of("order_id", orderId.toString(), "requested_pickup_at", pickupAt.toString())
        );
    }

    private void recordAnomaly(
            MerchantOrder order,
            FulfillmentAnomalyCode code,
            String detail,
            Instant observedAt
    ) {
        repository.recordAnomaly(order.orderId(), code, detail, observedAt);
        repository.appendOutbox(
                order.orderId(),
                "FulfillmentAnomalyDetected",
                Map.of(
                        "store_id", order.storeId(),
                        "code", code.name(),
                        "detail", detail,
                        "observed_at", observedAt.toString(),
                        "pickup_at", order.pickupAt().toString(),
                        "latest_ready_at", order.window().latestReadyAt().toString()
                ),
                observedAt
        );
    }

    private MerchantOrder required(UUID orderId) {
        return repository.find(orderId)
                .orElseThrow(() -> new java.util.NoSuchElementException("merchant order not found: " + orderId));
    }

    private static String requiredText(JsonNode payload, String name) {
        String value = payload.path(name).asText("");
        if (value.isBlank()) throw new IllegalArgumentException(name + " is required");
        return value;
    }

    private static boolean isRelevant(String eventType) {
        return eventType.equals("CommitmentConfirmed")
                || eventType.equals("CommitmentCancelled")
                || eventType.equals("PickupRescheduled");
    }
}
