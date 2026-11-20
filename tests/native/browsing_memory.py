import argparse
import json
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from marionette_driver.marionette import Marionette

from memory import VIDEO, diagnostic_samples
from smoke import wait_for


SITES = ["mail.example.org", "watch.example.net", "news.example.com"]
TAB_SITES = [0, 1, 2, 0, 1, 0, 1]
PROFILES = {"current": (4, -1), "reuse": (2, -1), "compact": (2, 3)}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/clip.webm":
            body, mime = VIDEO, "video/webm"
        else:
            site = self.headers["Host"].split(":")[0]
            if site == SITES[0]:
                rows = "".join(f"<tr><td><input type=checkbox></td><td>Contact {i}</td>"
                               f"<td>Conversation {i}</td><td>Yesterday</td></tr>" for i in range(180))
                content = f"<table>{rows}</table>"
            elif site == SITES[1]:
                content = '<video src="/clip.webm" muted loop playsinline controls></video>' + "".join(
                    f"<article><h3>Episode {i}</h3><p>Series {i // 10}</p></article>" for i in range(100))
            else:
                content = "".join(f"<section><h2>Chapter {i}</h2><p>{'Article text. ' * 40}</p></section>"
                                  for i in range(100))
            body = (f"<!doctype html><meta charset=utf-8><title>{site}</title><link rel=icon href=data:,>"
                    "<style>body{font:16px sans-serif;margin:2rem}td,article{padding:.5rem}</style>"
                    f"<main>{content}</main><script>addEventListener('pageshow',e=>document.body.dataset.cached=e.persisted)</script>").encode()
            mime = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def measure(args, server, name, index):
    limit, viewers = PROFILES[name]
    prefs = {
        "dom.ipc.processCount.webIsolated": limit, "browser.sessionhistory.max_total_viewers": viewers,
        "night.onboarding.completed": True, "night.memory.enabled": False,
        "network.dns.localDomains": ",".join(SITES), "network.proxy.type": 0,
        "dom.security.https_only_mode": False, "dom.security.https_first": False,
    }
    client = Marionette(bin=args.binary, app_args=["-app", args.app_ini], prefs=prefs, headless=False,
                        gecko_log=str(args.output / f"browse-{index}-{name}.log"))
    def snapshot():
        client.set_context("chrome")
        sample = diagnostic_samples(client)
        remote = client.execute_script("""
          return [...Services.wm.getMostRecentWindow('navigator:browser').gBrowser.tabs].map(t => ({
            remote: t.linkedBrowser.remoteType, pid: t.linkedBrowser.browsingContext.currentWindowGlobal.osPid,
            pending: t.hasAttribute('pending')
          }));
        """)
        assert all(not tab["pending"] for tab in remote)
        groups = {}
        for tab in remote:
            groups.setdefault(tab["remote"], set()).add(tab["pid"])
        seen = set()
        for pids in groups.values():
            assert seen.isdisjoint(pids), groups
            seen.update(pids)
        return {"total": sample["total"], "roles": sample["roles"], "tabs": remote}

    def navigate(site, tab, page):
        client.set_context("content")
        client.navigate(f"http://{SITES[site]}:{server.server_port}/{tab}/{page}")
        assert client.title == SITES[site], client.title

    try:
        client.start_session()
        client.set_context("chrome")
        wait_for(lambda: client.execute_script("return !!Services.wm.getMostRecentWindow('navigator:browser')?.gNightBrowser;"))
        assert client.execute_script("return Services.appinfo.fissionAutostart;")
        result = {"profile": name, "empty": snapshot()}
        handles, opens = [], []
        for i, site in enumerate(TAB_SITES):
            start = time.monotonic()
            if i:
                handle = client.open(type="tab")["handle"]
                client.switch_to_window(handle)
            handles.append(client.current_window_handle)
            navigate(site, i, 0)
            opens.append(round(1000 * (time.monotonic() - start)))
            if i == 1:
                result["two_sites"] = snapshot()
        result["opens_ms"] = opens
        result["seven_tabs"] = snapshot()
        assert len(result["seven_tabs"]["tabs"]) == 7
        assert len({tab["remote"] for tab in result["seven_tabs"]["tabs"]}) == 3
        for i, site in enumerate(TAB_SITES):
            client.switch_to_window(handles[i])
            for page in (1, 2):
                navigate(site, i, page)
        result["after_navigation"] = snapshot()
        client.minimize_window()
        time.sleep(3)
        result["minimized"] = snapshot()
        client.execute_script("Services.wm.getMostRecentWindow('navigator:browser').restore();")
        backs = []
        for i, handle in enumerate(handles):
            client.switch_to_window(handle)
            client.set_context("content")
            start = time.monotonic()
            client.go_back()
            wait_for(lambda: client.execute_script("return document.body?.dataset.cached !== undefined"))
            assert client.get_url().endswith(f"/{i}/1")
            backs.append({"ms": round(1000 * (time.monotonic() - start)),
                          "cached": client.execute_script("return document.body.dataset.cached === 'true'")})
        result["backs"] = backs
        return result
    finally:
        client.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--app-ini", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profiles", nargs="+", choices=list(PROFILES), default=["current", "reuse", "compact", "compact", "reuse", "current"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for index, name in enumerate(args.profiles):
            result = measure(args, server, name, index)
            (args.output / f"browse-{index}-{name}.json").write_text(json.dumps(result, indent=2))
            print(name, {phase: result[phase]["total"]["private_working_set_mib"] for phase in
                         ("empty", "two_sites", "seven_tabs", "after_navigation", "minimized")},
                  "median open ms", statistics.median(result["opens_ms"][1:]),
                  "back-cache hits", sum(row["cached"] for row in result["backs"]), flush=True)
