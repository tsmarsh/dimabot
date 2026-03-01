# 🤬 Slack Filth Enforcer

A Slack bot that **enforces mandatory swearing** in designated channels. If someone posts a message that's too clean, Claude decides it's not good enough, deletes it, and reposts it with creative profanity — under the original user's name and avatar.

No regex required. Claude knows what swearing is.

## How It Works

1. Add `🤬` anywhere in a channel topic to opt in
2. Every message gets judged by Claude: _"is this profane enough?"_
3. If it passes — nothing happens
4. If it's too clean:
   - The original message is **deleted**
   - Claude rewrites it with appropriate filth
   - It's **reposted** with the user's name and avatar (looks like they said it)
   - The user gets an **ephemeral notification** showing the before/after

## Channel Opt-In

Just put one of these anywhere in the channel topic:

```
🤬
🤬 Swearing required
filth enforced
swearing mandatory
```

That's it. No regex, no config files.

## Architecture

```
Slack → Lambda Function URL → handler.py ──→ return 200 immediately
                                  │
                                  └──→ async self-invoke ──→ claude-haiku  (is this profane?)
                                                              │
                                                         if too clean:
                                                              └──→ claude-sonnet  (rewrite it)
                                                                   └──→ delete + repost
```

- **Runtime:** Python 3.12 on AWS Lambda
- **Secrets:** AWS Secrets Manager (`slack-filth-bot/secrets`)
- **Infra:** SAM/CloudFormation (`template.yaml`)
- **No database** — channel enforcement status is cached in Lambda memory, invalidated on topic changes
- **Async processing** — Slack requires a 200 within 3 seconds. The handler returns immediately and re-invokes itself async for all the Claude + Slack work. Falls back to synchronous if invoke fails.

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- AWS credentials configured (`aws configure`)
- A [Slack App](https://api.slack.com/apps) in your workspace
- An [Anthropic API key](https://console.anthropic.com/)

## Slack App Setup

### 1. Create the App

Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.

### 2. Bot Token Scopes

Under **OAuth & Permissions → Scopes → Bot Token Scopes**, add:

| Scope | Why |
|-------|-----|
| `channels:history` | Read messages in public channels |
| `channels:read` | Read channel topics |
| `chat:write` | Post messages |
| `chat:write.customize` | Post with custom username/avatar |
| `users:read` | Get display names and avatars |
| `groups:history` | *(optional)* Private channels |
| `groups:read` | *(optional)* Private channel topics |

### 3. User Token Scopes

The bot needs to **delete user messages**, which requires a user token. Under **OAuth & Permissions → Scopes → User Token Scopes**, add:

| Scope | Why |
|-------|-----|
| `chat:write` | Delete messages posted by users |

### 4. Event Subscriptions

Enable **Event Subscriptions** and subscribe to these bot events:

- `message.channels`
- `message.groups` *(optional — private channels)*

You'll set the Request URL after deploying.

### 5. Install to Workspace

Install (or reinstall) the app. You'll get two tokens:

- **Bot User OAuth Token** (`xoxb-...`) — from the OAuth & Permissions page
- **User OAuth Token** (`xoxp-...`) — from the same page, scroll down

Also grab the **Signing Secret** from Basic Information.

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. Create the secret in AWS Secrets Manager (prompts for all four values first time)
2. Build and deploy the CloudFormation stack
3. Print the Function URL — paste this into Slack's Event Subscriptions Request URL

### Update Secrets Later

```bash
aws secretsmanager put-secret-value \
  --secret-id slack-filth-bot/secrets \
  --secret-string '{
    "SLACK_BOT_TOKEN":      "xoxb-...",
    "SLACK_USER_TOKEN":     "xoxp-...",
    "SLACK_SIGNING_SECRET": "...",
    "ANTHROPIC_API_KEY":    "sk-ant-..."
  }'
```

## Fallback Behaviour

If the user token can't delete the message (e.g. missing permissions), the bot replies in a thread instead:

> 🧼 Too clean! Here's what you *should* have said:
> > [filthy version]

## Testing

```bash
pip install -r requirements.txt pytest
pytest test_handler.py -v
```

## Cost

Very low. Each message that gets rewritten:
- **claude-haiku** call to check profanity: ~$0.0001
- **claude-sonnet** call to rewrite: ~$0.003
- Lambda execution: ~$0.000004
- Secrets Manager: cached on warm starts

For a channel with 100 messages/day and 20% needing rewrites: **~$2–3/month**.
