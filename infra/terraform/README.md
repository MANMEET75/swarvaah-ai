# Terraform starter

This configuration creates a private, encrypted SQS dial queue with a dead-letter queue and a private, encrypted S3 export bucket in `ap-south-1`. It is a **foundation only**. The current worker still polls the database outbox and does not consume SQS. Do not run `terraform apply` expecting a deployable calling platform.

Before production, add an existing-network or dedicated VPC integration, ECS services for API/worker/gateway, TLS load balancer, encrypted PostgreSQL and Redis, Secrets Manager, IAM least privilege, logging, backups, alarms, and tested migration/deployment procedures. Keep Terraform state encrypted and access restricted.

To inspect syntax and the planned resources after installing Terraform and configuring an AWS account:

```bash
terraform init
terraform validate
terraform plan
```
