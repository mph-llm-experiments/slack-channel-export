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
