output "postgres_endpoint" { value = aws_db_instance.postgres.address }
output "redis_endpoint" { value = aws_elasticache_replication_group.redis.primary_endpoint_address }
output "msk_arn" { value = aws_msk_serverless_cluster.events.arn }
