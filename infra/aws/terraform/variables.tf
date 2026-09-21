variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "project" {
  type    = string
  default = "pickup-pact"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "db_password" {
  type      = string
  sensitive = true
}
