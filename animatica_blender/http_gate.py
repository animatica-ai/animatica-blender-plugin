# SPDX-License-Identifier: GPL-3.0-or-later
"""The gated opener every request of this add-on goes through: it asks a gate (online access
allowed, or this machine) about each request, redirects included, and keeps a token off any
host other than the one it was meant for."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request


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


#: The host application's say on where a request may go, or None to allow everything (the
#: runtime used on its own). Called with the URL of every request the openers here make — the
#: first one AND every redirect hop — before anything connects; False refuses it. A Blender
#: addon points it at "is online access allowed, or is this this machine": checking only the
#: URL a caller started from let a server on localhost redirect the request anywhere.
GATE = None


class RequestRefused(urllib.error.URLError):
    """The host's gate said no to this URL. Nothing was sent anywhere."""


class Gate(urllib.request.BaseHandler):
    """Asks `GATE` about each request before it is opened, redirects included.

    A pre-processor rather than a check in the redirect handler: urllib runs it for every
    request the opener opens, whatever sent it there, and before any socket exists.
    """

    handler_order = 100             # ahead of every other handler

    def _check(self, req):
        gate = GATE
        if gate is not None and not gate(req.full_url):
            try:
                host = urllib.parse.urlsplit(req.full_url).hostname or req.host
            except ValueError:
                host = req.host
            raise RequestRefused(f"not allowed to connect to {host}")
        return req

    http_request = https_request = ftp_request = _check


def build_opener(*handlers):
    """An opener that honours `GATE` and keeps tokens off other hosts."""
    return urllib.request.build_opener(Gate, _DropAuthOnRedirect, *handlers)
