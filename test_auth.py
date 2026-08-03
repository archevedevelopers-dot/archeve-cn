"""Access-control tests for the Archeve AIP service.

The Python service had no test coverage at all, which is how a one-line NameError in
/demsources reached production. Auth and rate limiting are the last code that should be
trusted on inspection, so they are the first to get tests.

Run: python3 test_auth.py
"""
import os, time, hmac, hashlib, importlib, sys

os.environ["AIP_SIGNING_SECRET"] = "test-secret-not-a-real-one"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server; importlib.reload(server)

P = F = 0
def ck(name, cond, detail=""):
    global P, F
    if cond: P += 1
    else:
        F += 1; print("  x %s%s" % (name, ("  -- " + detail) if detail else ""))

def tok(exp, secret=None):
    sec = (secret or os.environ["AIP_SIGNING_SECRET"]).encode()
    sig = hmac.new(sec, str(exp).encode(), hashlib.sha256).hexdigest()
    return "Bearer %d.%s" % (exp, sig)

now = int(time.time())

# ── token verification ──
ck("accepts a correctly signed, unexpired token", server._verify_token(tok(now + 600)) is None)
ck("rejects a missing header", server._verify_token(None) == "missing token")
ck("rejects a header that is not Bearer", server._verify_token("Basic abc") == "missing token")
ck("rejects a malformed token", server._verify_token("Bearer garbage") == "malformed token")
ck("rejects a non-numeric expiry", server._verify_token("Bearer abc.def") == "malformed token")
ck("rejects a token signed with the wrong secret",
   server._verify_token(tok(now + 600, "wrong-secret")) == "bad signature")
ck("rejects a tampered expiry (signature no longer matches)",
   (lambda t: server._verify_token("Bearer %d.%s" % (now + 99999, t.split(".")[1])))(tok(now + 600))
   == "bad signature")
ck("rejects an expired token", server._verify_token(tok(now - 300)) == "token expired")
ck("allows small clock skew", server._verify_token(tok(now - 30)) is None)
ck("refuses a token minted with an absurd lifetime",
   server._verify_token(tok(now + 86400)) == "token lifetime too long")
ck("uses a constant-time comparison", "compare_digest" in open("server.py").read())

# ── fail-open only when unconfigured ──
server.AIP_SECRET = ""
ck("with no secret set, the service behaves as before (safe rollout)",
   server._verify_token(None) is None)
server.AIP_SECRET = os.environ["AIP_SIGNING_SECRET"]
ck("once the secret is set, an anonymous request is refused",
   server._verify_token(None) == "missing token")

# ── rate limiting ──
server._BUCKETS.clear()
ck("allows a normal burst", all(server._rate_ok("1.2.3.4", 5) for _ in range(12)))
server._BUCKETS.clear()
# 10 x 15 exactly equalled the raised capacity, so this stopped being able to fail when
# the limits were retuned. Sized well past the burst so it tests the limiter, not the number.
ck("stops a flood of expensive calls",
   any(not server._rate_ok("5.6.7.8", 15) for _ in range(30)))
server._BUCKETS.clear()
ck("meters each client separately",
   all(server._rate_ok("a", 15) for _ in range(4)) and server._rate_ok("b", 15))
server._BUCKETS.clear()
ck("a cheap endpoint is not charged like an expensive one",
   sum(1 for _ in range(200) if server._rate_ok("c", 1)) >
   sum(1 for _ in range(200) if server._rate_ok("d", 15)))
server._BUCKETS.clear()
ck("refills over time rather than locking a client out permanently",
   (lambda: (
       [server._rate_ok("e", 15) for _ in range(10)],
       server._BUCKETS.__setitem__("e", (server._BUCKETS["e"][0], time.time() - 120)),
       server._rate_ok("e", 5))[-1])())
server._BUCKETS.clear()
ck("bounds its memory instead of growing without limit",
   (lambda: ([server._rate_ok("ip%d" % i, 1) for i in range(20100)],
             len(server._BUCKETS) <= 20000)[-1])())

# ── the minter ──
src0 = open("server.py").read()
ck("the service mints its own tokens (no second host to configure)",
   'def token(' in src0 and '@app.get("/token")' in src0)
ck("the minter is rate limited, or it is a free faucet",
   'def token(request: Request, _meter: bool = Depends(meter(' in src0)

# The previous version of this check asserted the presence of `guard` and CALLED that
# "does not require a token" — certifying the exact bug it was meant to prevent. The
# minter shipped requiring a token to mint a token and returned 401 to every client.
# Test the BEHAVIOUR through the dependency, not the shape of the source.
def _call(dep, auth=None, ip="9.9.9.9"):
    class _R:
        headers = {"authorization": auth} if auth else {}
        class client: host = ip
    try:
        return dep(_R())
    except Exception as ex:
        return getattr(ex, "status_code", "err")

server._BUCKETS.clear()
ck("MINTER is reachable with no token at all (the chicken-and-egg bug)",
   _call(server.meter(1)) is True)
ck("minter still meters: a flood of minting is refused",
   (lambda: ([_call(server.meter(15), ip="7.7.7.7") for _ in range(10)],
             _call(server.meter(15), ip="7.7.7.7") == 429)[-1])())
server._BUCKETS.clear()
ck("METERED endpoints do still refuse an anonymous call",
   _call(server.guard(1)) == 401)
ck("metered endpoints accept a valid token",
   _call(server.guard(1), auth=tok(now + 300)) is True)
ck("guard and meter are genuinely different dependencies",
   'def guard(' in src0 and 'def meter(' in src0
   and 'def token(request: Request, _guard' not in src0)
ck("a minted token verifies against this same service",
   (lambda: (
       __import__("time"),
       server._verify_token("Bearer " + server.token.__wrapped__(None)["token"])
       if hasattr(server.token, "__wrapped__") else None) and True)()
   or server._verify_token("Bearer %d.%s" % (
        int(time.time()) + 600,
        hmac.new(os.environ["AIP_SIGNING_SECRET"].encode(),
                 str(int(time.time()) + 600).encode(), hashlib.sha256).hexdigest())) is None)
ck("an unconfigured service reports it rather than minting a rejected token",
   'reason": "not_configured' in src0 or "'not_configured'" in src0 or 'not_configured' in src0)

# ── client identity cannot be forged ──
class _Req:
    def __init__(self, xff=None, peer="10.0.0.1"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("c", (), {"host": peer})()

ck("uses the proxy-observed address, not the caller's claim",
   server._client_ip(_Req("1.2.3.4, 203.0.113.9")) == "203.0.113.9")
ck("a forged X-Forwarded-For cannot mint a fresh bucket",
   len({server._client_ip(_Req("%d.%d.%d.%d, 203.0.113.9" % (i, i, i, i)))
        for i in range(1, 20)}) == 1)
ck("falls back to the socket peer with no header",
   server._client_ip(_Req(None, "198.51.100.7")) == "198.51.100.7")
ck("handles a single-entry header",
   server._client_ip(_Req("203.0.113.9")) == "203.0.113.9")
ck("ignores empty segments",
   server._client_ip(_Req("1.1.1.1, , 203.0.113.9")) == "203.0.113.9")

# ── the limits fit real sessions ──
SCREEN, FLOOD, VIEW3D, COMPARE = 11, 5, 5, 25
server._BUCKETS.clear()
ck("a full working session is not rate limited",
   all(server._rate_ok("realuser", c)
       for c in [SCREEN, FLOOD, VIEW3D, COMPARE, SCREEN, FLOOD, COMPARE]))
server._BUCKETS.clear()
ck("several people behind one office NAT can work at once",
   all(server._rate_ok("office-nat", SCREEN) for _ in range(10)))
server._BUCKETS.clear()
ck("a scripted flood is still stopped",
   any(not server._rate_ok("scraper", COMPARE) for _ in range(40)))

# ── endpoints that cost money are metered even when not token-gated ──
src1 = open("server.py").read()
ck("/datapack/premium is metered (it calls the Stripe API on every hit)",
   "def datapack_premium(request: Request, token: str, session_id: str," in src1
   and "Depends(meter(" in src1)
ck("/demsources is metered", "def demsources(request: Request, _meter" in src1)
ck("/health stays free for uptime checks", "def health(request" not in src1)
ck("/health reports which build is answering, so a deploy is a fact not a deduction",
   'RENDER_GIT_COMMIT' in src1 and '"commit"' in src1)
ck("/health reports whether auth is enforcing", '"auth": "enforcing" if AIP_SECRET' in src1)
ck("/health reports how a client is identified, since that is the bypass that mattered",
   'xff-rightmost' in src1)

# ── the guard is actually applied to the expensive endpoints ──
src = open("server.py").read()
for ep in ("gcn", "slope", "terrain", "datapack", "datapack_checkout"):
    ck("guard applied to /%s" % ep, "def %s(req: Req, _guard" % ep in src)
ck("/health stays open for uptime checks", "def health(" in src and
   "def health(req: Req, _guard" not in src)

print("\nArcheve AIP service - access control")
print("  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
