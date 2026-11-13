"""Repairs to auto-rsa's SoFi run, applied at runtime (the engine's files are left alone).

Seen on macOS, Chrome 157, nodriver 0.47, 2026-11-09 and 11-11:

  * Chrome no longer sends `sameParty` on a cookie. nodriver's Cookie parser still requires
    it, and the KeyError is raised inside nodriver's websocket reader, which then dies without
    a word. Every later browser call waits forever (run #31 sat for two days). The parser now
    takes cookies as Chrome sends them, a reply that can't be parsed fails that one call, and
    cookies are read through the browser connection (the tab connection answered "Browser
    context management is not supported").
  * SoFi's verification page is six one-digit boxes and a "Remember this device" checkbox.
    auto-rsa looks for `input#code` and `#rememberBrowser`, finds neither, logs an error and
    carries on as if logged in.
  * A wrong password showed "Email or password is incorrect." auto-rsa read that page as
    "No valid 2FA method found" and also carried on.

A login that doesn't reach the SoFi app now stops the SoFi run with what SoFi's page said.
"""

import asyncio
import builtins
import json

BASE = "https://www.sofi.com"
WAIT_AFTER_LOGIN = 25  # seconds to wait for the app, a code page or an error after "Log In"
WAIT_AFTER_CODE = 30
_state: dict = {"failed": None}

REFUSED = "SoFi refused the login ({reason}). Check the email and password on the Brokerages page"

# What the page is showing, without any typed values (the form holds the email and password).
_LOOK = r"""
(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const pick = (sel) => [...new Set([...document.querySelectorAll(sel)].filter(vis).map(txt).filter(Boolean))].slice(0, 20);
  const skip = ['checkbox', 'radio', 'hidden', 'submit', 'button', 'password', 'email', 'file'];
  const fields = [...document.querySelectorAll('input')].filter(vis)
    .filter((i) => !skip.includes((i.type || 'text').toLowerCase()) && i.id !== 'username');
  const boxes = fields.filter((i) => i.maxLength === 1);
  const body = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  return JSON.stringify({
    url: location.href,
    boxes: boxes.length,
    fields: fields.length,
    filled: (boxes.length ? boxes : fields.slice(0, 1)).map((i) => i.value || '').join(''),
    code_page: boxes.length >= 4 || (fields.length > 0 && ![...document.querySelectorAll('input[type=password]')].some(vis)),
    wrong_password: /(email|username) or password is incorrect|incorrect (email|username|password)/i.test(body),
    locked: /account (is |has been )?locked|too many (failed )?attempts|temporarily (locked|blocked)/i.test(body),
    bad_code: /(code|passcode)[^.]{0,40}(incorrect|invalid|expired|doesn.t match|not valid)|(incorrect|invalid|wrong|expired) (verification )?code/i.test(body),
    texted: /sent a text message/i.test(body),
    headings: pick('h1,h2,h3,[role=heading]'),
    buttons: pick('button,[role=button],input[type=submit]'),
    links: pick('a,[role=link]'),
    inputs: [...document.querySelectorAll('input')].filter(vis)
      .map((i) => `${i.type}:${i.id || i.name || i.getAttribute('aria-label') || i.autocomplete || ''}:max${i.maxLength}`).slice(0, 15),
  });
})()
"""

# Focus code box number N (or the single code field when there are no boxes).
_FOCUS = r"""
((n) => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const skip = ['checkbox', 'radio', 'hidden', 'submit', 'button', 'password', 'email', 'file'];
  const fields = [...document.querySelectorAll('input')].filter(vis)
    .filter((i) => !skip.includes((i.type || 'text').toLowerCase()) && i.id !== 'username');
  const boxes = fields.filter((i) => i.maxLength === 1);
  const el = boxes.length ? boxes[n] : fields[0];
  if (!el) return false;
  el.focus();
  if (el.select) el.select();
  return true;
})(%d)
"""

_REMEMBER = r"""
(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  let cb = document.querySelector('input#rememberBrowser');
  if (!cb) {
    cb = [...document.querySelectorAll('input[type=checkbox]')].find((c) => {
      const lab = c.closest('label') || (c.id && document.querySelector(`label[for="${c.id}"]`)) || c.parentElement;
      return /remember/i.test((lab && lab.innerText) || '');
    });
  }
  if (!cb) return 'box not found';
  if (cb.checked) return 'was already ticked';
  const lab = cb.closest('label') || (cb.id && document.querySelector(`label[for="${cb.id}"]`));
  (vis(cb) ? cb : lab || cb).click();
  return cb.checked ? 'ticked' : 'clicked';
})()
"""

_VERIFY = r"""
(() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const b = [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(vis)
    .find((e) => /^\s*(verify( code)?|continue|submit)\s*$/i.test(e.innerText || e.value || ''));
  if (!b) return 'none';
  if (b.disabled) return 'disabled';
  b.click();
  return 'clicked';
})()
"""


# ── nodriver ────────────────────────────────────────────────────────────────
def patch_nodriver() -> None:
    try:
        from nodriver import cdp
        from nodriver.core import browser as nd_browser
        from nodriver.core import connection as nd_connection
    except ImportError:
        return
    net = cdp.network

    if not getattr(net.Cookie.from_json, "_awt_patched", False):

        def from_json(cls, j):
            return cls(
                name=str(j["name"]),
                value=str(j["value"]),
                domain=str(j.get("domain", "")),
                path=str(j.get("path", "/")),
                size=int(j.get("size", 0)),
                http_only=bool(j.get("httpOnly", False)),
                secure=bool(j.get("secure", False)),
                session=bool(j.get("session", False)),
                priority=net.CookiePriority.from_json(j.get("priority", "Medium")),
                same_party=bool(j.get("sameParty", False)),  # Chrome stopped sending this
                source_scheme=net.CookieSourceScheme.from_json(j.get("sourceScheme", "Unset")),
                source_port=int(j.get("sourcePort", -1)),
                expires=float(j["expires"]) if j.get("expires") is not None else None,
                same_site=net.CookieSameSite.from_json(j["sameSite"]) if j.get("sameSite") is not None else None,
                partition_key=net.CookiePartitionKey.from_json(j["partitionKey"]) if j.get("partitionKey") is not None else None,
                partition_key_opaque=bool(j["partitionKeyOpaque"]) if j.get("partitionKeyOpaque") is not None else None,
            )

        from_json._awt_patched = True
        net.Cookie.from_json = classmethod(from_json)

    tx_cls = nd_connection.Transaction
    if not getattr(tx_cls.__call__, "_awt_patched", False):
        original_call = tx_cls.__call__

        def call(self, **response):
            # An exception here used to kill the websocket reader, and every later call hung.
            try:
                return original_call(self, **response)
            except Exception as e:
                if not self.done():
                    self.set_exception(RuntimeError(f"Chrome's reply couldn't be read ({type(e).__name__}: {str(e)[:200]})"))

        call._awt_patched = True
        tx_cls.__call__ = call

    jar = nd_browser.CookieJar
    if not getattr(jar.get_all, "_awt_patched", False):

        async def get_all(self, requests_cookie_format: bool = False):
            if requests_cookie_format:
                raise NotImplementedError("requests_cookie_format isn't used by auto-rsa")
            try:
                return await self._browser.connection.send(cdp.storage.get_cookies())
            except Exception:
                # Older Chrome: ask an open tab instead.
                for t in self._browser.tabs:
                    if not t.closed:
                        return await t.send(cdp.network.get_cookies())
                raise

        async def save(self, file=".session.dat", pattern: str = ".*"):  # noqa: ARG001
            import pathlib
            import pickle

            cookies = await get_all(self)
            with pathlib.Path(file).resolve().open("w+b") as f:
                pickle.dump(cookies, f)

        async def load(self, file=".session.dat", pattern: str = ".*"):  # noqa: ARG001
            import pathlib
            import pickle

            with pathlib.Path(file).resolve().open("r+b") as f:
                saved = pickle.load(f)  # noqa: S301  (our own file, written by save above)
            params = [
                cdp.network.CookieParam(
                    name=c.name,
                    value=c.value,
                    domain=c.domain,
                    path=c.path,
                    secure=c.secure,
                    http_only=c.http_only,
                    same_site=c.same_site,
                    expires=cdp.network.TimeSinceEpoch(c.expires) if c.expires and c.expires > 0 else None,
                )
                for c in saved
            ]
            await self._browser.connection.send(cdp.storage.set_cookies(params))

        get_all._awt_patched = True
        jar.get_all, jar.save, jar.load = get_all, save, load


# ── the page ────────────────────────────────────────────────────────────────
async def look(page) -> dict:
    raw = await asyncio.wait_for(page.evaluate(_LOOK), 15)
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}


def in_app(url: str) -> bool:
    u = (url or "").split("?")[0]
    return "overview" in u or (u.startswith(BASE + "/wealth/app") and "login" not in u)


async def reached_app(page, seconds: float, *, code_page_ends: bool) -> dict:
    """Poll until SoFi's app is showing (twice in a row, since the app address can flash
    by on its way to the login page) or the page says what it needs. Returns the last look.
    After Log In a code page ends the wait; after the code is sent only an answer does."""
    hits, seen = 0, {}
    for _ in range(int(seconds)):
        await asyncio.sleep(1)
        try:
            seen = await look(page)
        except Exception:
            continue  # mid-navigation
        if in_app(seen.get("url", "")):
            hits += 1
            if hits >= 2:
                seen["in_app"] = True
                return seen
            continue
        hits = 0
        if seen.get("wrong_password") or seen.get("locked") or seen.get("bad_code"):
            return seen
        if code_page_ends and seen.get("code_page"):
            return seen
    return seen


def describe(seen: dict, name: str) -> None:
    keep = {k: seen.get(k) for k in ("url", "headings", "buttons", "links", "inputs")}
    print(f"{name} page now: {json.dumps(keep)[:1700]}")


async def type_code(page, code: str) -> bool:
    """Put the code into SoFi's boxes. Typing into the first box lets the page move along
    by itself; if it doesn't, each box is filled in turn."""
    from nodriver import cdp

    async def chars(text: str) -> None:
        for ch in text:
            await page.send(cdp.input_.dispatch_key_event("char", text=ch))
            await asyncio.sleep(0.12)

    before = await look(page)
    if not before.get("code_page"):
        return False
    if not await page.evaluate(_FOCUS % 0):
        return False
    await chars(code)
    await asyncio.sleep(0.4)
    after = await look(page)
    if in_app(after.get("url", "")) or after.get("filled") == code:
        return True
    if after.get("boxes", 0) >= len(code):
        for i, ch in enumerate(code):
            if await page.evaluate(_FOCUS % i):
                await chars(ch)
        await asyncio.sleep(0.4)
        after = await look(page)
        return in_app(after.get("url", "")) or after.get("filled") == code
    return False


async def handle_2fa(page, account: list[str], name: str, seen: dict) -> None:
    secret = account[2] if len(account) > 2 else None
    if isinstance(secret, str) and secret.strip().lower() in ("", "none", "false"):
        secret = None
    for attempt in (1, 2):
        if secret and not seen.get("texted"):
            import pyotp

            code = pyotp.TOTP(secret).now()
        else:
            # input() is the dashboard prompt; in a thread so the browser connection stays alive.
            ask = f"{name}: SoFi texted a 6-digit code to your phone. Type it here"
            if attempt == 2:
                ask = f"{name}: SoFi didn't accept that code. Type the newest code it texted you"
            code = (await asyncio.to_thread(builtins.input, ask)).strip().replace(" ", "")
            if not code or code.lower() == "cancel":
                raise RuntimeError("no verification code was entered, so the login stopped")
            if not code.isdigit():
                raise RuntimeError("the verification code should be digits only")
        try:
            print(f"{name}: Remember this device: {await page.evaluate(_REMEMBER)}")
        except Exception:
            pass
        if not await type_code(page, code):
            describe(await look(page), name)
            raise RuntimeError("couldn't type the verification code into SoFi's page")
        clicked = await page.evaluate(_VERIFY)
        if clicked == "disabled":
            await asyncio.sleep(1)
            await page.evaluate(_VERIFY)
        seen = await reached_app(page, WAIT_AFTER_CODE, code_page_ends=False)
        if seen.get("in_app"):
            return
        if seen.get("bad_code") and attempt == 1 and not (secret and not seen.get("texted")):
            continue
        describe(seen, name)
        if seen.get("bad_code"):
            raise RuntimeError("SoFi didn't accept the verification code")
        raise RuntimeError("the code went in, but SoFi didn't open the account page")


async def _need(finding, what: str):
    try:
        el = await finding
    except (TimeoutError, asyncio.TimeoutError):
        el = None
    if not el:
        raise RuntimeError(f"SoFi's login form didn't show {what}")
    return el


async def login(sofi_api, browser, page, account: list[str], name: str, discord_loop=None) -> None:
    """auto-rsa's own form steps (they still match SoFi's login form), then what SoFi
    answers: the app, a wrong password, or a verification code."""
    try:
        await asyncio.sleep(5)
        page = await browser.get(BASE)
        if not page:
            raise RuntimeError("SoFi's site didn't load")
        await page.get(BASE + "/wealth/app")
        await asyncio.sleep(2)
        user = await _need(page.select("input[id=username]"), "an email box")
        await user.send_keys(account[0])
        pw = await _need(page.select("input[type=password]"), "a password box")
        await pw.send_keys(account[1])
        button = await _need(page.find("Log In", best_match=True), "a Log In button")
        await button.click()

        seen = await reached_app(page, WAIT_AFTER_LOGIN, code_page_ends=True)
        if seen.get("in_app"):
            return
        if seen.get("wrong_password"):
            raise RuntimeError(REFUSED.format(reason="Email or password is incorrect"))
        if seen.get("locked"):
            raise RuntimeError(REFUSED.format(reason="the account is locked or has too many attempts"))
        if seen.get("code_page"):
            await handle_2fa(page, account, name, seen)
            return
        describe(seen, name)
        raise RuntimeError("SoFi showed a page this version doesn't recognise after Log In (details in the log line above)")
    except Exception as e:
        _state["failed"] = str(e) or type(e).__name__
        await sofi_api._sofi_error(f"Error logging in to {name}: {_state['failed']}", page=page, discord_loop=discord_loop)


# ── wiring ──────────────────────────────────────────────────────────────────
def apply(sofi_api) -> None:
    if sofi_api is None or getattr(sofi_api, "_awt_patched", False):
        return
    patch_nodriver()
    original_init = sofi_api.sofi_init

    async def login_and_account(browser, page, account, name, bot_obj=None, discord_loop=None):  # noqa: ARG001
        await login(sofi_api, browser, page, account, name, discord_loop)

    def sofi_init(sofi_account, name, cookie_filename, bot_obj, browser, discord_loop, sofi_obj):
        _state["failed"] = None
        result = original_init(sofi_account, name, cookie_filename, bot_obj, browser, discord_loop, sofi_obj)
        if _state["failed"] or result is None:
            # auto-rsa would print "Logged in" and go on to holdings or an order regardless.
            raise RuntimeError(f"{name} isn't logged in, so nothing else was tried")
        return result

    sofi_api._sofi_login_and_account = login_and_account
    sofi_api.sofi_init = sofi_init
    sofi_api._awt_patched = True
