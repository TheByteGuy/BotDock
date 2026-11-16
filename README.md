# BotDock

Host many Python bots (mostly Discord bots) in one Docker container, and manage them from a web page.

- Upload a `.py` file, a folder or a `.zip`, or pick a GitHub repo.
- Each bot gets its own virtualenv. Its `requirements.txt` is installed automatically.
- Secrets are stored encrypted and passed to the bot as environment variables. A `.env` in an upload is imported as secrets, not kept as a file.
- Start, stop and restart bots, watch their live console and browse their files.
- Crashed bots restart with backoff.
- Linked GitHub repos can auto-update: BotDock pulls new commits and restarts the bot.

Built for HexOS / TrueNAS SCALE as a custom app, and also runs anywhere Docker does. The web UI has no build step and loads nothing from a CDN, so it works on an offline server.

## Run it

With Docker:

```sh
docker build -t botdock:0.1.0 .
docker run -d --name botdock -p 30480:8080 -v botdock-data:/data \
  -e BOTDOCK_PASSWORD='choose-a-password' botdock:0.1.0
```

Then open `http://<host>:30480`.

Without Docker (Python 3.12):

```sh
pip install -r requirements.txt
python -m botdock
```

On HexOS or TrueNAS, see [deploy/HEXOS.md](deploy/HEXOS.md).

## First sign-in

If you set `BOTDOCK_PASSWORD`, sign in with it. Otherwise BotDock asks you to choose a password and wants a one-time setup code. The code is printed in the container log and saved to `setup-code.txt` in the data folder.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BOTDOCK_DATA` | `./data` (`/data` in the image) | Where everything is stored |
| `BOTDOCK_HOST` / `BOTDOCK_PORT` | `0.0.0.0` / `8080` | Listen address |
| `BOTDOCK_PASSWORD` | unset | Sets the admin password on first start |
| `BOTDOCK_SECRET_KEY` | unset | Fernet key for secrets. Generated into the data folder if unset |
| `BOTDOCK_COOKIE_SECURE` | `0` | Mark the session cookie `Secure` (set it behind HTTPS) |
| `BOTDOCK_GITHUB_CLIENT_ID` | unset | GitHub OAuth app for device sign-in. A fine-grained token works without it |

Everything BotDock keeps (bots, virtualenvs, logs, encrypted secrets and the key that decrypts them) lives in the data folder. Back it up and you have everything. Keep it private: it holds the key to your bots' secrets.

## Development

```sh
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`dev/mock_server.py` serves the web UI with fake bots and a fake GitHub, for working on the frontend. [SPEC.md](SPEC.md) describes the API and how the backend and frontend fit together.
