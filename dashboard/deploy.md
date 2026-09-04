# Deploying the dashboard without the click-through

`clasp` (Google's official Apps Script CLI) is installed in this repo, so updates
can be pushed from the terminal instead of pasting code and walking the Deploy
menu. Two one-time steps are needed first — both require your Google account, so
they're yours to run.

## One-time setup

**1. Turn on the Apps Script API** (a single toggle, needed for any CLI access):
https://script.google.com/home/usersettings → switch **Google Apps Script API** to ON.

**2. Log in** — this opens a browser window for you to approve:

```bash
npx clasp login
```

Credentials land in `~/.clasprc.json`, outside the repo (and gitignored anyway).

**3. Tell me when that's done.** I'll find the script ID with `npx clasp list`,
wire up `.clasp.json`, and push. After that every future change is one command.

## Afterwards

```bash
npx clasp push -f && npx clasp deploy -i <DEPLOYMENT_ID>
```

`deploy -i` updates the *existing* deployment, so your web-app URL never changes.

## Why not just automate the browser?

The Claude-in-Chrome extension isn't connected, and the in-app browser has no
access to your Google session — so there's no way to click through the Apps
Script UI on your behalf. clasp is the supported route and is better anyway:
no copy-paste, and the deployed code always matches what's in git.
