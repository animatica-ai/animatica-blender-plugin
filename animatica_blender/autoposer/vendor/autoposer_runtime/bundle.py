"""Finding the model — which the addon does NOT ship.

`ik.onnx` is pure geometry and could ship anywhere; `poser.onnx` is the checkpoint, and it is
account-gated. So the package carries inference code only and the bundle is fetched once, on
first use, into a cache the user can delete.

Resolution order, first hit wins:

1. an explicit path passed by the caller;
2. `$ANIMATICA_AUTOPOSER_BUNDLE` — a local bundle directory (how you run an unreleased
   checkpoint, and how this repo tests the shipped code against a fresh export);
3. the cache, if it already holds a complete, hash-verified bundle;
4. a download from the Animatica API, using the account's bearer token.

THE DOWNLOAD CONTRACT (what the API must serve):

    GET  {api_base}/autoposer/bundle?format=1
    Authorization: Bearer <animatica token>

    200 {"version": "2026.09.1",
         "files": {"meta.json":  {"url": "...", "sha256": "...", "bytes": 3411},
                   "poser.onnx": {"url": "...", "sha256": "...", "bytes": 148412345},
                   "ik.onnx":    {"url": "...", "sha256": "...", "bytes": 3502011}}}

URLs may be absolute or relative to `api_base`, and may be pre-signed and short-lived — they
are fetched immediately. Every file is verified against its `sha256` before anything is moved
into place, and the move is atomic per bundle version, so an interrupted download leaves the
previous bundle intact and never a half-written one.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FILES = ("meta.json", "poser.onnx", "ik.onnx")
DEFAULT_API_BASE = "https://api.animatica.ai"
ENV_BUNDLE = "ANIMATICA_AUTOPOSER_BUNDLE"
ENV_TOKEN = "ANIMATICA_TOKEN"
ENV_API = "ANIMATICA_API_BASE"


class BundleError(RuntimeError):
    """Raised with something a user can act on: what was looked for, and where."""


class Bundle:
    """A resolved bundle directory: two graphs and the metadata that describes them."""

    def __init__(self, path):
        self.path = Path(path)
        meta_file = self.path / "meta.json"
        if not meta_file.exists():
            raise BundleError(f"no meta.json in {self.path}")
        self.meta = json.loads(meta_file.read_text())
        for name in ("poser.onnx", "ik.onnx"):
            if not (self.path / name).exists():
                raise BundleError(f"{name} missing from {self.path}")

    @property
    def poser(self) -> Path:
        return self.path / "poser.onnx"

    @property
    def ik(self) -> Path:
        return self.path / "ik.onnx"

    @property
    def version(self) -> str:
        return str(self.meta.get("package_version", "?"))

    def verify(self, expected: dict | None = None) -> bool:
        """Check the two graphs against the hashes `meta.json` records — and against
        `expected` ({name: sha256}, the hashes the addon was built with), when given.

        Fails closed: a meta.json that records no hash for a graph does not verify, because
        "nothing to check against" is not the same as "checked".
        """
        files = self.meta.get("files")
        if not isinstance(files, dict):
            return False
        for name in ("poser.onnx", "ik.onnx"):
            want = (files.get(name) or {}).get("sha256")
            if not want:
                return False
            if expected and expected.get(name) and expected[name] != want:
                return False
        for name, info in files.items():
            f = self.path / name
            if not f.exists() or sha256(f) != (info or {}).get("sha256"):
                return False
        return True


def sha256(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_cache_dir() -> Path:
    """Where a downloaded bundle lives when the caller names no directory.

    A Blender addon should pass its OWN directory instead, so uninstalling the addon takes
    the model with it.
    """
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "Animatica" / "autoposer"


def local_bundle(path=None) -> Bundle | None:
    """A bundle the caller or the environment points at, or None."""
    p = path or os.environ.get(ENV_BUNDLE) or None
    if not p:
        return None
    b = Bundle(p)                      # an explicit path that is wrong should SAY so
    return b


def cached_bundle(cache_dir=None, *, verify: bool = True) -> Bundle | None:
    """The cached bundle, if it is there — and, by default, if it hashes to what it claims.

    `verify=False` for anything on a UI path. Verifying means reading ~150 MB through sha256,
    which is right after a download and at load time and NOWHERE ELSE: this function was being
    called from a panel's `draw()`, so Blender hashed the model on every redraw and sat at 100%
    of a core. The main loop then turned about once a second and the whole application — not
    just this addon — felt broken.
    """
    d = Path(cache_dir or default_cache_dir()) / "current"
    if not (d / "meta.json").exists():
        return None
    try:
        b = Bundle(d)
    except BundleError:
        return None
    return b if (not verify or b.verify()) else None


def _get(url: str, token: str | None, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "animatica-autoposer-runtime")
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def _download(url: str, token: str | None, dest: Path, expect_sha: str | None,
              on_progress=None, timeout: float = 60.0, opener=None) -> Path:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "animatica-autoposer-runtime")
    h = hashlib.sha256()
    open_url = (opener or _OPENER).open
    with open_url(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with dest.open("wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(dest.name, done, total)
    if not expect_sha or h.hexdigest() != expect_sha:
        # no published hash is a refusal too: an unverifiable file is not installed
        dest.unlink(missing_ok=True)
        raise BundleError(f"{dest.name}: downloaded bytes do not match the published sha256")
    return dest


def download(cache_dir=None, *, api_base=None, token=None, on_progress=None) -> Bundle:
    """Fetch the bundle for this account and install it into the cache.

    Everything lands in a temporary directory first and is hash-checked there; only then does
    it replace `current`. An interrupted or corrupted download therefore leaves whatever was
    already installed working.
    """
    base = (api_base or os.environ.get(ENV_API) or DEFAULT_API_BASE).rstrip("/")
    token = token or os.environ.get(ENV_TOKEN) or None
    if not token:
        raise BundleError(
            "no Animatica token: the autoposer model is account-gated. Sign in to Animatica "
            f"(or set ${ENV_TOKEN}), or point ${ENV_BUNDLE} at a local bundle directory.")
    try:
        manifest = json.loads(_get(f"{base}/autoposer/bundle?format=1", token))
    except urllib.error.HTTPError as e:
        detail = "sign in again" if e.code in (401, 403) else f"HTTP {e.code}"
        raise BundleError(f"could not ask {base} for the autoposer model ({detail})") from e
    except urllib.error.URLError as e:
        raise BundleError(f"could not reach {base}: {e.reason}") from e

    files = manifest.get("files") or {}
    missing = [n for n in FILES if not (files.get(n) or {}).get("sha256")]
    if missing:
        raise BundleError(f"the server's bundle manifest is missing {', '.join(missing)} "
                          "(or its sha256)")

    root = Path(cache_dir or default_cache_dir())
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="bundle-", dir=str(root)))
    try:
        for name in FILES:
            info = files[name]
            url = info["url"]
            if not urllib.parse.urlsplit(url).scheme:
                # a path, not a URL: relative to the API. Anything CARRYING a scheme is
                # taken as-is -- joining it onto the base would send a mangled request
                # somewhere real.
                url = f"{base}/{url.lstrip('/')}"
            # The token is for the API. An absolute URL elsewhere (a pre-signed object-store
            # link) carries its own credentials and must not be handed ours.
            file_token = token if _origin(url) == _origin(base) else None
            _download(url, file_token, staging / name, info.get("sha256"), on_progress)
        bundle = Bundle(staging)
        if not bundle.verify():
            raise BundleError("the downloaded bundle does not match its own meta.json")
        _install(staging, root)
        return Bundle(root / "current")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- Hugging Face
HF_ENDPOINT = "https://huggingface.co"
HF_SUBFOLDER = "onnx"

#: The model this build of the addon uses: one Hugging Face commit, and the sha256 of every file
#: at it. Pinned, not `main`, so a push to the repo cannot change what an installed addon
#: downloads, and the hashes live HERE rather than only in the repo's own meta.json, so the
#: repo cannot vouch for itself. Bump the three together (see `pinned_hashes`).
HF_REPO = "Animatica-ai/autoposer"
HF_REVISION = "792cc2f0f2ca210b8bb5eaab781c6b0f388d458b"
HF_PINNED = {
    (HF_REPO, HF_SUBFOLDER, HF_REVISION): {
        "meta.json": "2bfc78f04767a6fddad2aee684d67ab76d25d4392dde66d5d2be5e61cc496b68",
        "poser.onnx": "d75271f71d848e374f53909aee547e90438170e48ae509a9167fa99ea1ede0ed",
        "ik.onnx": "0ff3815ad7ccaf137302946a5c947b8795c04340174a3578d028bfbd77ea724e",
    },
}


def pinned_hashes(repo_id: str, subfolder: str, revision: str) -> dict | None:
    """The hashes this build expects for `repo_id/subfolder@revision`, or None if unpinned."""
    return HF_PINNED.get((repo_id, subfolder or HF_SUBFOLDER, revision))
ENV_HF = "ANIMATICA_AUTOPOSER_HF"
ENV_HF_TOKEN = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Carry the token to huggingface.co, and to nowhere else.

    An LFS file redirects to a CDN with the credentials already in the query string, and object
    stores reject a request that ALSO carries an Authorization header ("only one auth mechanism
    allowed"). Stripping it across a host change is both the fix and the right thing to do with
    somebody's token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            # scheme too: an https -> http hop on the same host would put the token on the
            # wire in the clear
            new.headers = {k: v for k, v in new.headers.items()
                           if k.lower() != "authorization"}
            new.unredirected_hdrs = {k: v for k, v in new.unredirected_hdrs.items()
                                     if k.lower() != "authorization"}
        return new


def _origin(url: str):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


#: Every request that may carry a token goes through this: urllib's default handler forwards
#: `Authorization` to wherever a redirect points.
_OPENER = urllib.request.build_opener(_DropAuthOnRedirect)


def hf_token(explicit=None) -> str | None:
    """A token from the caller, the environment, or the huggingface-cli login file.

    `download_hf` sends only the caller's token up front; the ambient ones (environment, login
    file) are tried only after the repo has refused an anonymous request, so a public repo
    never sees a token the user did not hand to this addon.
    """
    if explicit:
        return str(explicit).strip()
    for var in ENV_HF_TOKEN:
        if os.environ.get(var):
            return os.environ[var].strip()
    for p in (Path.home() / ".cache" / "huggingface" / "token",
              Path.home() / ".huggingface" / "token"):
        try:
            if p.exists():
                t = p.read_text().strip()
                if t:
                    return t
        except OSError:
            pass
    return None


def hf_url(repo_id: str, filename: str, revision: str = "main",
           subfolder: str = HF_SUBFOLDER, endpoint: str = HF_ENDPOINT) -> str:
    path = f"{subfolder}/{filename}" if subfolder else filename
    return f"{endpoint.rstrip('/')}/{repo_id}/resolve/{revision}/{path}"


def parse_hf(source: str):
    """`hf://Animatica-ai/autoposer[/subfolder][@revision]` -> (repo, subfolder, revision).

    No `@revision` means the revision this build is pinned to, not `main`.
    """
    s = str(source)
    if s.startswith("hf://"):
        s = s[len("hf://"):]
    rev = HF_REVISION
    if "@" in s:
        s, rev = s.rsplit("@", 1)
    parts = [p for p in s.strip("/").split("/") if p]
    if len(parts) < 2:
        raise BundleError(f"{source!r} is not a Hugging Face repo id (owner/name)")
    repo = "/".join(parts[:2])
    sub = "/".join(parts[2:]) or HF_SUBFOLDER
    return repo, sub, rev


def download_hf(repo_id: str, cache_dir=None, *, revision: str = HF_REVISION,
                subfolder: str = HF_SUBFOLDER, token=None, endpoint: str = HF_ENDPOINT,
                on_progress=None) -> Bundle:
    """Fetch the bundle from a Hugging Face repo — private ones included, with a token.

    `meta.json` comes first and carries the sha256 of the two graphs, so the repo needs no
    manifest of its own: the model describes itself, and every byte is checked against that
    description before anything is installed. For the revision this build is pinned to, every
    file — meta.json included — must ALSO match the hashes embedded in the addon.
    """
    tok = str(token).strip() if token else None
    pinned = pinned_hashes(repo_id, subfolder, revision) if endpoint == HF_ENDPOINT else None
    root = Path(cache_dir or default_cache_dir())
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="bundle-", dir=str(root)))
    try:
        for name in FILES:
            url = hf_url(repo_id, name, revision, subfolder, endpoint)
            if pinned:
                expect = pinned[name]
            elif name == "meta.json":
                expect = None
            else:
                meta = json.loads((staging / "meta.json").read_text())
                expect = (meta.get("files", {}).get(name) or {}).get("sha256")
                if not expect:
                    raise BundleError(f"{repo_id}'s meta.json records no sha256 for {name}; "
                                      "refusing to install an unverifiable model")
            try:
                try:
                    if name == "meta.json" and not pinned:
                        _download_unverified(url, tok, staging / name, on_progress)
                    else:
                        _download(url, tok, staging / name, expect, on_progress)
                except urllib.error.HTTPError as e:
                    # A gated repo: only now is a token the user set up for Hugging Face
                    # itself ($HF_TOKEN, huggingface-cli login) worth offering.
                    ambient = hf_token() if (tok is None and e.code in (401, 403)) else None
                    if not ambient:
                        raise
                    tok = ambient
                    if name == "meta.json" and not pinned:
                        _download_unverified(url, tok, staging / name, on_progress)
                    else:
                        _download(url, tok, staging / name, expect, on_progress)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise BundleError(
                        f"{repo_id} is private or gated. Paste a Hugging Face access token "
                        "with read permission into the addon preferences (or set $HF_TOKEN); "
                        "you can make one at huggingface.co/settings/tokens.") from e
                if e.code == 404:
                    raise BundleError(
                        f"{endpoint}/{repo_id} has no {subfolder}/{name} at revision "
                        f"{revision!r}") from e
                raise BundleError(f"could not fetch {name} from {repo_id}: HTTP {e.code}") from e
            except urllib.error.URLError as e:
                raise BundleError(f"could not reach {endpoint}: {e.reason}") from e
        bundle = Bundle(staging)
        if not bundle.verify(pinned):
            raise BundleError("the downloaded bundle does not match its own meta.json")
        _install(staging, root)
        return Bundle(root / "current")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _download_unverified(url, token, dest: Path, on_progress=None) -> Path:
    """meta.json of an UNPINNED revision: it is what carries the hashes, so nothing precedes
    it to check it against. Everything it describes is then checked against it, and a bundle
    whose meta.json records no hashes is refused."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "animatica-autoposer-runtime")
    with _OPENER.open(req, timeout=60.0) as r:
        dest.write_bytes(r.read())
    return dest


def _install(staging: Path, root: Path):
    """Swap a verified staging directory in as `current`, atomically."""
    current, previous = root / "current", root / "previous"
    shutil.rmtree(previous, ignore_errors=True)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    shutil.rmtree(previous, ignore_errors=True)


def resolve(source=None, *, cache_dir=None, api_base=None, token=None,
            allow_download: bool = True, on_progress=None) -> Bundle:
    """The one entry point: give me a bundle, or tell me plainly why you cannot.

    `source` may be a directory, or `hf://owner/repo[/subfolder][@revision]`. With neither,
    the cache is used if it holds a verified bundle, and otherwise the Animatica API (or
    `$ANIMATICA_AUTOPOSER_HF`, if that names a Hugging Face repo) is asked for one.
    """
    src = source or os.environ.get(ENV_BUNDLE) or None
    if src and str(src).startswith("hf://"):
        repo, sub, rev = parse_hf(src)
        pinned = pinned_hashes(repo, sub, rev)
        b = cached_bundle(cache_dir, verify=False)
        # A cached bundle serves this source if it came from it, or — for a pinned revision —
        # if its graphs are byte-for-byte the pinned ones, wherever it came from.
        if b is not None and (b.meta.get("source") == str(src) or pinned) \
                and b.verify(pinned):
            return b
        if not allow_download:
            raise BundleError("the model is not in the cache yet, and downloading was "
                              "not allowed")
        b = download_hf(repo, cache_dir, revision=rev, subfolder=sub, token=token,
                        on_progress=on_progress)
        _stamp_source(b, str(src))
        return b
    b = local_bundle(src)
    if b is not None:
        return b
    b = cached_bundle(cache_dir)
    if b is not None:
        return b
    if not allow_download:
        raise BundleError(
            "the autoposer model is not installed yet, and downloading was not allowed")
    hf = os.environ.get(ENV_HF)
    if hf:
        repo, sub, rev = parse_hf(hf)
        b = download_hf(repo, cache_dir, revision=rev, subfolder=sub, token=token,
                        on_progress=on_progress)
        _stamp_source(b, f"hf://{repo}/{sub}@{rev}")
        return b
    return download(cache_dir, api_base=api_base, token=token, on_progress=on_progress)


def _stamp_source(b: Bundle, source: str):
    """Record where a cached bundle came from, so switching sources re-downloads rather than
    silently serving the previous model under the new name."""
    b.meta["source"] = source
    (b.path / "meta.json").write_text(json.dumps(b.meta))
