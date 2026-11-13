"""One job, one process. Runs inside the auto-rsa engine's own virtualenv.

The server starts this script, writes a JSON job spec as the first line of
stdin, and reads structured events back. Brokerage credentials arrive as the
environment variables auto-rsa already expects.

Three things make auto-rsa drivable from a dashboard instead of a terminal:
  * input() is replaced, so a 2FA code or CAPTCHA request becomes a prompt the
    dashboard answers (the reply comes back on stdin as one JSON line);
  * print_and_discord / print_all_holdings are replaced, so order outcomes and
    holdings come back as data instead of console text;
  * every other print becomes a log event, tagged with the broker running now.

Events go to a private copy of the original stdout. File descriptor 1 is then
pointed at stderr, so anything a library or browser driver writes straight to
the terminal shows up as raw log text instead of corrupting the event stream.

Only the standard library is used here, so the same file also runs paper-only
jobs with the server's Python when the engine isn't installed.
"""

import base64
import builtins
import io
import json
import os
import re
import sys
import threading
import traceback
from pathlib import Path

_evt = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)
_emit_lock = threading.Lock()
_current = {"broker": None, "failed": None}
# Webull logins that never really happened this run (refused, or a username Webull can't use).
# auto-rsa still prints "Logged in to Webull!" after each one; LogWriter drops that line once per login.
_webull_state: dict = {"refused": {}, "unreal_logins": 0}


def emit(t: str, **data) -> None:
    data["t"] = t
    if "broker" not in data:
        data["broker"] = _current["broker"]
    line = json.dumps(data, default=str)
    with _emit_lock:
        _evt.write(line + "\n")
        _evt.flush()


# Lines that mean the broker run failed even though auto-rsa carries on: its own
# "Error with/in <broker>", a broker module's "Error logging in to …", any
# "Login Failed", a broker with no credentials, and SoFi's own "Sofi error:" (its run swallows
# every exception, so without this a failed SoFi run would end as "Done" with $0.00).
_FAIL = re.compile(r"^(Error (?:with|in) \w[\w ]*?:|Error logging in\b|Sofi error:|.*\bLogin Failed\b|.* not found, skipping)", re.I)


class LogWriter(io.TextIOBase):
    """Turns print() output into log events, one per line."""

    def __init__(self) -> None:
        self._buf = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._line(line)
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._line(self._buf)
                self._buf = ""

    def _line(self, line: str) -> None:
        line = line.rstrip()
        if not line.strip() or set(line.strip()) <= set("=-"):
            return
        if line.strip() == "Logged in to Webull!" and _webull_state["unreal_logins"] > 0:
            # auto-rsa prints this after its retry loop without checking the login worked.
            _webull_state["unreal_logins"] -= 1
            return
        if _FAIL.match(line.strip()) and _current["failed"] is None:
            _current["failed"] = line.strip()[:300]
        emit("log", text=line[:2000])


# ── prompts ─────────────────────────────────────────────────────────────────
_input_lock = threading.Lock()
_prompt_seq = [0]


def bridged_input(prompt: object = "") -> str:
    text = str(prompt).strip()
    with _input_lock:
        _prompt_seq[0] += 1
        pid = _prompt_seq[0]
        image = None
        if "captcha" in text.lower():
            p = Path("captcha.png")
            if p.exists():
                image = base64.b64encode(p.read_bytes()).decode()
        emit("prompt", id=pid, text=text or "The brokerage is asking for input", image=image)
        line = sys.stdin.readline()
        emit("prompt_done", id=pid)
        if not line:
            return ""
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if reply.get("cancel"):
            return "cancel"
        return str(reply.get("value", "")).strip()


# ── holdings and messages from the engine ───────────────────────────────────
def last4(account: object) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", str(account))
    return "…" + (s[-4:] if len(s) >= 4 else s)


def emit_holdings(broker_obj, loop=None, *, mask_account_number: bool = True) -> None:  # noqa: ARG001
    accounts_by_parent = broker_obj.get_account_numbers()
    if not accounts_by_parent:
        # A login that returns no accounts would otherwise read as "Done, $0.00".
        if _current["failed"]:
            return  # the login error already says why; don't add a "logged in, but…" line after it
        name = getattr(broker_obj, "_Brokerage__name", "this brokerage")
        print(f"Error with {name}: logged in, but no accounts came back, so there is nothing to show. Check that login's details on the Brokerages page")
        return
    for parent, accounts in accounts_by_parent.items():
        out = []
        for account in accounts:
            positions = []
            for sym, h in (broker_obj.get_holdings(parent, account) or {}).items():
                positions.append(
                    {
                        "symbol": str(sym).upper(),
                        "qty": float(h.get("quantity") or 0),
                        "price": float(h.get("price") or 0),
                        "total": float(h.get("total") or 0),
                    }
                )
            total = broker_obj.get_account_totals(parent, account)
            try:
                acct_type = broker_obj.get_account_types(parent, account)
            except Exception:
                acct_type = ""
            out.append(
                {
                    "account": last4(account),
                    "total": float(total) if isinstance(total, (int, float)) else None,
                    "type": acct_type or "",
                    "positions": positions,
                }
            )
        emit("holdings", parent=str(parent), accounts=out)


def bridged_print_and_discord(message, loop=None, *, embed: bool = False) -> None:  # noqa: ARG001
    if embed:
        emit("log", text=f"[embed] {json.dumps(message)[:1500]}")
        return
    emit("msg", text=str(message)[:2000])


# Buttons whose name became ambiguous after a site redesign. Fidelity added
# "Log in with a passkey" next to "Log in" (seen 2026-11-05), so the library's
# get_by_role("button", name="Log in") matches both and Playwright's strict mode
# refuses to click. Asking for the exact name picks the real one.
EXACT_ROLE_NAMES = {("button", "Log in")}


def patch_playwright() -> None:
    try:
        from playwright.sync_api import Frame, FrameLocator, Locator, Page
    except ImportError:
        return
    for cls in (Page, Frame, FrameLocator, Locator):
        original = cls.get_by_role

        def get_by_role(self, role, *args, _original=original, **kwargs):
            if (role, kwargs.get("name")) in EXACT_ROLE_NAMES and kwargs.get("exact") is None:
                kwargs["exact"] = True
            return _original(self, role, *args, **kwargs)

        cls.get_by_role = get_by_role


def describe_page(page, label: str) -> None:
    """Log what's on screen (headings, buttons, links, inputs) and save a screenshot,
    so a login step that stops matching the site can be fixed from one run."""
    try:
        info = page.evaluate(
            """() => {
              const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
              const txt = (e) => (e.innerText || e.getAttribute('aria-label') || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 90);
              const pick = (sel) => [...new Set([...document.querySelectorAll(sel)].filter(vis).map(txt).filter(Boolean))].slice(0, 25);
              return {
                url: location.href,
                headings: pick('h1,h2,h3,h4,[role=heading]'),
                buttons: pick('button,[role=button],input[type=submit]'),
                links: pick('a,[role=link]'),
                inputs: [...document.querySelectorAll('input')].filter(vis)
                  .map((i) => `${i.type}:${i.placeholder || i.getAttribute('aria-label') || i.name || i.id}`).slice(0, 15),
              };
            }"""
        )
        emit("log", text=f"{label} page now: {json.dumps(info)[:1800]}")
    except Exception as e:
        emit("log", text=f"{label}: couldn't read the page ({e})")
    try:
        from datetime import datetime

        Path("debug").mkdir(exist_ok=True)
        shot = Path("debug") / f"{label.lower().replace(' ', '-')}-{datetime.now():%Y%m%d-%H%M%S}.png"
        page.screenshot(path=str(shot), full_page=True)
        emit("log", text=f"{label}: screenshot saved to data/engine/{shot}")
    except Exception:
        pass


def _visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _first_visible(page, roles: tuple[str, ...], name: "re.Pattern[str]"):
    for role in roles:
        loc = page.get_by_role(role, name=name)
        if _visible(loc):
            return loc.first
    return None


def _code_box(page):
    for loc in (
        page.get_by_placeholder("XXXXXX"),
        page.locator("input[autocomplete='one-time-code']"),
        page.get_by_label(re.compile(r"(security|verification|one.time)?\s*code", re.I)),
    ):
        if _visible(loc):
            return loc.first
    return None


ANOTHER_WAY = re.compile(r"try another way|(use|choose|try) (a |an )?(different|another|other) (way|method|option)|other (ways|options|methods)", re.I)
TEXT_ME = re.compile(r"^\s*(text me( the code)?|text( a)? (the )?code|send (me )?(a |the )?(text|code)( by text)?|text message|sms)\b", re.I)
CONTINUE = re.compile(r"^\s*(continue|send( code)?|next|submit|verify)\s*$", re.I)


def fidelity_2fa_fallback(fa) -> tuple[bool, bool]:
    """Fidelity's 2FA screens as of late 2026, beyond the two the library knows."""
    page = fa.page
    page.wait_for_timeout(1000)
    # Push-notification page whose "other way" control is a button or reworded.
    alt = _first_visible(page, ("link", "button"), ANOTHER_WAY)
    if alt:
        alt.click()
        page.wait_for_timeout(1500)
        fa.wait_for_loading_sign()
    # "Text me" as a button or link, or as a radio choice followed by Continue/Send.
    choice = _first_visible(page, ("button", "link"), TEXT_ME)
    if choice:
        choice.click()
    else:
        radio = _first_visible(page, ("radio",), TEXT_ME)
        if radio:
            radio.check()
            go = _first_visible(page, ("button",), CONTINUE)
            if go:
                go.click()
    if choice or _code_box(page) is None:
        page.wait_for_timeout(1500)
        fa.wait_for_loading_sign()
    box = _code_box(page)
    if box is not None:
        box.click()
        return (True, False)  # auto-rsa now asks for the code, which becomes the dashboard prompt
    if _visible(page.get_by_text(re.compile(r"authenticator app", re.I))):
        raise RuntimeError("Fidelity wants a code from your authenticator app. Edit this login and add the authenticator secret, then test again")
    if _visible(page.get_by_text(re.compile(r"notification|approve|Fidelity app|push", re.I))):
        bridged_input("Fidelity sent a sign-in notification to your phone. Approve it in the Fidelity app, then type ok and press Send")
        try:
            page.wait_for_url(re.compile(r"/portfolio/summary"), timeout=60000)
            return (True, True)
        except Exception as e:
            raise RuntimeError("The Fidelity app approval didn't go through") from e
    raise RuntimeError("Fidelity showed a 2FA screen this version doesn't recognise")


# Fidelity's anti-automation page. It sits on the sign-in URL, so the library
# mistakes it for 2FA. Fidelity's Terms of Use forbid third-party trading tools,
# so this is a stop sign, not a selector to work around.
FIDELITY_BLOCK = re.compile(r"complete this action right now", re.I)
FIDELITY_BLOCK_NOTICE = (
    "Fidelity blocked this automated login (its \"Sorry, we can't complete this action right now\" page). "
    "Fidelity's Terms of Use forbid third-party trading tools, so the dashboard stopped here instead of retrying"
)
_fidelity_state = {"blocked": False}


def fidelity_blocked(page) -> bool:
    return page is not None and _visible(page.get_by_text(FIDELITY_BLOCK))


def use_chrome_for_fidelity(fid) -> None:
    """Run fidelity-api's browser in the installed Google Chrome instead of Playwright's
    Firefox. The library calls `playwright.firefox.launch(...)` in one place, so that
    attribute is pointed at Chrome; everything after the launch (login, 2FA, orders) is
    unchanged. The window follows the dashboard's Headless setting like any other browser."""
    original_sync = fid.sync_playwright

    class ChromeLauncher:
        def __init__(self, chromium):
            self._chromium = chromium

        def launch(self, **kwargs):
            kwargs.pop("args", None)  # the library passes Firefox-only flags
            emit("log", text="Fidelity is opening in Google Chrome")
            try:
                return self._chromium.launch(channel="chrome", **kwargs)
            except Exception as e:
                if "chrome" in str(e).lower() and ("not found" in str(e).lower() or "executable" in str(e).lower()):
                    raise RuntimeError("Google Chrome isn't installed (looked in /Applications). Install it, or switch the Fidelity browser back to Firefox in Settings") from e
                raise

    class ChromePlaywright:
        def __init__(self, pw):
            self._pw = pw
            self.firefox = ChromeLauncher(pw.chromium)

        def __getattr__(self, name):
            return getattr(self._pw, name)

    class ChromeManager:
        def __init__(self):
            self._manager = original_sync()

        def start(self):
            return ChromePlaywright(self._manager.start())

        def __enter__(self):
            return ChromePlaywright(self._manager.__enter__())

        def __exit__(self, *exc):
            return self._manager.__exit__(*exc)

    fid.sync_playwright = ChromeManager


def patch_fidelity() -> None:
    try:
        from fidelity import fidelity as fid
    except ImportError:
        return
    if os.environ.get("FIDELITY_BROWSER") == "chrome":
        use_chrome_for_fidelity(fid)
    original_login = fid.FidelityAutomation.login
    original_2fa = fid.FidelityAutomation.login_2FA

    def login(self, username, password, totp_secret=None, save_device=False):
        if _fidelity_state["blocked"]:
            # Same Mac, same browser: the next login would hit the same block.
            print("Error logging in to Fidelity: skipped, Fidelity blocked an earlier login in this run")
            return (False, False)
        result = original_login(self, username, password, totp_secret, save_device)
        page = getattr(self, "page", None)
        url = getattr(page, "url", "") or ""
        if result != (False, False) or "signin" not in url:
            return result
        if fidelity_blocked(page):
            _fidelity_state["blocked"] = True
            print(f"Error logging in to Fidelity: {FIDELITY_BLOCK_NOTICE}")
            return (False, False)
        print("Fidelity's 2FA screen didn't match the library; trying the other known screens")
        try:
            return fidelity_2fa_fallback(self)
        except Exception as e:
            describe_page(self.page, "Fidelity")
            print(f"Error logging in to Fidelity: {e}")
            return (False, False)

    def login_2fa(self, code, save_device=True):
        if _visible(self.page.get_by_placeholder("XXXXXX")):
            return original_2fa(self, code, save_device)
        box = _code_box(self.page)
        if box is None:
            describe_page(self.page, "Fidelity")
            return False
        box.fill(code)
        go = _first_visible(self.page, ("button",), CONTINUE)
        if go:
            go.click()
        try:
            self.page.wait_for_url(re.compile(r"/portfolio/summary"), timeout=20000)
            return True
        except Exception:
            describe_page(self.page, "Fidelity")
            return False

    fid.FidelityAutomation.login = login
    fid.FidelityAutomation.login_2FA = login_2fa


# Webull takes an email, or a phone number written with its country code: +1-5551234567.
# The webull library sends anything that isn't a valid email as a phone number, so a bare
# 10-digit number reaches Webull as an account it can't match, and Webull answers that as
# a wrong password. Five of those lock the login. Same rule as server/catalog.py.
_WEBULL_PHONE = re.compile(r"\+\d{1,3}-\d{6,14}")


def webull_username_problem(username: str) -> str | None:
    u = (username or "").strip()
    if "@" in u or _WEBULL_PHONE.fullmatch(u):
        return None
    return (
        "the saved username isn't in a form Webull accepts. Use your email address, or your phone "
        "number written like +1-5551234567 (country code, a dash, then the number)"
    )


def patch_webull() -> None:
    """The webull library's login() returns Webull's reply instead of raising when the
    login is refused, and auto-rsa then carries on and prints "Logged in to Webull!".
    Say what Webull actually answered (its code and message only, never tokens).

    auto-rsa also retries the login three times in a row. After a refusal that would send
    the same wrong password two more times, and five wrong passwords lock the account for
    30 minutes, so a refused username is not sent again during this run.

    A username Webull can't use (see webull_username_problem) is stopped before anything is
    sent, and with no session the PIN and account lookups are skipped too."""
    try:
        from webull import webull as Webull
    except ImportError:
        return
    original_login = Webull.login
    original_trade_token = Webull.get_trade_token
    original_account_id = Webull.get_account_id

    def login(self, username="", *args, **kwargs):
        if username in _webull_state["refused"] and not kwargs.get("mfa"):
            return _webull_state["refused"][username]
        problem = webull_username_problem(username) if username else None
        if problem:
            result = {"code": "username_format", "msg": problem}
            _webull_state["refused"][username] = result
            _webull_state["unreal_logins"] += 1
            print(f"Error logging in to Webull: {problem}. Nothing was sent to Webull, so this can't lock the login. Fix it on the Brokerages page")
            return result
        result = original_login(self, username, *args, **kwargs)
        if isinstance(result, dict) and "accessToken" not in result:
            _webull_state["refused"][username] = result
            _webull_state["unreal_logins"] += 1
            code = str(result.get("code", "?"))[:60]
            msg = str(result.get("msg") or result.get("message") or "no message")[:200]
            print(
                f"Error logging in to Webull: Webull refused the login (code {code}: {msg}). "
                "Check the username, password and Device ID (DID) on the Brokerages page"
            )
        return result

    def get_trade_token(self, *args, **kwargs):
        if not getattr(self, "_access_token", ""):
            return False  # no session, so there is nothing for the PIN to unlock
        return original_trade_token(self, *args, **kwargs)

    def get_account_id(self, *args, **kwargs):
        if not getattr(self, "_access_token", ""):
            return None
        return original_account_id(self, *args, **kwargs)

    Webull.login = login
    Webull.get_trade_token = get_trade_token
    Webull.get_account_id = get_account_id


def patch_sofi(auto_rsa) -> None:
    """auto-rsa's fun_run starts sofi_run(order_obj=, bot_obj=, loop=) with no `command`, but
    sofi_run still requires one (it reads the second item to choose holdings or a trade), so every
    SoFi run died with "missing 1 required positional argument: 'command'". Supply it from the order."""
    original = getattr(auto_rsa, "sofi_run", None)
    if original is None or getattr(original, "_awt_patched", False):
        return

    def sofi_run(order_obj, command=None, bot_obj=None, loop=None):
        if command is None:
            command = ("sofi", "_holdings" if order_obj.get_holdings() else "_transaction")
        return original(order_obj, command, bot_obj, loop)

    sofi_run._awt_patched = True
    auto_rsa.sofi_run = sofi_run


def patch_sofi_login() -> None:
    """SoFi's login, verification-code page and cookie reads (see runner/sofi_patch.py)."""
    sofi_api = sys.modules.get("src.brokerages.sofi_api")
    if sofi_api is None:
        return
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import sofi_patch

        sofi_patch.apply(sofi_api)
    except Exception as e:
        emit("log", text=f"SoFi repairs didn't load, so SoFi runs as auto-rsa ships it: {type(e).__name__}: {e}")


def patch_engine() -> None:
    patch_playwright()
    patch_fidelity()
    patch_webull()
    patch_sofi(sys.modules.get("src.auto_rsa"))
    patch_sofi_login()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("src") or mod is None:
            continue
        if hasattr(mod, "print_and_discord"):
            mod.print_and_discord = bridged_print_and_discord
        if hasattr(mod, "print_all_holdings"):
            mod.print_all_holdings = emit_holdings


# ── main ────────────────────────────────────────────────────────────────────
def run_engine_broker(spec: dict, broker: str) -> None:
    from src.auto_rsa import fun_run
    from src.brokers import AllBrokersInfo
    from src.helper_api import StockOrder

    info = AllBrokersInfo().parse_input(broker)
    if info is None:
        raise RuntimeError(f"auto-rsa doesn't know a broker called {broker}")
    order = StockOrder()
    if spec["kind"] == "holdings":
        order.set_holdings(holdings=True)
    else:
        order.set_action(spec["action"])
        order.set_amount(float(spec["qty"]))
        for s in spec["symbols"]:
            order.set_stock(s)
        order.set_dry(dry=bool(spec["dry"]))
    order.set_brokers(info)
    order.order_validate(pre_login=True)
    fun_run(order)


def main() -> None:
    spec = json.loads(sys.stdin.readline())
    sys.stdout = LogWriter()
    builtins.input = bridged_input
    emit("start", pid=os.getpid())

    real = [b for b in spec["brokers"] if b != "paper"]
    if real:
        engine = spec.get("engine_dir")
        if not engine or not Path(engine, "src", "auto_rsa.py").exists():
            for b in real:
                emit("broker", broker=b, state="error", note="The auto-rsa engine isn't installed yet")
            real = []
        else:
            sys.path.insert(0, engine)
            try:
                import src.auto_rsa  # noqa: F401  (imports every broker module)

                patch_engine()
            except BaseException as e:  # sys.exit inside the engine lands here too
                for b in real:
                    emit("broker", broker=b, state="error", note=f"Engine failed to load: {e}")
                emit("log", text=traceback.format_exc())
                real = []

    for broker in spec["brokers"]:
        if broker != "paper" and broker not in real:
            continue
        _current["broker"] = broker
        _current["failed"] = None
        emit("broker", state="running")
        try:
            if broker == "paper":
                from paper import run as run_paper

                run_paper(spec, emit, bridged_input)
            else:
                run_engine_broker(spec, broker)
            sys.stdout.flush()
            if _current["failed"]:
                emit("broker", state="error", note=_current["failed"])
            else:
                emit("broker", state="done")
        except BaseException as e:
            sys.stdout.flush()
            emit("log", text=traceback.format_exc())
            emit("broker", state="error", note=str(e)[:300] or type(e).__name__)
    _current["broker"] = None
    emit("end")


if __name__ == "__main__":
    main()
