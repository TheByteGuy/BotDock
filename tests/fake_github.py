"""A small fake of the GitHub REST API + device flow + codeload, as an ASGI app.

Used in-process by the tests (through httpx.ASGITransport) and runnable as a real HTTP server for
manual end-to-end runs:

    python tests/fake_github.py --port 18800 --seed

then start BotDock with BOTDOCK_GITHUB_API_URL=http://127.0.0.1:18800. Push a commit with
``curl -X POST localhost:18800/_admin/push -d '{"repo":"octo/ticketbot","files":{"main.py":"..."}}'``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import time
from typing import Callable
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

CODELOAD_TOKEN = "codeload-temp-credential"


class FakeGitHub:
    def __init__(self, codeload_base: str = "http://codeload.test", base_url: str = "http://github.test"):
        self.codeload_base = codeload_base.rstrip("/")
        self.base_url = base_url
        self.tokens: dict[str, dict] = {
            "good-token": {"login": "octo", "name": "Octo Cat", "avatar_url": "https://avatars.githubusercontent.com/u/1"},
        }
        self.client_ids = {"Iv1.testclient"}
        self.device_interval = 0
        self.device_script: list[str] = []  # per poll: pending | slow_down | expired | denied | ok
        self.device_polls = 0
        self.repos: dict[str, dict] = {}
        self.log: list[dict] = []  # every request: method, path, auth, status
        self.rate_limit_reset: float | None = None  # when set, API calls get 403 rate limited
        self.tar_hook: Callable[[tarfile.TarFile, str], None] | None = None
        self.corrupt_tarball = False
        self._n = 0
        self.app = Starlette(routes=[
            Route("/login/device/code", self.device_code, methods=["POST"]),
            Route("/login/oauth/access_token", self.access_token, methods=["POST"]),
            Route("/user", self.user),
            Route("/user/repos", self.user_repos),
            Route("/repos/{owner}/{repo}", self.repo),
            Route("/repos/{owner}/{repo}/branches", self.branches),
            Route("/repos/{owner}/{repo}/commits/{ref:path}", self.commit),
            Route("/repos/{owner}/{repo}/tarball/{ref:path}", self.tarball),
            Route("/{owner}/{repo}/legacy.tar.gz/{sha}", self.codeload),
            Route("/_admin/push", self.admin_push, methods=["POST"]),
        ])

    # state helpers ------------------------------------------------------------------
    def add_repo(self, full_name: str, files: dict[str, bytes | str], *, branch: str = "main", private: bool = False,
                 language: str | None = "Python", description: str | None = None, pushed_at: str | None = None,
                 extra_branches: tuple[str, ...] = ()) -> str:
        self.repos[full_name] = {
            "private": private, "default_branch": branch, "language": language, "description": description,
            "pushed_at": pushed_at or "2026-11-01T00:00:00Z", "branches": {},
        }
        sha = self._commit(full_name, branch, {k: _b(v) for k, v in files.items()}, "initial commit")
        if pushed_at:
            self.repos[full_name]["pushed_at"] = pushed_at
        for b in extra_branches:
            self.repos[full_name]["branches"][b] = list(self.repos[full_name]["branches"][branch])
        return sha

    def push(self, full_name: str, files: dict[str, bytes | str] | None = None, delete: tuple[str, ...] = (),
             branch: str = "main", message: str = "update") -> str:
        head = self.head(full_name, branch)
        tree = dict(head["files"])
        for k, v in (files or {}).items():
            tree[k] = _b(v)
        for k in delete:
            tree.pop(k, None)
        return self._commit(full_name, branch, tree, message)

    def head(self, full_name: str, branch: str = "main") -> dict:
        return self.repos[full_name]["branches"][branch][-1]

    def _commit(self, full_name: str, branch: str, tree: dict[str, bytes], message: str) -> str:
        self._n += 1
        sha = hashlib.sha1(f"{full_name}{branch}{self._n}{time.time()}".encode()).hexdigest()
        commit = {"sha": sha, "message": message, "date": f"2026-11-15T12:{self._n % 60:02d}:00Z", "files": tree}
        self.repos[full_name]["branches"].setdefault(branch, []).append(commit)
        self.repos[full_name]["pushed_at"] = f"2026-11-15T12:{self._n % 60:02d}:00Z"
        return sha

    def find_commit(self, full_name: str, ref: str) -> dict | None:
        repo = self.repos.get(full_name)
        if repo is None:
            return None
        if ref in repo["branches"]:
            return repo["branches"][ref][-1]
        for commits in repo["branches"].values():
            for c in commits:
                if c["sha"] == ref:
                    return c
        return None

    def api_calls(self, prefix: str = "") -> list[dict]:
        return [e for e in self.log if e["path"].startswith(prefix)]

    # plumbing -------------------------------------------------------------------------
    def _record(self, request: Request, status: int) -> None:
        self.log.append({"method": request.method, "path": request.url.path, "status": status,
                         "auth": request.headers.get("authorization"),
                         "if_none_match": request.headers.get("if-none-match")})

    def _json(self, request: Request, data, status: int = 200, headers: dict | None = None) -> Response:
        self._record(request, status)
        h = {"x-ratelimit-remaining": "4999", **(headers or {})}
        return JSONResponse(data, status_code=status, headers=h)

    def _auth(self, request: Request) -> tuple[dict | None, Response | None]:
        if self.rate_limit_reset is not None:
            return None, self._json(request, {"message": "API rate limit exceeded"}, 403, {
                "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(self.rate_limit_reset))})
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        user = self.tokens.get(token or "")
        if user is None:
            return None, self._json(request, {"message": "Bad credentials"}, 401)
        return user, None

    def _repo_or_404(self, request: Request):
        full = f"{request.path_params['owner']}/{request.path_params['repo']}"
        if full not in self.repos:
            return None, full, self._json(request, {"message": "Not Found"}, 404)
        return self.repos[full], full, None

    # device flow ----------------------------------------------------------------------
    async def device_code(self, request: Request):
        form = await request.form()
        if form.get("client_id") not in self.client_ids:
            return self._json(request, {"error": "incorrect_client_credentials"}, 400)
        return self._json(request, {"device_code": "dev-code-123", "user_code": "WDJB-MJHT",
                                    "verification_uri": f"{self.base_url}/login/device",
                                    "expires_in": 900, "interval": self.device_interval})

    async def access_token(self, request: Request):
        form = await request.form()
        self.device_polls += 1
        if form.get("device_code") != "dev-code-123":
            return self._json(request, {"error": "bad_verification_code"})
        step = self.device_script.pop(0) if self.device_script else "pending"
        if step == "ok":
            self.tokens["device-token-xyz"] = {"login": "octo", "name": "Octo Cat", "avatar_url": None}
            return self._json(request, {"access_token": "device-token-xyz", "token_type": "bearer", "scope": "repo"})
        errors = {"pending": "authorization_pending", "slow_down": "slow_down", "expired": "expired_token",
                  "denied": "access_denied"}
        body = {"error": errors[step]}
        if step == "slow_down":
            body["interval"] = self.device_interval
        return self._json(request, body)

    # API ------------------------------------------------------------------------------
    async def user(self, request: Request):
        user, err = self._auth(request)
        return err or self._json(request, {"login": user["login"], "name": user["name"],
                                           "avatar_url": user["avatar_url"], "id": 1})

    async def user_repos(self, request: Request):
        user, err = self._auth(request)
        if err:
            return err
        per_page = int(request.query_params.get("per_page", 30))
        page = int(request.query_params.get("page", 1))
        items = sorted(self.repos.items(), key=lambda kv: kv[1]["pushed_at"], reverse=True)
        chunk = items[(page - 1) * per_page: page * per_page]
        return self._json(request, [{
            "full_name": full, "name": full.split("/")[1], "owner": {"login": full.split("/")[0]},
            "private": r["private"], "default_branch": r["default_branch"], "description": r["description"],
            "language": r["language"], "pushed_at": r["pushed_at"], "html_url": f"{self.base_url}/{full}",
        } for full, r in chunk])

    async def repo(self, request: Request):
        _, err = self._auth(request)
        if err:
            return err
        r, full, err = self._repo_or_404(request)
        return err or self._json(request, {"full_name": full, "default_branch": r["default_branch"],
                                           "private": r["private"]})

    async def branches(self, request: Request):
        _, err = self._auth(request)
        if err:
            return err
        r, full, err = self._repo_or_404(request)
        if err:
            return err
        per_page = int(request.query_params.get("per_page", 30))
        page = int(request.query_params.get("page", 1))
        names = list(r["branches"])[(page - 1) * per_page: page * per_page]
        return self._json(request, [{"name": n} for n in names])

    async def commit(self, request: Request):
        _, err = self._auth(request)
        if err:
            return err
        r, full, err = self._repo_or_404(request)
        if err:
            return err
        c = self.find_commit(full, unquote(request.path_params["ref"]))
        if c is None:
            return self._json(request, {"message": "No commit found"}, 404)
        etag = f'W/"{c["sha"]}"'
        if request.headers.get("if-none-match") == etag:
            self._record(request, 304)
            return Response(status_code=304, headers={"etag": etag})
        return self._json(request, {"sha": c["sha"], "commit": {
            "message": c["message"] + "\n\nlong body", "committer": {"date": c["date"]}}}, headers={"etag": etag})

    async def tarball(self, request: Request):
        _, err = self._auth(request)
        if err:
            return err
        r, full, err = self._repo_or_404(request)
        if err:
            return err
        c = self.find_commit(full, unquote(request.path_params["ref"]))
        if c is None:
            return self._json(request, {"message": "Not Found"}, 404)
        self._record(request, 302)
        loc = f"{self.codeload_base}/{full}/legacy.tar.gz/{c['sha']}?token={CODELOAD_TOKEN}"
        return Response(status_code=302, headers={"location": loc})

    async def codeload(self, request: Request):
        if request.headers.get("authorization"):  # GitHub's token must not be forwarded here
            self._record(request, 400)
            return Response("Authorization header must not be sent to codeload", status_code=400)
        if request.query_params.get("token") != CODELOAD_TOKEN:
            self._record(request, 404)
            return Response(status_code=404)
        full = f"{request.path_params['owner']}/{request.path_params['repo']}"
        c = self.find_commit(full, request.path_params["sha"])
        if c is None:
            self._record(request, 404)
            return Response(status_code=404)
        self._record(request, 200)
        if self.corrupt_tarball:
            return Response(b"\x1f\x8b\x08\x00garbage-not-a-tarball", media_type="application/x-gzip")
        top = f"{full.replace('/', '-')}-{c['sha'][:7]}"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo(top)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            tf.addfile(ti)
            dirs = set()
            for path, data in sorted(c["files"].items()):
                parts = path.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if d not in dirs:
                        dirs.add(d)
                        di = tarfile.TarInfo(f"{top}/{d}")
                        di.type = tarfile.DIRTYPE
                        di.mode = 0o755
                        tf.addfile(di)
                fi = tarfile.TarInfo(f"{top}/{path}")
                fi.size = len(data)
                fi.mode = 0o644
                tf.addfile(fi, io.BytesIO(data))
            if self.tar_hook:
                self.tar_hook(tf, top)
        return Response(gzip.compress(buf.getvalue()), media_type="application/x-gzip")

    async def admin_push(self, request: Request):
        body = json.loads(await request.body())
        sha = self.push(body["repo"], body.get("files"), tuple(body.get("delete", ())),
                        body.get("branch", "main"), body.get("message", "update"))
        return JSONResponse({"sha": sha})


def _b(v: bytes | str) -> bytes:
    return v if isinstance(v, bytes) else v.encode("utf-8")


TICKET_BOT = '''import json, os, time
os.makedirs("data", exist_ok=True)
state = json.load(open("data/tickets.json"))
state["runs"] = state.get("runs", 0) + 1
json.dump(state, open("data/tickets.json", "w"))
print("VERSION", open("version.txt").read().strip(), "runs", state["runs"], flush=True)
while True:
    time.sleep(1)
'''


def seed(fake: FakeGitHub) -> None:
    fake.add_repo("octo/ticketbot", {"main.py": TICKET_BOT, "version.txt": "1\n",
                                     "data/tickets.json": json.dumps({"runs": 0, "tickets": []})},
                  private=True, description="Ticket bot", extra_branches=("dev",))
    fake.add_repo("octo/other", {"bot.py": "print('other')\n"}, language="JavaScript")


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18800)
    ap.add_argument("--codeload", default=None, help="base URL for tarball redirects (use a different host)")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()
    fake = FakeGitHub(codeload_base=args.codeload or f"http://localhost:{args.port}",
                      base_url=f"http://127.0.0.1:{args.port}")
    if args.seed:
        seed(fake)
    uvicorn.run(fake.app, host="127.0.0.1", port=args.port, log_level="warning")
