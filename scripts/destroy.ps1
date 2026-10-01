param(
    [Parameter(Mandatory=$true)]
    [string]$Environment,
    [string]$ProjectName = "twin"
)

# Validate environment parameter
if ($Environment -notmatch '^(dev|test|prod)$') {
    Write-Host "Error: Invalid environment '$Environment'" -ForegroundColor Red
    Write-Host "Available environments: dev, test, prod" -ForegroundColor Yellow
    exit 1
}

Write-Host "Preparing to destroy $ProjectName-$Environment infrastructure..." -ForegroundColor Yellow

# Navigate to terraform directory
Set-Location (Join-Path (Split-Path $PSScriptRoot -Parent) "terraform")

# Get AWS Account ID for backend configuration
$awsAccountId = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Could not get AWS account ID - are you logged in to AWS?" -ForegroundColor Red
    exit 1
}
$awsRegion = if ($env:DEFAULT_AWS_REGION) { $env:DEFAULT_AWS_REGION } else { "eu-north-1" }

# Initialize terraform with S3 backend
Write-Host "Initializing Terraform with S3 backend..." -ForegroundColor Yellow
terraform init -input=false -reconfigure `
  -backend-config="bucket=twin-terraform-state-$awsAccountId" `
  -backend-config="key=$Environment/terraform.tfstate" `
  -backend-config="region=$awsRegion" `
  -backend-config="use_lockfile=true" `
  -backend-config="encrypt=true"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: terraform init failed" -ForegroundColor Red
    exit 1
}

# Check if workspace exists (exact name match)
$workspaces = terraform workspace list | ForEach-Object { $_.TrimStart('*', ' ').Trim() }
if ($workspaces -notcontains $Environment) {
    Write-Host "Error: Workspace '$Environment' does not exist" -ForegroundColor Red
    Write-Host "Available workspaces:" -ForegroundColor Yellow
    terraform workspace list
    exit 1
}

# Select the workspace
terraform workspace select $Environment

Write-Host "Emptying S3 buckets..." -ForegroundColor Yellow

# Define bucket names with account ID (matching Day 4 naming)
$FrontendBucket = "$ProjectName-$Environment-frontend-$awsAccountId"
$MemoryBucket = "$ProjectName-$Environment-memory-$awsAccountId"

# Empty frontend bucket if it exists
# (native commands don't throw in PowerShell, so check $LASTEXITCODE instead of try/catch)
aws s3 ls "s3://$FrontendBucket" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Emptying $FrontendBucket..." -ForegroundColor Gray
    aws s3 rm "s3://$FrontendBucket" --recursive
} else {
    Write-Host "  Frontend bucket not found or already empty" -ForegroundColor Gray
}

# Empty memory bucket if it exists
aws s3 ls "s3://$MemoryBucket" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Emptying $MemoryBucket..." -ForegroundColor Gray
    aws s3 rm "s3://$MemoryBucket" --recursive
} else {
    Write-Host "  Memory bucket not found or already empty" -ForegroundColor Gray
}

Write-Host "Running terraform destroy..." -ForegroundColor Yellow

# Create a dummy lambda zip if it doesn't exist (Terraform needs the file even to destroy)
$lambdaZip = Join-Path (Split-Path $PSScriptRoot -Parent) "backend/lambda-deployment.zip"
if (-not (Test-Path $lambdaZip)) {
    Write-Host "Creating dummy lambda package for destroy operation..." -ForegroundColor Gray
    $dummyFile = Join-Path ([System.IO.Path]::GetTempPath()) "dummy.txt"
    Set-Content -Path $dummyFile -Value "dummy"
    Compress-Archive -Path $dummyFile -DestinationPath $lambdaZip -Force
    Remove-Item $dummyFile
}

# Run terraform destroy with auto-approve
if ($Environment -eq "prod" -and (Test-Path "prod.tfvars")) {
    terraform destroy -var-file=prod.tfvars `
                     -var="project_name=$ProjectName" `
                     -var="environment=$Environment" `
                     -auto-approve
} else {
    terraform destroy -var="project_name=$ProjectName" `
                     -var="environment=$Environment" `
                     -auto-approve
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: terraform destroy failed - check the output above" -ForegroundColor Red
    exit 1
}

Write-Host "Infrastructure for $Environment has been destroyed!" -ForegroundColor Green
Write-Host ""
Write-Host "Kept on purpose (not managed by this environment):" -ForegroundColor Cyan
Write-Host "   - Learned Q&A in s3://$ProjectName-knowledge-$awsAccountId" -ForegroundColor White
Write-Host "   - Secrets in SSM Parameter Store (/$ProjectName/...)" -ForegroundColor White
Write-Host ""
Write-Host "The Telegram webhook still points at the deleted API. After deploying again, run:" -ForegroundColor Cyan
Write-Host "   ./scripts/set_telegram_webhook.sh $Environment" -ForegroundColor White
Write-Host ""
Write-Host "  To remove the workspace completely, run:" -ForegroundColor Cyan
Write-Host "   terraform workspace select default" -ForegroundColor White
Write-Host "   terraform workspace delete $Environment" -ForegroundColor White
