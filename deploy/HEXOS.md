# Installing BotDock on HexOS

Everything BotDock keeps (bots, their virtualenvs, logs, encrypted secrets and the key that
decrypts them) lives in one `data` folder. Back that folder up and you have everything.

## Easiest — HexOS Custom Install (about 2 minutes)

The image is built by GitHub and published as `ghcr.io/thebyteguy/botdock`, so the NAS just
downloads it. Nothing to build, no shell.

1. In HexOS, turn on **Expert Mode** (in your preferences).
2. Go to **Apps** and choose **Custom Install**. A JSON editor opens.
3. Replace its contents with [`botdock.json`](botdock.json) (open it on GitHub, click **Raw**,
   copy everything).
4. Install. HexOS asks for an **Admin password** (a random one is filled in; change it to one you
   will remember, 8+ characters).
5. When it shows **Running**, click **Launch** and sign in with that password.

It stores everything under Applications Performance → `botdock/data` and publishes port 30480
(HexOS moves it if that port is taken; Launch follows it).

**Updating:** when a new version is tagged, raise `tag` (and `script.version`) in the script to
match and run Custom Install again with it. Your bots and secrets in `data` are kept.

## Option A — Install via YAML (TrueNAS UI)

To install with the prebuilt image, follow [TRUENAS.md](TRUENAS.md). The steps below build the
image on the NAS instead, with [`truenas-compose-build.yaml`](truenas-compose-build.yaml).

1. On your PC run `powershell -ExecutionPolicy Bypass -File deploy/make-bundle.ps1`. It writes `dist/botdock-app.zip`.
2. On a NAS share, make `apps/botdock/app` and `apps/botdock/data`. Unzip the bundle into `app`
   (so `app/Dockerfile` exists).
3. Find the folder's real path on the NAS: TrueNAS UI → Datasets. It looks like
   `/mnt/<pool>/<share>/apps/botdock`.
4. TrueNAS UI → Apps → Discover Apps → ⋮ → **Install via YAML**. Name it `botdock`, paste
   `deploy/truenas-compose-build.yaml`, and replace the two `/mnt/POOL/apps/botdock` paths and the
   timezone. Optionally uncomment `BOTDOCK_PASSWORD` and set a password of 8+ characters.
5. Save. The first start builds the image (a few minutes). Then open `http://<nas-ip>:30480`.

**Updating BotDock:** unzip a new bundle over `app`, then restart the app. It rebuilds on start.
Your bots and secrets are in `data` and are not touched.

## Option B — HexOS install script with an image you built yourself

For a changed copy of BotDock that isn't on GitHub's registry:

1. Copy the bundle to the NAS as in Option A, steps 1–3.
2. TrueNAS UI → System → Shell:
   `sudo docker build -t botdock:0.1.0 /mnt/<pool>/<share>/apps/botdock/app`
3. In `botdock.json`, set `app_values.image` to
   `{ "repository": "botdock", "tag": "0.1.0", "pull_policy": "never" }`, then install it with
   Custom Install as above.

**Updating:** rebuild with the same command, then stop and start the app.

## First use

1. Open the web UI by the NAS's IP address. If you preset a password, sign in with it. Otherwise
   BotDock asks you to choose one and wants a one-time setup code: it is printed in the app's log
   (Apps → botdock → Logs) and is also in `setup-code.txt` in the `data` folder.
2. **Add bot** → drop a `.py` file, a folder, or a `.zip`. A `requirements.txt` in the folder is
   installed into that bot's own virtualenv. A `.env` in the folder is imported into the bot's
   Secrets and not stored as a file.
3. Open the bot → **Secrets** and add what it needs (e.g. `DISCORD_TOKEN`). They are passed to the
   bot as environment variables.
4. **Start**. Watch the Console tab.

Bots run with their own folder as the working directory, so files they write (like `data/*.json`)
persist. Updating a bot's code in the default *merge* mode keeps those files.

## GitHub

Account → GitHub. The recommended way is a fine-grained personal access token limited to your bot
repos with Contents: Read-only. Pick a repo when adding a bot, or link an existing bot from its
Settings. With Auto-update on, BotDock checks GitHub every couple of minutes, pulls new commits and
restarts the bot. Put files the bot rewrites while running (`data/**`, `config.py`) in the bot's
"Never overwrite" list. Linking an existing bot changes nothing until you press Pull.

## Notes

- All bots run as the same user inside the container. Any bot (or a bad pip package it installs)
  can read the other bots' secrets and the stored GitHub token. Only run code you trust, and keep
  the GitHub token read-only.
- BotDock only answers to IP addresses, `localhost`, plain names like `nas`, and names ending in
  `.local`, `.lan`, `.home`, `.internal`. To use another name (your own domain, a reverse proxy),
  set `BOTDOCK_ALLOWED_HOSTS=name1,name2`.

- The container starts as root only to fix ownership of `/data`, then runs BotDock and all bots as
  UID/GID 568 (TrueNAS `apps`). Change with `PUID` / `PGID`.
- Anyone who can sign in to BotDock can run code on the server inside the container. Keep it on
  your LAN or behind a VPN; do not port-forward it.
- System packages some bots need (beyond `git`, `ffmpeg`, `libopus`) can be baked in:
  add `args: { EXTRA_APT: "pkg1 pkg2" }` under `build:` in the YAML.
