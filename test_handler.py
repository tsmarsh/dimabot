"""Tests for the Slack Filth Enforcer handler."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest

import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test-signing-secret"
BOT_TOKEN = "xoxb-test-token"
USER_TOKEN = "xoxp-test-token"
ANTHROPIC_KEY = "sk-ant-test"

SECRETS = {
    "SLACK_BOT_TOKEN": BOT_TOKEN,
    "SLACK_USER_TOKEN": USER_TOKEN,
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
    handler._channel_enforced.clear()
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
# is_channel_enforced
# ---------------------------------------------------------------------------


class TestIsChannelEnforced:
    def _mock_slack(self, topic: str) -> MagicMock:
        slack = MagicMock()
        slack.conversations_info.return_value = {
            "channel": {"topic": {"value": topic}}
        }
        return slack

    def test_emoji_marker(self):
        slack = self._mock_slack("🤬 Filth mandatory here")
        assert handler.is_channel_enforced(slack, "C1") is True

    def test_swearing_keyword(self):
        slack = self._mock_slack("Swearing required | product team channel")
        assert handler.is_channel_enforced(slack, "C2") is True

    def test_filth_enforced_keyword(self):
        slack = self._mock_slack("filth enforced | dev-banter")
        assert handler.is_channel_enforced(slack, "C3") is True

    def test_normal_topic_not_enforced(self):
        slack = self._mock_slack("General discussion")
        assert handler.is_channel_enforced(slack, "C4") is False

    def test_empty_topic_not_enforced(self):
        slack = self._mock_slack("")
        assert handler.is_channel_enforced(slack, "C5") is False

    def test_result_is_cached(self):
        slack = self._mock_slack("🤬 enforced")
        handler.is_channel_enforced(slack, "C6")
        handler.is_channel_enforced(slack, "C6")
        # Should only hit the API once
        assert slack.conversations_info.call_count == 1

    def test_api_error_returns_false(self):
        from slack_sdk.errors import SlackApiError
        slack = MagicMock()
        slack.conversations_info.side_effect = SlackApiError(
            "not_in_channel", MagicMock(data={"ok": False})
        )
        assert handler.is_channel_enforced(slack, "C7") is False


# ---------------------------------------------------------------------------
# is_profane
# ---------------------------------------------------------------------------


class TestIsProfane:
    @patch.object(handler, "get_claude_client")
    def test_profane_message(self, mock_get_claude):
        mock_claude = MagicMock()
        mock_get_claude.return_value = mock_claude
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="YES")]
        )
        assert handler.is_profane("what the fuck is going on") is True

    @patch.object(handler, "get_claude_client")
    def test_clean_message(self, mock_get_claude):
        mock_claude = MagicMock()
        mock_get_claude.return_value = mock_claude
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="NO")]
        )
        assert handler.is_profane("good morning everyone") is False

    @patch.object(handler, "get_claude_client")
    def test_case_insensitive_yes(self, mock_get_claude):
        mock_claude = MagicMock()
        mock_get_claude.return_value = mock_claude
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="yes")]
        )
        assert handler.is_profane("shit yeah") is True


# ---------------------------------------------------------------------------
# handle_message_event
# ---------------------------------------------------------------------------


class TestHandleMessageEvent:
    def _enforced_slack(self):
        mock_slack = MagicMock()
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "🤬 swearing required"}}
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
        return mock_slack

    @patch.object(handler, "is_profane", return_value=True)
    @patch.object(handler, "get_slack_client")
    def test_profane_message_no_action(self, mock_get_slack, mock_is_profane):
        mock_get_slack.return_value = self._enforced_slack()

        handler.handle_message_event(
            {"channel": "C123", "text": "holy shit that's great", "user": "U1", "ts": "1.1"}
        )

        mock_get_slack.return_value.chat_delete.assert_not_called()
        mock_get_slack.return_value.chat_postMessage.assert_not_called()

    @patch.object(handler, "rewrite_message", return_value="bloody hell mate")
    @patch.object(handler, "is_profane", return_value=False)
    @patch.object(handler, "get_user_client")
    @patch.object(handler, "get_slack_client")
    def test_clean_message_rewrite_and_repost(
        self, mock_get_slack, mock_get_user, mock_is_profane, mock_rewrite
    ):
        mock_slack = self._enforced_slack()
        mock_get_slack.return_value = mock_slack
        mock_user_slack = MagicMock()
        mock_get_user.return_value = mock_user_slack

        handler.handle_message_event(
            {"channel": "C123", "text": "hello world", "user": "U1", "ts": "1.1"}
        )

        mock_rewrite.assert_called_once_with("hello world")
        mock_user_slack.chat_delete.assert_called_once_with(channel="C123", ts="1.1")
        mock_slack.chat_postMessage.assert_called_once_with(
            channel="C123",
            text="bloody hell mate",
            username="testy",
            icon_url="https://img.example.com/72.png",
        )
        mock_slack.chat_postEphemeral.assert_called_once()

    def test_bot_message_skipped(self):
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
    def test_unenforced_channel_ignored(self, mock_get_slack):
        mock_slack = MagicMock()
        mock_get_slack.return_value = mock_slack
        mock_slack.conversations_info.return_value = {
            "channel": {"topic": {"value": "Normal channel — polite discussion only"}}
        }

        handler.handle_message_event(
            {"channel": "C999", "text": "hello", "user": "U1", "ts": "1.1"}
        )

        mock_slack.chat_delete.assert_not_called()

    @patch.object(handler, "rewrite_message", return_value="damn it")
    @patch.object(handler, "is_profane", return_value=False)
    @patch.object(handler, "get_user_client")
    @patch.object(handler, "get_slack_client")
    def test_delete_fails_falls_back_to_thread(
        self, mock_get_slack, mock_get_user, mock_is_profane, mock_rewrite
    ):
        from slack_sdk.errors import SlackApiError

        mock_slack = self._enforced_slack()
        mock_get_slack.return_value = mock_slack
        mock_user_slack = MagicMock()
        mock_get_user.return_value = mock_user_slack
        mock_user_slack.chat_delete.side_effect = SlackApiError(
            message="cant_delete", response=MagicMock(data={"ok": False})
        )

        handler.handle_message_event(
            {"channel": "C123", "text": "hello", "user": "U1", "ts": "1.1"}
        )

        mock_slack.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1.1"


# ---------------------------------------------------------------------------
# handle_topic_change
# ---------------------------------------------------------------------------


class TestHandleTopicChange:
    def test_invalidates_cache(self):
        handler._channel_enforced["C123"] = True
        handler.handle_topic_change({"channel": "C123"})
        assert "C123" not in handler._channel_enforced

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
