"""Clear answer when e-Sign (Digio) is not configured yet.

WHY: with no DIGIO_CLIENT_ID / DIGIO_CLIENT_SECRET on the server, POST
/contracts/me/stage2/esign/initiate still calls Digio, Digio answers 401, and the
API returns 502 ("Could not start e-Sign: Digio returned 401"). Cloudflare replaces
an origin 502 with its own branded error page, so the app showed
"The origin web server returned an invalid or incomplete response..." - which
looks like a crash / overloaded server and cost the app a Play "Broken
Functionality" finding.

Now, when the server is going to make REAL Digio calls but has no credentials, it
answers immediately with a plain 409 and a human sentence, without calling Digio.
A 409 is not one of the errors Cloudflare swaps for its own page, and the app
already shows the response's `detail` string in the Stage 2 error box.

Dev / test are unaffected (the client is in mock mode there), and so is any
production server that HAS credentials (a real Digio failure still returns 502).
"""
from __future__ import annotations

ESIGN_UNAVAILABLE_MESSAGE = (
    "Agreement signing is being set up and will be available shortly. "
    "Please check back soon."
)


def esign_not_configured(client) -> bool:
    """True when `client` would make real Digio calls but has no credentials."""
    if getattr(client, "mock", False):
        return False
    return not (getattr(client, "client_id", "") and getattr(client, "client_secret", ""))
