from demo.route.payments.http_client import PaymentClient
from demo.route.payments.domain import validate


class RepositoryPaymentClient:
    transport = 'local_test'

    def __init__(self, repository):
        self.repo = repository

    def operation(self, command):
        c = validate(command)
        result = self.repo.operation(c['operation_key'])
        return None if result is None else PaymentClient._bound(result, c)

    def execute(self, command, *, fault='none'):
        c = validate(command)
        result = self.repo.execute(c, notification_copies=2 if fault == 'duplicate_notification' else 1,
                                   notification_delay=3 if fault == 'late_notification' else 0)
        if fault == 'drop_reply' and self.repo.consume_fault(c['world_id'], c['operation_key'], fault):
            raise OSError('response lost after commit')
        return PaymentClient._bound(result, c)

    def snapshot(self, world, order_id):
        return self.repo.snapshot(world, order_id)

    def health(self):
        return dict(service='pickup-payment', storage_ready=self.repo.storage_ready())
