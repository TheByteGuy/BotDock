# Installing BotDock on TrueNAS SCALE with YAML

About 5 minutes. You make one dataset for BotDock's data, then paste
[`truenas-compose.yaml`](truenas-compose.yaml) into TrueNAS. The image is prebuilt
(`ghcr.io/thebyteguy/botdock`), so there's nothing to build.

Works on TrueNAS SCALE 24.10 and newer, including a HexOS server. On HexOS,
[HexOS Custom Install](HEXOS.md) is even simpler, and shows BotDock as a normal HexOS app.

## 1. Make the dataset

A dataset is a folder on your pool that TrueNAS can snapshot and back up on its own.
BotDock keeps everything in it: your bots, their virtualenvs, logs, encrypted secrets and the
key that unlocks them.

1. Open the TrueNAS web UI and go to **Datasets**.
2. If you don't have an `apps` dataset yet (for keeping app data together):
   select your pool (the top row, e.g. `tank`), click **Add Dataset**, name it `apps`,
   leave **Dataset Preset** on **Generic**, and click **Save**.
3. Select the `apps` dataset and click **Add Dataset** again.
   - **Name:** `botdock`
   - **Dataset Preset:** **Apps** (gives the TrueNAS `apps` user, UID 568, access. BotDock
     runs as that user.)
   - Click **Save**. If TrueNAS asks about changing permissions, confirm.
4. Click the new `botdock` dataset. Its **Path** is shown in the details on the right, for
   example `/mnt/tank/apps/botdock`. Copy it; you need it in step 2.

Optional but worth it: **Data Protection → Periodic Snapshot Tasks → Add**, pick
`apps/botdock`, daily, keep 2 weeks. A bad bot update can then be rolled back.

> **On HexOS:** HexOS creates its own datasets for its locations. You can still add `botdock`
> under your pool as above, or use a folder inside your Applications location. Either way,
> copy the real `/mnt/...` path from TrueNAS → Datasets.

## 2. Install the app

1. Open [`truenas-compose.yaml`](truenas-compose.yaml) (on GitHub, click **Raw**) and copy it.
2. In TrueNAS go to **Apps → Discover Apps**, click the **⋮** menu at the top right, and choose
   **Install via YAML**.
3. **Name:** `botdock`. Paste the YAML, then change the three marked lines:
   - `/mnt/POOL/apps/botdock` → the path you copied (keep `:/data` on the end).
   - `TZ` → your timezone, e.g. `Europe/London`
     ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).
   - `BOTDOCK_PASSWORD` → a password of 4+ characters. Or delete that line, and BotDock gives
     you a one-time setup code in its log instead (**Apps → botdock → Logs**).
4. Click **Save**. TrueNAS downloads the image and starts it. Wait for **Running**.
5. Open `http://<your-nas-ip>:30480` and sign in.

The password is only used the first time. Change it later inside BotDock (**Account → Change
password**). After that, you can remove the line from the YAML (**Apps → botdock → Edit**).
There's no password reset, so keep it in a password manager.

## 3. Add a bot

1. **Add bot** → drop a `.py` file, a folder or a `.zip`, or pick a GitHub repo.
   A `requirements.txt` is installed into that bot's own virtualenv.
2. Open the bot → **Secrets**, and add what it needs, like `DISCORD_TOKEN`.
3. **Start**, and watch the **Console** tab.

## Updating BotDock

When a new version comes out, **Apps → botdock → Edit**, change the tag in
`ghcr.io/thebyteguy/botdock:0.1.2` to the new version, and **Save**. Your bots and secrets stay in
the dataset.

## If something goes wrong

| Problem | Fix |
|---|---|
| App stuck **Deploying**, log says `permission denied` on `/data` | The dataset path is wrong or doesn't exist. Check it in **Datasets** and edit the YAML |
| Install fails with `port is already allocated` | Something else uses 30480. Change `"30480:8080"` to e.g. `"30481:8080"` and open that port instead |
| Image download fails (`denied` or `not found`) | Check the image line is exactly `ghcr.io/thebyteguy/botdock:0.1.2` |
| Page won't load by a domain name or reverse proxy | Add `BOTDOCK_ALLOWED_HOSTS: "your.domain"` under `environment` |
| Log says the password was refused and asks for a setup code | The `BOTDOCK_PASSWORD` was too short or a common one (like the placeholder). Sign in with the setup code from the log or `setup-code.txt` in the dataset, and choose a password there |

## Backing up

Everything is in the `botdock` dataset. Snapshot or replicate it and you have every bot and
secret. Keep backups private: the dataset holds the key that decrypts your bots' secrets.

## Building the image yourself instead

[`truenas-compose-build.yaml`](truenas-compose-build.yaml) builds the image on the NAS from a copy
of this project, with no registry. See Option A in [HEXOS.md](HEXOS.md).
