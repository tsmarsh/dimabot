"""
Slack Filth Enforcer Bot — AWS Lambda Handler

A Slack bot that reads channel topics for regex patterns, validates messages
against them, and rewrites "too clean" messages using Claude. Designed for
joke channels where swearing is mandatory.

Channel topic format:
    🤬 Swearing required | regex:/\b(fuck|shit|damn|arse|bollocks|bloody)\b/i

Deployment: AWS Lambda behind API Gateway (or Lambda Function URL)
Secrets: AWS Secrets Manager
"""

import json
import logging
import os
import re
import hashlib
import hmac
import time
import boto3
import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_secrets_cache = {}


def get_secrets() -> dict:
    """Fetch secrets from AWS Secrets Manager (cached for Lambda warm starts)."""
    if _secrets_cache:
        return _secrets_cache

    secret_name = os.environ.get("SECRET_NAME", "slack-filth-bot/secrets")
    region = os.environ.get("AWS_REGION", "eu-west-2")

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secrets = json.loads(response["SecretString"])
    _secrets_cache.update(secrets)
    return _secrets_cache


def get_slack_client() -> WebClient:
    secrets = get_secrets()
    return WebClient(token=secrets["SLACK_BOT_TOKEN"])


def get_user_client() -> WebClient:
    """User token client — required for chat.delete on messages we didn't post."""
    secrets = get_secrets()
    return WebClient(token=secrets["SLACK_USER_TOKEN"])


def get_claude_client() -> anthropic.Anthropic:
    secrets = get_secrets()
    return anthropic.Anthropic(api_key=secrets["ANTHROPIC_API_KEY"])


# ---------------------------------------------------------------------------
# Channel rule cache (regex extracted from topic)
# ---------------------------------------------------------------------------

# In-memory cache — survives across warm Lambda invocations
_channel_rules: dict[str, re.Pattern | None] = {}

REGEX_TOPIC_PATTERN = re.compile(r"regex:\/(.*?)\/([gimsux]*)", re.IGNORECASE)


def extract_regex_from_topic(topic: str) -> re.Pattern | None:
    """Parse a regex from a channel topic string like 'regex:/pattern/flags'."""
    match = REGEX_TOPIC_PATTERN.search(topic)
    if not match:
        return None

    pattern_str = match.group(1)
    flags_str = match.group(2).lower()

    flags = 0
    if "i" in flags_str:
        flags |= re.IGNORECASE
    if "m" in flags_str:
        flags |= re.MULTILINE
    if "s" in flags_str:
        flags |= re.DOTALL
    if "x" in flags_str:
        flags |= re.VERBOSE

    try:
        return re.compile(pattern_str, flags)
    except re.error as e:
        logger.warning(f"Invalid regex in topic: {pattern_str} — {e}")
        return None


def get_channel_rule(slack: WebClient, channel_id: str) -> re.Pattern | None:
    """Get (and cache) the regex rule for a channel from its topic."""
    if channel_id not in _channel_rules:
        try:
            info = slack.conversations_info(channel=channel_id)
            topic = info["channel"]["topic"]["value"]
            _channel_rules[channel_id] = extract_regex_from_topic(topic)
            logger.info(
                f"Loaded rule for {channel_id}: {_channel_rules[channel_id]}"
            )
        except SlackApiError as e:
            logger.error(f"Failed to get channel info for {channel_id}: {e}")
            _channel_rules[channel_id] = None

    return _channel_rules.get(channel_id)


def invalidate_channel_rule(channel_id: str):
    """Remove cached rule so it gets reloaded on next message."""
    _channel_rules.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Claude rewriting
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """You are the Filth Enforcer, a foul-mouthed but lovable bot that
rewrites sanitised messages to include creative, funny profanity.

Rules:
- Keep the EXACT same meaning and intent of the original message.
- Add swearing/profanity naturally — don't just prepend "fuck" to everything.
- Be creative and funny, never cruel or targeted at individuals.
- Keep roughly the same length — don't turn a short message into an essay.
- Preserve any @mentions, links, and emoji exactly as they are.
- If the message contains code blocks or formatted content, only modify the prose.
- The rewrite must pass this regex: {regex_pattern}
- Output ONLY the rewritten message, nothing else. No quotes, no preamble."""


def rewrite_message(text: str, regex_pattern: str) -> str:
    """Use Claude to rewrite a clean message with appropriate filth."""
    claude = get_claude_client()

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=REWRITE_PROMPT.format(regex_pattern=regex_pattern),
        messages=[
            {
                "role": "user",
                "content": f"Rewrite this message:\n\n{text}",
            }
        ],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Slack request verification
# ---------------------------------------------------------------------------


def verify_slack_signature(headers: dict, body: str) -> bool:
    """Verify the request came from Slack using signing secret.

    See: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    secrets = get_secrets()
    signing_secret = secrets["SLACK_SIGNING_SECRET"]

    timestamp = headers.get("x-slack-request-timestamp", "")
    slack_signature = headers.get("x-slack-signature", "")

    if not timestamp or not slack_signature:
        return False

    # Reject requests older than 5 minutes (replay attack protection)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    computed = (
        "v0="
        + hmac.HMAC(
            signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(computed, slack_signature)


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


def handle_message_event(event: dict):
    """Process a message event — validate and rewrite if too clean."""
    # Skip bot messages, message_changed metadata, etc.
    if event.get("bot_id") or event.get("subtype"):
        return

    channel = event.get("channel")
    text = event.get("text", "")
    user = event.get("user")
    ts = event.get("ts")

    if not text or not user or not channel:
        return

    slack = get_slack_client()
    pattern = get_channel_rule(slack, channel)

    if pattern is None:
        return  # No rule for this channel

    # Message passes the regex — it's filthy enough
    if pattern.search(text):
        logger.info(f"Message passes in {channel}")
        return

    # Too clean! Rewrite it.
    logger.info(f"Message too clean in {channel} from {user}: {text[:50]}...")

    try:
        filthy_version = rewrite_message(text, pattern.pattern)

        # Get user info for attribution
        user_info = slack.users_info(user=user)
        profile = user_info["user"]["profile"]
        display_name = (
            profile.get("display_name")
            or user_info["user"].get("real_name", "Someone")
        )
        avatar_url = profile.get("image_72", "")

        # Delete the clean message using user token (bot can't delete user messages)
        try:
            user_slack = get_user_client()
            user_slack.chat_delete(channel=channel, ts=ts)
        except SlackApiError as e:
            # Fall back to thread reply if user token missing or lacks permission
            logger.warning(f"Couldn't delete message: {e}")
            slack.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=f"🧼 Too clean! Here's what you *should* have said:\n>{filthy_version}",
            )
            return

        # Repost with user's name and avatar
        slack.chat_postMessage(
            channel=channel,
            text=filthy_version,
            username=display_name,
            icon_url=avatar_url,
        )

        # Ephemeral notice to the user
        slack.chat_postEphemeral(
            channel=channel,
            user=user,
            text=(
                f"🧼 Your message was too clean for this channel and has been improved.\n"
                f"*Original:* _{text}_\n"
                f"*Improved:* {filthy_version}"
            ),
        )

    except Exception as e:
        logger.error(f"Error rewriting message: {e}", exc_info=True)


def handle_topic_change(event: dict):
    """Invalidate cache when a channel topic changes."""
    channel = event.get("channel")
    if channel:
        invalidate_channel_rule(channel)
        logger.info(f"Invalidated rule cache for {channel}")


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def _dispatch_event(evt: dict):
    """Route a Slack event to the appropriate handler."""
    if evt.get("type") == "message":
        if evt.get("subtype") == "channel_topic":
            handle_topic_change(evt)
        else:
            handle_message_event(evt)


def lambda_handler(event, context):
    """
    AWS Lambda handler for Slack Events API.

    Expects API Gateway proxy integration or Lambda Function URL.

    Slack requires a 200 response within 3 seconds. Because Claude rewrites
    and Slack API calls can exceed that, this handler responds immediately
    and re-invokes itself asynchronously (InvocationType='Event') to do
    the actual processing.
    """

    # ── Async self-invocation (internal, no signature check needed) ──
    if event.get("_async_processing"):
        _dispatch_event(event["slack_event"])
        return {"statusCode": 200, "body": "ok"}

    # ── External request from Slack ──
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body_str = event.get("body", "")

    # Handle base64 encoding from API Gateway
    if event.get("isBase64Encoded"):
        import base64

        body_str = base64.b64decode(body_str).decode("utf-8")

    # Verify Slack signature
    if not verify_slack_signature(headers, body_str):
        logger.warning("Invalid Slack signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    body = json.loads(body_str)

    # Handle Slack URL verification challenge (must be synchronous)
    if body.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": body["challenge"]}),
        }

    # Handle event callbacks — respond 200 immediately, process async
    if body.get("type") == "event_callback":
        evt = body.get("event", {})

        try:
            boto3.client("lambda").invoke(
                FunctionName=context.function_name,
                InvocationType="Event",
                Payload=json.dumps(
                    {"_async_processing": True, "slack_event": evt}
                ),
            )
        except Exception as e:
            logger.error(
                f"Async self-invoke failed, processing synchronously: {e}"
            )
            _dispatch_event(evt)

    return {"statusCode": 200, "body": "ok"}
