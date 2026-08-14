# Deploying /mychannels to Cloud Run

Three things to set up: the Slack app, the GCP project, and the Cloud Run service. You'll go back and forth between Slack and GCP once (to wire up the slash command URL after deploy). Budget 20-30 minutes.

## Prerequisites

- `gcloud` CLI installed and authenticated
- Docker installed (or use Cloud Build — covered below)
- Admin access to your Slack workspace

## 1. Create the Slack App

Go to https://api.slack.com/apps and click **Create New App** > **From scratch**.

Name it whatever you want (e.g., "Channel List Bot"). Select your workspace.

### Bot Token Scopes

Go to **OAuth & Permissions** > **Scopes** > **Bot Token Scopes** and add:

- `commands`
- `users:read`
- `chat:write`

### User Token Scopes

In the same **OAuth & Permissions** page, under **User Token Scopes**, add:

- `channels:read`
- `groups:read`

These user scopes are what give the app visibility into private channels. The user token comes from whoever installs the app — this **must be a workspace admin** for full channel visibility.

### Install to Workspace

Click **Install to Workspace** at the top of the OAuth & Permissions page. Authorize both the bot and user permissions.

After installation, you'll see two tokens on the OAuth page:

- **Bot User OAuth Token** (`xoxb-...`) — this is `SLACK_BOT_TOKEN`
- **User OAuth Token** (`xoxp-...`) — this is `SLACK_USER_TOKEN`

Save both. You'll also need the **Signing Secret** from **Basic Information** > **App Credentials** — this is `SLACK_SIGNING_SECRET`.

### Create the Slash Command (partially — you'll finish after deploy)

Go to **Slash Commands** > **Create New Command**:

- Command: `/mychannels`
- Request URL: leave blank for now (you'll fill this in after deploying to Cloud Run)
- Short Description: "List your Slack channels"
- Usage Hint: `[@user]`

You can't save without a Request URL, so **skip this step for now** and come back after step 3.

## 2. Set Up GCP

Pick a GCP project (or create one). These commands assume you've set it:

```bash
export GCP_PROJECT=your-project-id
export GCP_REGION=us-central1  # or wherever you want

gcloud config set project $GCP_PROJECT
```

Enable the required APIs:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com
```

### Store Secrets (optional but recommended)

You can pass tokens as plain env vars to Cloud Run, but Secret Manager is cleaner:

```bash
echo -n "xoxb-your-bot-token" | \
  gcloud secrets create SLACK_BOT_TOKEN --data-file=-

echo -n "xoxp-your-user-token" | \
  gcloud secrets create SLACK_USER_TOKEN --data-file=-

echo -n "your-signing-secret" | \
  gcloud secrets create SLACK_SIGNING_SECRET --data-file=-
```

Grant the Cloud Run service account access:

```bash
PROJECT_NUMBER=$(gcloud projects describe $GCP_PROJECT --format='value(projectNumber)')

gcloud secrets add-iam-policy-binding SLACK_BOT_TOKEN \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding SLACK_USER_TOKEN \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding SLACK_SIGNING_SECRET \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## 3. Deploy to Cloud Run

### Option A: Build and deploy with Cloud Build (no local Docker needed)

```bash
gcloud run deploy mychannels \
  --source . \
  --region $GCP_REGION \
  --allow-unauthenticated \
  --set-secrets="SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,SLACK_USER_TOKEN=SLACK_USER_TOKEN:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest" \
  --min-instances=0 \
  --max-instances=1 \
  --memory=256Mi \
  --cpu=1 \
  --timeout=30
```

### Option B: Build locally and push

```bash
# Build
docker build -t gcr.io/$GCP_PROJECT/mychannels .

# Push
docker push gcr.io/$GCP_PROJECT/mychannels

# Deploy
gcloud run deploy mychannels \
  --image gcr.io/$GCP_PROJECT/mychannels \
  --region $GCP_REGION \
  --allow-unauthenticated \
  --set-secrets="SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,SLACK_USER_TOKEN=SLACK_USER_TOKEN:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest" \
  --min-instances=0 \
  --max-instances=1 \
  --memory=256Mi \
  --cpu=1 \
  --timeout=30
```

### Option C: Plain env vars (skip Secret Manager)

If you skipped Secret Manager, replace `--set-secrets` with:

```bash
  --set-env-vars="SLACK_BOT_TOKEN=xoxb-...,SLACK_USER_TOKEN=xoxp-...,SLACK_SIGNING_SECRET=..."
```

This works but means the tokens are visible in the Cloud Run console to anyone with project access.

### Get the URL

After deploy, `gcloud run deploy` prints the service URL. It looks like:

```
https://mychannels-xxxxxxxxxx-uc.a.run.app
```

Save this — you need it for the slash command.

## 4. Wire Up the Slash Command

Go back to your Slack app at https://api.slack.com/apps > your app > **Slash Commands**.

Create (or edit) the `/mychannels` command:

- **Request URL:** `https://mychannels-xxxxxxxxxx-uc.a.run.app/slack/events`

Note the `/slack/events` path — this is where Slack Bolt listens.

Save the command.

## 5. Verify

Open Slack and type `/mychannels` in any channel (your DM to yourself works great). You should see an ephemeral message listing your channels.

If it doesn't work:

```bash
# Check the logs
gcloud run services logs read mychannels --region $GCP_REGION --limit=20
```

Common issues:
- **"dispatch_failed"** — the Request URL is wrong or the service isn't running. Check the URL ends with `/slack/events`.
- **"invalid_auth"** — the bot token or signing secret is wrong. Check Secret Manager values.
- **Timeout** — Slack gives you 3 seconds to respond. If you have hundreds of channels, the pagination might take too long on a cold start. Set `--min-instances=1` if this is a problem (costs ~$5/month).

## Updating

After code changes:

```bash
# Option A (Cloud Build)
gcloud run deploy mychannels --source . --region $GCP_REGION

# Option B (local Docker)
docker build -t gcr.io/$GCP_PROJECT/mychannels . && \
docker push gcr.io/$GCP_PROJECT/mychannels && \
gcloud run deploy mychannels --image gcr.io/$GCP_PROJECT/mychannels --region $GCP_REGION
```

## Costs

This should cost effectively nothing. Cloud Run scales to zero, and this app will get maybe a handful of requests per month. You'd need thousands of daily invocations before you'd see a bill.

If you set `--min-instances=1` to avoid cold starts, expect ~$5-10/month for the always-on instance.

## Token Rotation

If the installing admin's account is deactivated or their token is revoked, the user token (`SLACK_USER_TOKEN`) will stop working. A different workspace admin will need to reinstall the app, and you'll need to update the secret:

```bash
echo -n "xoxp-new-user-token" | \
  gcloud secrets versions add SLACK_USER_TOKEN --data-file=-

# Redeploy to pick up the new secret version (or use :latest and restart)
gcloud run services update mychannels --region $GCP_REGION \
  --set-secrets="SLACK_USER_TOKEN=SLACK_USER_TOKEN:latest"
```
