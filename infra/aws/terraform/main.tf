data "aws_caller_identity" "current" {}

resource "aws_security_group" "data" {
  name_prefix = "${var.project}-data-"
  vpc_id      = var.vpc_id
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "postgres" {
  identifier             = "${var.project}-postgres"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t4g.micro"
  allocated_storage      = 20
  username               = "pickuppact"
  password               = var.db_password
  db_name                = "pickuppact"
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.data.id]
  skip_final_snapshot    = true
  publicly_accessible    = false
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project}-redis"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.project}-redis"
  description                = "Pickup Pact capacity lease and Celery broker"
  node_type                  = "cache.t4g.micro"
  num_cache_clusters         = 1
  engine                     = "redis"
  automatic_failover_enabled = false
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.data.id]
}

resource "aws_msk_serverless_cluster" "events" {
  cluster_name = "${var.project}-events"
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.data.id]
  }
  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }
}
