# 🤬 Slack Filth Enforcer

A Slack bot that **enforces mandatory swearing** in designated channels. If someone posts a message that's too clean, it gets deleted and reposted with creative profanity courtesy of Claude.

## How It Works

1. Set a channel topic containing a regex: `🤬 Swearing mandatory | regex:/\b(fuck|shit|damn|arse|bollocks|bloody)\b/i`
2. The bot validates every message against the regex
3. Messages that fail (too clean!) get:
   - Deleted
   - Rewritten by Claude with appropriate filth
   - Reposted with the original user's name and avatar
   - The user gets an ephemeral notification showing the before/after

## Architecture

```
Slack → Lambda Function URL → handler.py ──→ return 200 immediately
                                  │
                                  └──→ async self-invoke ──→ Claude API
                                                              ↓
                                                      rewrite + repost
```

- **Runtime:** Python 3.12 on AWS Lambda
- **Secrets:** AWS Secrets Manager (`slack-filth-bot/secrets`)
- **Infra:** SAM/CloudFormation (template.yaml)
- **No database** — channel rules are cached in Lambda memory and refreshed from Slack channel topics
- **Async processing** — Slack requires a 200 response within 3 seconds. The handler returns 200 immediately and re-invokes itself asynchronously (`InvocationType='Event'`) to perform the Claude rewrite and Slack API calls. If the async invocation fails, it falls back to synchronous processing.

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- AWS credentials configured (`aws configure`)
- A [Slack App](https://api.slack.com/apps) created in your workspace
- An [Anthropic API key](https://console.anthropic.com/)

## Slack App Setup

### 1. Create the App

Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch.

### 2. OAuth Scopes

Under **OAuth & Permissions**, add these Bot Token Scopes:

| Scope | Why |
|-------|-----|
| `channels:history` | Read messages in public channels |
| `channels:read` | Read channel info (topics) |
| `chat:write` | Post messages |
| `chat:write.customize` | Post with custom username/avatar |
| `users:read` | Get user display names and avatars |
| `groups:history` | *(optional)* Read messages in private channels |
| `groups:read` | *(optional)* Read private channel topics |

### 3. Event Subscriptions

Enable **Event Subscriptions** and set the Request URL to your Lambda Function URL (you'll get this after deploying).

Subscribe to these **bot events**:

- `message.channels` — messages in public channels
- `message.groups` — *(optional)* messages in private channels

### 4. Install to Workspace

Install the app and copy the **Bot User OAuth Token** (`xoxb-...`).

Also grab the **Signing Secret** from the app's Basic Information page.

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. Create the secret in AWS Secrets Manager (first time — prompts for tokens)
2. Build with SAM
3. Deploy the CloudFormation stack
4. Print the Function URL to paste into Slack

### Update Secrets Later

```bash
aws secretsmanager put-secret-value \
  --secret-id slack-filth-bot/secrets \
  --secret-string '{
    "SLACK_BOT_TOKEN": "xoxb-...",
    "SLACK_SIGNING_SECRET": "...",
    "ANTHROPIC_API_KEY": "sk-ant-..."
  }'
```

## Channel Topic Format

The bot looks for a regex in the channel topic:

```
regex:/PATTERN/FLAGS
```

**Examples:**

| Topic | Effect |
|-------|--------|
| `regex:/\b(fuck\|shit\|damn\|arse\|bollocks)\b/i` | Must contain at least one swear word |
| `regex:/🍕/` | Every message must contain a pizza emoji |
| `regex:/\b\d+\b/` | Every message must contain a number |
| `Fun channel \| regex:/!{3,}/` | Every message must contain at least 3 exclamation marks |

**Supported flags:** `i` (case-insensitive), `m` (multiline), `s` (dotall), `x` (verbose)

## Fallback Behaviour

If the bot can't delete a message (missing permissions), it replies in a thread instead:

> 🧼 Too clean! Here's what you *should* have said:
> > [filthy version]

## Testing

```bash
pip install -r requirements.txt pytest
pytest test_handler.py -v
```

### Local Integration Testing

```bash
# Set env vars for local testing
export SECRET_NAME=slack-filth-bot/secrets
export AWS_REGION=eu-west-2

# Use SAM local for testing
sam local invoke FilthEnforcerFunction -e test-event.json
```

## Cost

Extremely low. Each invocation:
- ~200ms Lambda execution (~$0.000004)
- 1 Secrets Manager call (cached on warm starts)
- 1 Claude Sonnet 4.5 API call for rewrites only (~$0.003 per rewrite)
- Slack API calls (free)

For a channel with 100 messages/day where 20% need rewriting: ~$2/month.
