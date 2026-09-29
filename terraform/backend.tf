terraform {
  backend "s3" {
    # These values are set by the deployment scripts via -backend-config
    # (bucket, key, region, encrypt, use_lockfile).
    # State locking uses S3 native lock files (use_lockfile) - no DynamoDB table needed.
  }
}
