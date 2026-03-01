"""
Slack Filth Enforcer Bot — AWS Lambda Handler

A Slack bot that reads channel topics for an opt-in marker, validates messages
using Claude, and rewrites "too clean" messages with creative profanity.

Channel topic format (just include one of these anywhere in the topic):
    🤬 Swearing required
    swearing mandatory
    filth enforced

Deployment: AWS Lambda behind API Gateway (or Lambda Function URL)
Secrets: AWS Secrets Manager
"""

import json
import logging
import os
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
# Channel enforcement cache
# ---------------------------------------------------------------------------

# Opt-in markers — if the topic contains any of these (case-insensitive),
# the channel is enforced. No regex required.
_ENFORCEMENT_MARKERS = ("🤬", "swearing", "filth enforced", "profanity required")

# In-memory cache — survives across warm Lambda invocations
_channel_enforced: dict[str, bool] = {}


def is_channel_enforced(slack: WebClient, channel_id: str) -> bool:
    """Return True if the channel topic opts in to filth enforcement."""
    if channel_id not in _channel_enforced:
        try:
            info = slack.conversations_info(channel=channel_id)
            topic = info["channel"]["topic"]["value"].lower()
            enforced = any(marker.lower() in topic for marker in _ENFORCEMENT_MARKERS)
            _channel_enforced[channel_id] = enforced
            logger.info(f"Channel {channel_id} enforced={enforced}")
        except SlackApiError as e:
            logger.error(f"Failed to get channel info for {channel_id}: {e}")
            _channel_enforced[channel_id] = False

    return _channel_enforced.get(channel_id, False)


def invalidate_channel_cache(channel_id: str):
    """Remove cached enforcement status so it gets reloaded on next message."""
    _channel_enforced.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Claude profanity check
# ---------------------------------------------------------------------------

IS_PROFANE_PROMPT = """You are a profanity detector for a Slack channel that requires swearing.
Answer with ONLY the single word YES or NO.
YES = the message contains genuine swearing or profanity.
NO  = the message is clean, polite, or only contains mild words like "damn" or "hell"."""


def is_profane(text: str) -> bool:
    """Ask Claude whether the message already contains sufficient profanity."""
    claude = get_claude_client()
    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=5,
        system=IS_PROFANE_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    answer = response.content[0].text.strip().upper()
    logger.info(f"is_profane({text[:40]!r}) → {answer}")
    return answer.startswith("Y")


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
- Output ONLY the rewritten message, nothing else. No quotes, no preamble."""


def rewrite_message(text: str) -> str:
    """Use Claude to rewrite a clean message with appropriate filth."""
    claude = get_claude_client()

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=REWRITE_PROMPT,
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

    if not is_channel_enforced(slack, channel):
        return  # Channel not opted in

    # Ask Claude if the message is already profane enough
    if is_profane(text):
        logger.info(f"Message passes profanity check in {channel}")
        return

    # Too clean! Rewrite it.
    logger.info(f"Message too clean in {channel} from {user}: {text[:50]}...")

    try:
        filthy_version = rewrite_message(text)

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
        invalidate_channel_cache(channel)
        logger.info(f"Invalidated enforcement cache for {channel}")


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
