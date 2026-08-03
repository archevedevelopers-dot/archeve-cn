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
ck("stops a flood of expensive calls",
   any(not server._rate_ok("5.6.7.8", 15) for _ in range(10)))
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

# ── the guard is actually applied to the expensive endpoints ──
src = open("server.py").read()
for ep in ("gcn", "slope", "terrain", "datapack", "datapack_checkout"):
    ck("guard applied to /%s" % ep, "def %s(req: Req, _guard" % ep in src)
ck("/health stays open for uptime checks", "def health(" in src and
   "def health(req: Req, _guard" not in src)

print("\nArcheve AIP service - access control")
print("  %d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
