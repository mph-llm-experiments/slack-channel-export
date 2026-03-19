def fetch_user_channels(client, user_id):
    """Fetch all channels for a user, paginating through results.

    Args:
        client: Slack WebClient (must be initialized with a user token
                that has channels:read and groups:read scopes).
        user_id: The Slack user ID to look up.

    Returns:
        List of channel dicts from the Slack API.
    """
    all_channels = []
    cursor = None

    while True:
        kwargs = {
            "user": user_id,
            "types": "public_channel,private_channel",
            "limit": 200,
            "exclude_archived": True,
        }
        if cursor:
            kwargs["cursor"] = cursor

        response = client.users_conversations(**kwargs)
        all_channels.extend(response["channels"])

        cursor = response.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return all_channels


def format_channel_list(channels, user_name, max_length=40000):
    """Format a list of channels into a readable Slack message.

    Args:
        channels: List of channel dicts from the Slack API.
        user_name: Display name for the header.
        max_length: Maximum character length of the returned message. If None,
                    no truncation is applied (useful for DM copies). Defaults
                    to 40000 (Slack's plain-text limit).

    Returns:
        Formatted string for the Slack ephemeral response.
    """
    total = len(channels)
    header = f"📋 Channels for @{user_name} ({total} channel{'s' if total != 1 else ''})"

    if total == 0:
        return f"{header}\n\nNo channels found."

    public = sorted(
        [c for c in channels if not c.get("is_private")],
        key=lambda c: c["name"],
    )
    private = sorted(
        [c for c in channels if c.get("is_private")],
        key=lambda c: c["name"],
    )

    lines = [header, ""]

    if public:
        lines.append("Public:")
        for c in public:
            purpose = c.get("purpose", {}).get("value", "")
            members = c.get("num_members", 0)
            line = f"  #{c['name']} — {members} members"
            if purpose:
                line += f" — {purpose}"
            lines.append(line)

    if private:
        if public:
            lines.append("")
        lines.append("Private:")
        for c in private:
            purpose = c.get("purpose", {}).get("value", "")
            members = c.get("num_members", 0)
            line = f"  🔒 #{c['name']} — {members} members"
            if purpose:
                line += f" — {purpose}"
            lines.append(line)

    message = "\n".join(lines)

    if max_length is None or len(message) <= max_length:
        return message

    # Count how many channels made it into the truncated message
    truncated_lines = [header, ""]
    shown = 0
    for line in lines[2:]:  # Skip header and blank line
        candidate = "\n".join(truncated_lines + [line])
        footer = f"\n\n(Showing {shown} of {total} channels — full list sent via DM)"
        if len(candidate + footer) > max_length:
            break
        truncated_lines.append(line)
        if line.startswith("  "):  # Actual channel lines start with indent
            shown += 1

    return "\n".join(truncated_lines) + f"\n\n(Showing {shown} of {total} channels — full list sent via DM)"
