# Foundational India-region resources. This does not deploy the application.
resource "aws_sqs_queue" "dial_dead_letter" {
  name                      = "swarvaah-${var.environment}-dial-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "dial" {
  name                       = "swarvaah-${var.environment}-dial"
  visibility_timeout_seconds = 120
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dial_dead_letter.arn
    maxReceiveCount     = 5
  })
}

resource "aws_s3_bucket" "exports" {
  bucket_prefix = "swarvaah-${var.environment}-exports-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket                  = aws_s3_bucket.exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "exports" {
  bucket = aws_s3_bucket.exports.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    id     = "expire-pilot-exports"
    status = "Enabled"
    expiration { days = 30 }
  }
}

output "dial_queue_url" { value = aws_sqs_queue.dial.url }
output "exports_bucket" { value = aws_s3_bucket.exports.bucket }
