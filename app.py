# app.py
import os
import re

from slack_bolt import App
from slack_sdk import WebClient

from channels import fetch_user_channels, format_channel_list

bot_token = os.environ.get("SLACK_BOT_TOKEN")
user_token = os.environ.get("SLACK_USER_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

app = App(token=bot_token, signing_secret=signing_secret, token_verification_enabled=False)

bot_client = WebClient(token=bot_token)
user_client = WebClient(token=user_token)

# Matches Slack's encoded user mention: <@U12345> or <@U12345|username>
USER_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")


def handle_mychannels(ack, command, respond):
    ack()

    requesting_user_id = command["user_id"]
    text = command.get("text", "").strip()

    # Determine target user
    mention_match = USER_MENTION_RE.search(text)
    if mention_match:
        target_user_id = mention_match.group(1)
    elif text:
        respond(text="Usage: `/mychannels` or `/mychannels @user`", response_type="ephemeral")
        return
    else:
        target_user_id = requesting_user_id

    # If looking up someone else, check admin
    if target_user_id != requesting_user_id:
        try:
            info = bot_client.users_info(user=requesting_user_id)
            if not info["user"].get("is_admin", False):
                respond(
                    text="Only workspace admins can look up other users' channels.",
                    response_type="ephemeral",
                )
                return
        except Exception:
            respond(text="Something went wrong, please try again.", response_type="ephemeral")
            return

    # Get the target user's display name
    try:
        target_info = bot_client.users_info(user=target_user_id)
        user_name = target_info["user"].get("real_name", target_user_id)
    except Exception:
        respond(text="Couldn't find that user.", response_type="ephemeral")
        return

    # Fetch channels
    try:
        channels = fetch_user_channels(user_client, target_user_id)
    except Exception:
        respond(
            text="Couldn't retrieve channels for that user. If their account is "
                 "deactivated, their channel list is no longer available. "
                 "This tool works best when run before deactivation.",
            response_type="ephemeral",
        )
        return

    message = format_channel_list(channels, user_name)
    respond(text=message, response_type="ephemeral")

    # If truncated, DM the full untruncated list
    if f"of {len(channels)} channels" in message and "full list sent via DM" in message:
        full_message = format_channel_list(channels, user_name, max_length=None)
        try:
            bot_client.chat_postMessage(channel=requesting_user_id, text=full_message)
        except Exception:
            pass  # Best effort DM


app.command("/mychannels")(handle_mychannels)

if __name__ == "__main__":
    app.start(port=int(os.environ.get("PORT", 3000)))
