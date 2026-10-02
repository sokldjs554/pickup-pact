"""PostgreSQL journey backend; never boots a private SQLite fallback."""
from ..store import JourneyStore
from ..payments.domain import key
from .journey_repository import PostgresJourneyRepository
from .coordinator import PostgresCoordinator
from .inbox import PostgresPaymentInbox


class PostgresJourneyStore(JourneyStore):
    backend = 'postgresql'
    evidence_scope = 'native_postgresql_development_not_host_ha'

    def __init__(self, dsn, *, fleet, payment_gateway, notification_secret, worker_id,
                 schema='pact_orders', initialize=True, lease_seconds=30, runtime_guard=None,
                 evidence_scope=None):
        key(worker_id)
        if len(worker_id) > 80 or fleet is None or payment_gateway is None:
            raise ValueError('explicit bounded role identity and remote clients required')
        if not isinstance(notification_secret, str) or len(notification_secret) < 24:
            raise ValueError('shared notification secret required')
        self.fleet, self.payment_gateway, self.payment_enabled = fleet, payment_gateway, True
        self.repository = PostgresJourneyRepository(dsn, schema=schema, initialize=initialize,
                                                    runtime_guard=runtime_guard)
        if evidence_scope is not None:
            self.evidence_scope = evidence_scope
        self.payment_inbox = PostgresPaymentInbox(self.repository, notification_secret)
        self.operations = self.payment_operations = PostgresCoordinator(self, worker_id, lease_seconds)
        self.node_id = worker_id

    @property
    def automatic_recovery_enabled(self):
        return self.repository.worker_ready()

    def close(self):
        self.repository.close()

    def create(self, intent, world_id=None, card_token='demo-approved', payment_fault='none'):
        s = self.new_state(intent, world_id, card_token, payment_fault)
        self.repository.create(s)
        return self.view(s)

    def read_state(self, sid):
        return self.repository.read(sid)

    def command(self, sid, c):
        return self.operations.command(sid, c)

    def transfer_control(self, sid, c):
        return self.operations.transfer_control(sid, c)

    def connection(self):
        raise RuntimeError('use native repository units; SQLite SQL is not translated')
