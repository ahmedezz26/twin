#!/bin/bash
set -e

# Points the Telegram bot's webhook at this environment's API Gateway, so your replies
# to "unanswered question" notifications reach the twin.
# Run once after creating an environment (its API URL changes after destroy + deploy).
# Telegram allows one webhook per bot: the environment you run this for last receives the replies.

ENVIRONMENT=${1:-dev}
AWS_REGION=${DEFAULT_AWS_REGION:-eu-north-1}

cd "$(dirname "$0")/../terraform"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
terraform init -input=false -reconfigure \
  -backend-config="bucket=twin-terraform-state-${AWS_ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="use_lockfile=true" \
  -backend-config="encrypt=true" > /dev/null
terraform workspace select "$ENVIRONMENT" > /dev/null

API_URL=$(terraform output -raw api_gateway_url)
WEBHOOK_URL="${API_URL}/telegram-webhook"

# Read the secrets from SSM straight into variables - they are never printed
BOT_TOKEN=$(aws ssm get-parameter --name /twin/telegram/bot_token --with-decryption \
  --query Parameter.Value --output text --region "$AWS_REGION")
WEBHOOK_SECRET=$(aws ssm get-parameter --name /twin/telegram/webhook_secret --with-decryption \
  --query Parameter.Value --output text --region "$AWS_REGION")

echo "🔗 Setting Telegram webhook to: ${WEBHOOK_URL}"
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message"]'
echo ""

echo "ℹ️ Webhook status:"
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
echo ""
