"""A deliberately awkward local ServiceNow and IdP for Heimdall's browser tests.

Keep it awkward: each trap below is a way a real instance made "the catalog URL works sometimes",
or a way a loose label match fills the wrong field.

- Instance on 127.0.0.1, IdP on localhost: different host names, like a real IdP.
- SSO is a redirect chain with a pause at the IdP (the certificate and PIN step), and the
  consume step drops the deep link and lands on navpage, which hops on to a Next Experience home
  that navigates once more by itself a moment later.
- The first catalog visit after sign-in bounces to home (deep link lost).
- Later visits alternate between the classic form and a Next Experience shell with the form in
  an iframe.
- Every page holds a long-poll (AMB), so "networkidle" never arrives.
- Labels: "Short description" and "Description" (substring trap); a mandatory marker inside a
  label; a reference field with a hidden twin input carrying the same label; two fields both
  labelled "Notes" (genuinely ambiguous); a select whose option text has padding.

Standard library only.
"""
import http.server
import socketserver
import threading
import urllib.parse

SYS_ID = "0123456789abcdef0123456789abcdef"
ITEM_PATH = "/com.glideapp.servicecatalog_cat_item_view.do"

_AMB = "<script>(function poll(){fetch('/amb').then(poll,poll)})()</script>"

FORM_HTML = """<!doctype html><html><head><title>security change</title></head><body>%s
<div><label for="sd"><span class="required-marker" aria-label="Mandatory - must be populated before Submit">*</span>
  <span>Short description</span></label><input id="sd"></div>
<div><label for="desc">Description</label><textarea id="desc"></textarea></div>
<div><label for="cat">Category</label>
  <select id="cat"><option value="">-- None --</option><option value="sw"> Software </option>
  <option value="hw"> Hardware </option></select></div>
<div><label for="sys_display.rf">Requested for</label><input id="sys_display.rf">
  <input type="hidden" id="rf" aria-label="Requested for"></div>
<div><label><input type="checkbox" id="ok" checked> I confirm this is accurate</label></div>
<div><label for="n1">Notes</label><input id="n1"></div>
<div><label for="n2">Notes</label><input id="n2"></div>
<div><label for="masked">Phone</label><input id="masked"
  oninput="this.value=this.value.replace(/[^0-9]/g,'')"></div>
<button type="button" id="att" title="Add attachments">Add attachments</button>
<input type="file" id="f" style="display:none"><div id="files"></div>
<button type="button" id="submit" onclick="document.title='SUBMITTED'">Submit</button>
<script>
document.getElementById('att').onclick = () => document.getElementById('f').click();
document.getElementById('f').onchange = e => {
  document.getElementById('files').textContent = e.target.files[0].name; };
</script></body></html>""" % _AMB


class FakeServiceNow:
    """Start with start(), stop with stop(). Set catalog_hits before a run to pick which trap the
    first catalog visit hits: 0 -> lost deep link, then iframe; 2 -> classic form directly."""

    def __init__(self) -> None:
        self.catalog_hits = 0
        self.port = 0
        self.never_sign_in = False
        self._server = None

    @property
    def instance_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeServiceNow":
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code=200, body="", headers=None):
                try:
                    self.send_response(code)
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    data = body.encode("utf-8")
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass   # the browser hung up mid-reply (a navigation); that's normal here

            def _to_idp(self):
                self._send(302, headers={"Location": f"http://localhost:{fake.port}/idp"})

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                host = (self.headers.get("Host") or "").split(":")[0]
                signed_in = "glide_session=1" in (self.headers.get("Cookie") or "")
                if u.path == "/amb":
                    threading.Event().wait(5)          # a long-poll that outlasts any idle wait
                    return self._send(200, "{}")
                if host == "localhost" and u.path == "/idp":
                    if fake.never_sign_in:
                        return self._send(200, "<h1>Pick a certificate</h1>")
                    return self._send(200, (
                        f'<form id="f" method="post" action="http://127.0.0.1:{fake.port}/saml_consume.do">'
                        '<input type="hidden" name="SAMLResponse" value="x"></form>'
                        "<script>setTimeout(() => document.getElementById('f').submit(), 800)</script>"))
                if host != "127.0.0.1":
                    return self._send(404, "wrong host")
                if not signed_in:
                    return self._to_idp()
                if u.path == "/navpage.do":
                    return self._send(200, "<script>location.replace('/now/nav/ui/home')</script>")
                if u.path == "/now/nav/ui/home":
                    return self._send(200, "<h1>Home</h1>" + _AMB + (
                        "<script>setTimeout(() => location.replace('/now/nav/ui/home?x=1'), 1200)</script>"
                        if "x=1" not in u.query else ""))
                if u.path == ITEM_PATH:
                    if "inner=1" in u.query:
                        return self._send(200, FORM_HTML)
                    fake.catalog_hits += 1
                    if fake.catalog_hits == 1:
                        return self._send(302, headers={"Location": "/now/nav/ui/home"})
                    if fake.catalog_hits % 2 == 0:
                        target = urllib.parse.quote(self.path, safe="")
                        return self._send(302, headers={"Location": f"/now/nav/ui/classic/params/target/{target}"})
                    return self._send(200, FORM_HTML)
                if u.path.startswith("/now/nav/ui/classic/params/target/"):
                    inner = urllib.parse.unquote(u.path.split("/target/", 1)[1])
                    sep = "&" if "?" in inner else "?"
                    return self._send(200, f"<h1>Shell</h1>{_AMB}"
                                           f"<iframe src='{inner}{sep}inner=1' width=900 height=900></iframe>")
                return self._send(404, "not found")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if urllib.parse.urlsplit(self.path).path == "/saml_consume.do":
                    return self._send(302, headers={"Location": "/navpage.do",
                                                    "Set-Cookie": "glide_session=1; Path=/; HttpOnly"})
                return self._send(404, "not found")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass   # broken pipes from abandoned long-polls aren't test failures

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
