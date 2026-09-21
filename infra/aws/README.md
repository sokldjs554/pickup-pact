# AWS deployment blueprint

The intended AWS topology is EKS for services, RDS PostgreSQL, ElastiCache Redis, and MSK Kafka. MongoDB/Elasticsearch can be provided by compatible managed services or operators on EKS depending on cost and operational requirements.

The Terraform is deliberately a **blueprint**, not evidence of a live deployment. A real run must add security groups, private DNS, secrets management, IAM policies, backups, encryption, autoscaling and cost controls before production use.
