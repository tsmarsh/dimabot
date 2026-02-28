"""Tests for the Slack Filth Enforcer handler."""

import hashlib
import hmac
import json
import re
import time
from unittest.mock import MagicMock, patch

import pytest

import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test-signing-secret"
BOT_TOKEN = "xoxb-test-token"
ANTHROPIC_KEY = "sk-ant-test"

SECRETS = {
    "SLACK_BOT_TOKEN": BOT_TOKEN,
    "SLACK_SIGNING_SECRET": SIGNING_SECRET,
    "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
}


def _make_signature(body: str, timestamp: str | None = None) -> tuple[str, str]:
    """Create a valid Slack signature for the given body."""
    ts = timestamp or str(int(time.time()))
    sig_basestring = f"v0:{ts}:{body}"
    sig = (
        "v0="
        + hmac.HMAC(
            SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
    )
    return ts, sig


def _api_gw_event(body: dict, valid_sig: bool = True) -> dict:
    """Build an API Gateway / Function URL proxy event."""
    body_str = json.dumps(body)
    if valid_sig:
        ts, sig = _make_signature(body_str)
    else:
        ts, sig = str(int(time.time())), "v0=badsignature"

    return {
        "headers": {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
        "body": body_str,
        "isBase64Encoded": False,
    }


@pytest.fixture(autouse=True)
def _patch_secrets():
    """Patch get_secrets for every test."""
    handler._secrets_cache.clear()
    handler._channel_rules.clear()
    with patch.object(handler, "get_secrets", return_value=SECRETS):
        yield


# ---------------------------------------------------------------------------
# verify_slack_signature
# ---------------------------------------------------------------------------


class TestVerifySlackSignature:
    def test_valid_signature(self):
        body = '{"type": "event_callback"}'
        ts, sig = _make_signature(body)
        headers = {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        }
        assert handler.verify_slack_signature(headers, body) is True

    def test_expired_timestamp(self):
        body = '{"type": "event_callback"}'
        old_ts = str(int(time.time()) - 600)  # 10 minutes ago
        ts, sig = _make_signature(body, timestamp=old_ts)
        headers = {
            "x-slack-request-timestamp": old_ts,
            "x-slack-signature": sig,
        }
        assert handler.verify_slack_signature(headers, body) is False

    def test_tampered_body(self):
        body = '{"type": "event_callback"}'
        ts, sig = _make_signature(body)
        headers = {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        }
        assert handler.verify_slack_signature(headers, body + "x") is False

    def test_missing_timestamp(self):
        headers = {"x-slack-signature": "v0=abc"}
        assert handler.verify_slack_signature(headers, "body") is False

    def test_missing_signature(self):
        headers = {"x-slack-request-timestamp": str(int(time.time()))}
        assert handler.verify_slack_signature(headers, "body") is False

    def test_empty_headers(self):
        assert handler.verify_slack_signature({}, "body") is False

    def test_non_numeric_timestamp(self):
        headers = {
            "x-slack-request-timestamp": "not-a-number",
            "x-slack-signature": "v0=abc",
        }
        assert handler.verify_slack_signature(headers, "body") is False


# ---------------------------------------------------------------------------
# extract_regex_from_topic
# ---------------------------------------------------------------------------


class TestExtractRegexFromTopic:
    def test_happy_path(self):
        topic = "🤬 Swearing mandatory | regex:/\\b(fuck|shit)\\b/i"
        pat = handler.extract_regex_from_topic(topic)
        assert pat is not None
        assert pat.search("oh shit")
        assert not pat.search("oh darn")

    def test_case_insensitive_flag(self):
        pat = handler.extract_regex_from_topic("regex:/hello/i")
        assert pat is not None
        assert pat.search("HELLO")

    def test_no_regex_in_topic(self):
        assert handler.extract_regex_from_topic("Just a normal topic") is None

    def test_empty_topic(self):
        assert handler.extract_regex_from_topic("") is None

    def test_invalid_regex(self):
        assert handler.extract_regex_from_topic("regex:/[invalid/") is None

    def test_multiline_flag(self):
        pat = handler.extract_regex_from_topic("regex:/^hello/m")
        assert pat is not None
        assert pat.flags & re.MULTILINE

    def test_dotall_flag(self):
        pat = handler.extract_regex_from_topic("regex:/a.b/s")
        assert pat is not None
        assert pat.flags & re.DOTALL

    def test_verbose_flag(self):
        pat = handler.extract_regex_from_topic("regex:/a b/x")
        assert pat is not None
        assert pat.flags & re.VERBOSE

    def test_multiple_flags(self):
        pat = handler.extract_regex_from_topic("regex:/test/ims")
        assert pat is not None
        assert pat.flags & re.IGNORECASE
        assert pat.flags & re.MULTILINE
        assert pat.flags & re.DOTALL

    def test_no_flags(self):
        pat = handler.extract_regex_from_topic("regex:/hello/")
        assert pat is not None
        assert pat.search("hello")
        assert not pat.search("HELLO")


# ---------------------------------------------------------------------------
# handle_message_event
# ---------------------------------------------------------------------------


class TestHandleMessageEvent:
    @patch.object(handler, "get_slack_client")
    def test_message_passes_regex_no_action(self, mock_get_slack):
        """Message containing a swear word should pass without any action."""
        mock_slack = MagicMock()
        mock_get_slack.return_value = mock_slack
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "regex:/damn/i"}}
        }

        handler.handle_message_event(
            {"channel": "C123", "text": "damn right", "user": "U1", "ts": "1.1"}
        )

        mock_slack.chat_delete.assert_not_called()
        mock_slack.chat_postMessage.assert_not_called()

    @patch.object(handler, "rewrite_message", return_value="bloody hell mate")
    @patch.object(handler, "get_slack_client")
    def test_message_too_clean_rewrite_and_repost(
        self, mock_get_slack, mock_rewrite
    ):
        """Clean message should be deleted, rewritten, and reposted."""
        mock_slack = MagicMock()
        mock_get_slack.return_value = mock_slack
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "regex:/damn/i"}}
        }
        mock_slack.users_info.return_value = {
            "user": {
                "real_name": "Test User",
                "profile": {
                    "display_name": "testy",
                    "image_72": "https://img.example.com/72.png",
                },
            }
        }

        handler.handle_message_event(
            {"channel": "C123", "text": "hello world", "user": "U1", "ts": "1.1"}
        )

        mock_rewrite.assert_called_once()
        mock_slack.chat_delete.assert_called_once_with(channel="C123", ts="1.1")
        mock_slack.chat_postMessage.assert_called_once_with(
            channel="C123",
            text="bloody hell mate",
            username="testy",
            icon_url="https://img.example.com/72.png",
        )
        mock_slack.chat_postEphemeral.assert_called_once()

    def test_bot_message_skipped(self):
        """Bot messages should be silently skipped."""
        # No mocks needed — should return before any Slack/Claude calls
        handler.handle_message_event(
            {
                "bot_id": "B123",
                "channel": "C123",
                "text": "bot says hi",
                "user": "U1",
                "ts": "1.1",
            }
        )

    def test_subtype_message_skipped(self):
        """Messages with a subtype (e.g. message_changed) should be skipped."""
        handler.handle_message_event(
            {
                "subtype": "message_changed",
                "channel": "C123",
                "text": "edited",
                "user": "U1",
                "ts": "1.1",
            }
        )

    def test_missing_text_skipped(self):
        handler.handle_message_event(
            {"channel": "C123", "user": "U1", "ts": "1.1"}
        )

    @patch.object(handler, "get_slack_client")
    def test_no_rule_for_channel(self, mock_get_slack):
        """Channels without a regex topic should be ignored."""
        mock_slack = MagicMock()
        mock_get_slack.return_value = mock_slack
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "No regex here"}}
        }

        handler.handle_message_event(
            {"channel": "C999", "text": "hello", "user": "U1", "ts": "1.1"}
        )

        mock_slack.chat_delete.assert_not_called()

    @patch.object(handler, "rewrite_message", return_value="damn it")
    @patch.object(handler, "get_slack_client")
    def test_delete_fails_falls_back_to_thread(
        self, mock_get_slack, mock_rewrite
    ):
        """If chat_delete fails, bot should reply in a thread instead."""
        from slack_sdk.errors import SlackApiError

        mock_slack = MagicMock()
        mock_get_slack.return_value = mock_slack
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "regex:/damn/i"}}
        }
        mock_slack.users_info.return_value = {
            "user": {
                "real_name": "Test",
                "profile": {"display_name": "testy", "image_72": ""},
            }
        }
        mock_slack.chat_delete.side_effect = SlackApiError(
            message="cant_delete", response=MagicMock(data={"ok": False})
        )

        handler.handle_message_event(
            {"channel": "C123", "text": "hello", "user": "U1", "ts": "1.1"}
        )

        # Should fall back to thread reply
        mock_slack.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1.1"


# ---------------------------------------------------------------------------
# handle_topic_change
# ---------------------------------------------------------------------------


class TestHandleTopicChange:
    def test_invalidates_cache(self):
        handler._channel_rules["C123"] = re.compile(r"test")
        handler.handle_topic_change({"channel": "C123"})
        assert "C123" not in handler._channel_rules

    def test_no_channel_key(self):
        # Should not raise
        handler.handle_topic_change({})


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    def test_url_verification_challenge(self):
        body = {"type": "url_verification", "challenge": "abc123"}
        event = _api_gw_event(body)
        ctx = MagicMock()

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 200
        resp_body = json.loads(result["body"])
        assert resp_body["challenge"] == "abc123"

    def test_invalid_signature_returns_401(self):
        body = {"type": "event_callback", "event": {"type": "message"}}
        event = _api_gw_event(body, valid_sig=False)
        ctx = MagicMock()

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 401

    @patch("boto3.client")
    def test_event_callback_invokes_async(self, mock_boto_client):
        """event_callback should return 200 and invoke Lambda async."""
        mock_lambda = MagicMock()
        mock_boto_client.return_value = mock_lambda

        body = {
            "type": "event_callback",
            "event": {"type": "message", "text": "hi", "channel": "C1", "user": "U1"},
        }
        event = _api_gw_event(body)
        ctx = MagicMock()
        ctx.function_name = "slack-filth-enforcer"

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 200
        mock_lambda.invoke.assert_called_once()
        invoke_kwargs = mock_lambda.invoke.call_args.kwargs
        assert invoke_kwargs["InvocationType"] == "Event"
        payload = json.loads(invoke_kwargs["Payload"])
        assert payload["_async_processing"] is True
        assert payload["slack_event"]["type"] == "message"

    @patch.object(handler, "_dispatch_event")
    def test_async_self_invocation(self, mock_dispatch):
        """Internal async invocations should dispatch directly."""
        event = {
            "_async_processing": True,
            "slack_event": {"type": "message", "text": "hi"},
        }
        ctx = MagicMock()

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 200
        mock_dispatch.assert_called_once_with({"type": "message", "text": "hi"})

    @patch.object(handler, "_dispatch_event")
    @patch("boto3.client")
    def test_async_invoke_failure_falls_back_sync(
        self, mock_boto_client, mock_dispatch
    ):
        """If async self-invoke fails, should process synchronously."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.side_effect = Exception("Lambda invoke failed")
        mock_boto_client.return_value = mock_lambda

        body = {
            "type": "event_callback",
            "event": {"type": "message", "text": "hi", "channel": "C1", "user": "U1"},
        }
        event = _api_gw_event(body)
        ctx = MagicMock()
        ctx.function_name = "slack-filth-enforcer"

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 200
        mock_dispatch.assert_called_once()

    def test_unknown_event_type_returns_200(self):
        body = {"type": "app_rate_limited"}
        event = _api_gw_event(body)
        ctx = MagicMock()

        result = handler.lambda_handler(event, ctx)

        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# _dispatch_event
# ---------------------------------------------------------------------------


class TestDispatchEvent:
    @patch.object(handler, "handle_message_event")
    def test_routes_message(self, mock_handle):
        handler._dispatch_event({"type": "message", "text": "hi"})
        mock_handle.assert_called_once()

    @patch.object(handler, "handle_topic_change")
    def test_routes_topic_change(self, mock_handle):
        handler._dispatch_event(
            {"type": "message", "subtype": "channel_topic", "channel": "C1"}
        )
        mock_handle.assert_called_once()

    @patch.object(handler, "handle_message_event")
    @patch.object(handler, "handle_topic_change")
    def test_ignores_unknown_event_type(self, mock_topic, mock_msg):
        handler._dispatch_event({"type": "reaction_added"})
        mock_topic.assert_not_called()
        mock_msg.assert_not_called()
