#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────
# Slack Filth Enforcer — Deploy Script
# ──────────────────────────────────────────────────────

STACK_NAME="slack-filth-enforcer"
SECRET_NAME="slack-filth-bot/secrets"
REGION="${AWS_REGION:-eu-west-2}"
S3_BUCKET="${SAM_BUCKET:-}"  # Set this or let SAM create one

echo "══════════════════════════════════════════════"
echo "  Slack Filth Enforcer — Deployment"
echo "══════════════════════════════════════════════"

# ── Step 1: Create the secret (first time only) ──────
echo ""
echo "Step 1: Checking Secrets Manager..."

if ! aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" 2>/dev/null; then
    echo "Secret not found. Creating..."
    echo ""
    echo "You'll need these values from https://api.slack.com/apps:"
    echo "  - SLACK_BOT_TOKEN     (xoxb-...)"
    echo "  - SLACK_SIGNING_SECRET (from App Credentials)"
    echo "  - ANTHROPIC_API_KEY   (sk-ant-...)"
    echo ""

    read -rp "SLACK_BOT_TOKEN: " SLACK_BOT_TOKEN
    read -rp "SLACK_USER_TOKEN: " SLACK_USER_TOKEN
    read -rp "SLACK_SIGNING_SECRET: " SLACK_SIGNING_SECRET
    read -rp "ANTHROPIC_API_KEY: " ANTHROPIC_API_KEY

    aws secretsmanager create-secret \
        --name "$SECRET_NAME" \
        --region "$REGION" \
        --secret-string "{
            \"SLACK_BOT_TOKEN\": \"$SLACK_BOT_TOKEN\",
            \"SLACK_USER_TOKEN\": \"$SLACK_USER_TOKEN\",
            \"SLACK_SIGNING_SECRET\": \"$SLACK_SIGNING_SECRET\",
            \"ANTHROPIC_API_KEY\": \"$ANTHROPIC_API_KEY\"
        }"

    echo "✅ Secret created: $SECRET_NAME"
else
    echo "✅ Secret already exists: $SECRET_NAME"
fi

# ── Step 2: Build ─────────────────────────────────────
echo ""
echo "Step 2: Building..."
sam build --template-file template.yaml

# ── Step 3: Deploy ────────────────────────────────────
echo ""
echo "Step 3: Deploying..."

DEPLOY_ARGS=(
    --stack-name "$STACK_NAME"
    --region "$REGION"
    --capabilities CAPABILITY_IAM
    --resolve-s3
    --parameter-overrides "SecretName=$SECRET_NAME"
    --no-confirm-changeset
)

sam deploy "${DEPLOY_ARGS[@]}"

# ── Step 4: Get the URL ──────────────────────────────
echo ""
echo "Step 4: Getting Function URL..."

FUNCTION_URL=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" \
    --output text)

echo ""
echo "══════════════════════════════════════════════"
echo "  ✅ DEPLOYED!"
echo ""
echo "  Function URL: $FUNCTION_URL"
echo ""
echo "  Next steps:"
echo "  1. Go to https://api.slack.com/apps"
echo "  2. Event Subscriptions → Request URL: $FUNCTION_URL"
echo "  3. Subscribe to bot events:"
echo "     - message.channels"
echo "     - message.groups (for private channels)"
echo "  4. OAuth Scopes needed:"
echo "     - channels:history"
echo "     - channels:read"
echo "     - chat:write"
echo "     - chat:write.customize"
echo "     - users:read"
echo "  5. Install/reinstall the app to your workspace"
echo "  6. Invite the bot to your filth channels"
echo "  7. Set channel topics with regex, e.g.:"
echo '     🤬 Swearing mandatory | regex:/\b(fuck|shit|damn|arse|bollocks|bloody)\b/i'
echo "══════════════════════════════════════════════"
