# slack-channel-list Design Spec

## Problem

When users go on leave, their Slack accounts are deactivated per company policy. At the current Slack tier, deactivation strips all channel memberships. On reactivation, users lose their entire channel list and must manually rejoin from memory. The current workaround — "take a screenshot" — is inadequate.

## Solution

A Slack slash command (`/mychannels`) that dumps a user's channel list with metadata, so they can save it before leave or IT can capture it on their behalf.

## User Experience

### Self-service

A user types `/mychannels` in any channel (including their own DM-to-self, which persists through deactivation). They receive an ephemeral response listing all their channels with name, type, member count, and description.

### IT/admin use

A workspace admin types `/mychannels @jane` to get the channel list for another user. This covers the case where IT needs to capture the list for a leave ticket. Non-admin users who try to look up someone else get an ephemeral error.

## Architecture

Single Python application deployed as a container on GCP Cloud Run. One HTTP endpoint handles the Slack slash command.

```
User ──► Slack ──► Cloud Run (slash command handler) ──► Slack API
                                                          │
                                              users.conversations
                                              users.info (admin check)
```

### Components

- **`app.py`** — Slack Bolt application with the `/mychannels` slash command handler
- **`Dockerfile`** — Python container for Cloud Run deployment
- **`requirements.txt`** — Dependencies: `slack-bolt`, `gunicorn`

### Request flow

1. User invokes `/mychannels` (optionally with `@user` argument)
2. Slack POSTs to the Cloud Run service URL
3. App verifies the Slack request signature (handled by Slack Bolt)
4. If a target user is specified:
   a. Call `users.info` on the requesting user to check `is_admin`
   b. If not admin, respond with ephemeral error and stop
5. Call `users.conversations` with the target user's ID to fetch their channels (paginating if necessary)
6. Sort and format the channel list
7. Respond with an ephemeral message

### Slack API

**Scopes required (bot token):**

- `commands` — register the slash command
- `channels:read` — list public channels the user is in
- `groups:read` — list private channels the user is in
- `users:read` — look up user info for admin check

**Endpoints used:**

- `users.conversations` — returns channels for a given user. Supports `user` parameter, `types` parameter (set to `public_channel,private_channel`). Paginated; must follow `next_cursor`.
- `users.info` — returns user profile including `is_admin` field.

### Response format

Ephemeral message (visible only to the person who ran the command):

```
📋 Channels for @jane (47 channels)

Public:
  #engineering — 234 members — Main engineering discussion
  #incidents — 89 members — Incident response coordination
  ...

Private:
  🔒 #eng-leads — 12 members — Engineering leadership
  🔒 #proj-atlas — 6 members — Project Atlas working group
  ...
```

Channels sorted alphabetically within each section.

### Permission model

| Scenario | Behavior |
|----------|----------|
| `/mychannels` (no argument) | Returns the invoking user's channels |
| `/mychannels @jane` by admin | Returns @jane's channels |
| `/mychannels @jane` by non-admin | Ephemeral error: "Only workspace admins can look up other users' channels." |

Admin status is determined by the `is_admin` field from `users.info`.

## Deployment

- **Platform:** GCP Cloud Run (scales to zero)
- **Container:** Python 3.12 + gunicorn + slack-bolt
- **Secrets:** Slack bot token (`SLACK_BOT_TOKEN`) and signing secret (`SLACK_SIGNING_SECRET`) stored as Cloud Run environment variables or GCP Secret Manager
- **Slack app configuration:** Slash command URL pointed at the Cloud Run service URL + `/slack/events`

## Pagination

`users.conversations` returns a max of 200 channels per call. The app must paginate using `cursor`/`next_cursor` to handle users in more than 200 channels. For very large lists, the Slack message response has a character limit (~3000 chars for ephemeral messages in blocks, ~40000 for plain text). If the list is too long for a single message, the app should truncate and note "showing N of M channels — full list sent via DM" and DM the complete list.

## Error handling

- **Invalid user:** If the `@user` argument doesn't resolve, respond with "Couldn't find that user."
- **Deactivated user:** `users.conversations` may not work for deactivated users. If it fails, respond with "That user's account is deactivated — their channel list is no longer available. This tool works best when run before deactivation."
- **Rate limiting:** Slack Bolt handles rate limit retries automatically.
- **Slack API errors:** Catch and respond with a generic "Something went wrong, please try again."
