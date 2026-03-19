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

1. User invokes `/mychannels` (optionally with `@user` argument — Slack encodes this as `<@USERID>` in the `text` field)
2. Slack POSTs to the Cloud Run service URL
3. App verifies the Slack request signature (handled by Slack Bolt)
4. If a target user is specified:
   a. Call `users.info` on the requesting user to check `is_admin`
   b. If not admin, respond with ephemeral error and stop
5. Call `users.conversations` with the target user's ID to fetch their channels (paginating if necessary)
6. Sort and format the channel list
7. Respond with an ephemeral message

### Slack API

**Token strategy: user token via OAuth**

A bot token with `groups:read` can only see private channels the bot has been invited to — which defeats the purpose. To get a complete channel list (including all private channels), the app uses a **user token** obtained via Slack OAuth.

When the self-service flow is used (`/mychannels` with no argument), the app can use the invoking user's own user token. For the admin lookup flow (`/mychannels @jane`), the app needs a token with admin-level visibility. The simplest approach: the Slack app is installed by a workspace admin, and the **installing admin's user token** (with `channels:read` and `groups:read` user scopes) is stored and used for all `users.conversations` calls. This token can see all channels for any user.

**Scopes required:**

- `commands` — register the slash command (bot scope)
- `channels:read` — list public channels (user scope)
- `groups:read` — list private channels (user scope)
- `users:read` — look up user info for admin check (bot scope)

**Endpoints used:**

- `users.conversations` — returns channels for a given user. Supports `user` parameter, `types` parameter (set to `public_channel,private_channel`). Paginated via `cursor`/`next_cursor`. Called with the admin **user token** to ensure full visibility.
- `users.info` — returns user profile including `is_admin` field. Called with the **bot token**.

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
- **Secrets:** Slack bot token (`SLACK_BOT_TOKEN`), admin user token (`SLACK_USER_TOKEN`), and signing secret (`SLACK_SIGNING_SECRET`) stored as Cloud Run environment variables or GCP Secret Manager
- **Slack app configuration:** Slash command URL pointed at the Cloud Run service URL + `/slack/events`

## Pagination

`users.conversations` returns a default of 100 channels per call (max 1000 with the `limit` parameter). The app must paginate using `cursor`/`next_cursor` to handle users in more than 200 channels. For very large lists, the Slack message response has a character limit (~3000 chars for ephemeral messages in blocks, ~40000 for plain text). If the list is too long for a single message, the app should truncate and note "showing N of M channels — full list sent via DM" and DM the complete list.

## Error handling

- **Invalid user:** If the `@user` argument doesn't resolve, respond with "Couldn't find that user."
- **Deactivated user:** `users.conversations` may not work for deactivated users. If it fails, respond with "That user's account is deactivated — their channel list is no longer available. This tool works best when run before deactivation."
- **Rate limiting:** Slack Bolt handles rate limit retries automatically.
- **Slack API errors:** Catch and respond with a generic "Something went wrong, please try again."
