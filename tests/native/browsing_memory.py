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
PROFILES = {"previous": (4, -1), "default": (None, None), "reuse": (2, -1),
            "single": (1, -1), "compact": (2, 3)}
LEGACY_WIDGETS = """discoverystreamfeed sectionslayoutfeed wallpaperfeed weatherfeed webnotificationsfeed
    stocksfeed adsfeed inferredpersonalizationfeed smartshortcutsfeed newtabattributionfeed
    newtabmessaging listsfeed privacyfeed recentsearchesfeed pictureofthedayfeed timerfeed
    travelfeed externalcomponentsfeed extensionwidgetsfeed""".split()


def legacy_preferences():
    return {f"browser.newtabpage.activity-stream.feeds.{name}": True for name in LEGACY_WIDGETS}


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


def browser(args, name, extra_prefs):
    prefs = {
        "night.onboarding.completed": True, "night.memory.enabled": False,
        "network.dns.localDomains": ",".join(SITES), "network.proxy.type": 0,
        "dom.security.https_only_mode": False, "dom.security.https_first": False,
        **extra_prefs,
    }
    return Marionette(bin=args.binary, app_args=["-app", args.app_ini], prefs=prefs, headless=False,
                      gecko_log=str(args.output / f"{name}.log"))


def start(client):
    client.start_session()
    client.set_context("chrome")
    wait_for(lambda: client.execute_script("return !!Services.wm.getMostRecentWindow('navigator:browser')?.gNightBrowser;"))
    assert client.execute_script("return Services.appinfo.fissionAutostart;")


def measure_home(args, server, legacy_feeds, index):
    prefs = legacy_preferences() if legacy_feeds else {}
    client = browser(args, f"home-{index}", prefs)
    try:
        start(client)
        client.set_context("content")
        client.navigate("about:home")
        wait_for(lambda: client.execute_script("return !!document.getElementById('query');"))
        client.set_context("chrome")
        def feeds():
            return client.execute_script("""
              const service = ChromeUtils.importESModule("resource:///modules/AboutNewTab.sys.mjs").AboutNewTab;
              return [...(service.activityStream?.store.feeds.keys() || [])];
            """)
        wait_for(lambda: "feeds.system.topsites" in feeds(), timeout=45)
        time.sleep(3)
        names = feeds()
        expected = {"feeds." + name for name in LEGACY_WIDGETS}
        assert (expected.issubset(names) if legacy_feeds else expected.isdisjoint(names)), names
        home = diagnostic_samples(client)
        client.set_context("content")
        for i, site in enumerate(SITES[:2]):
            if i:
                client.switch_to_window(client.open(type="tab")["handle"])
            client.navigate(f"http://{site}:{server.server_port}/home-test")
            assert client.title == site
        client.set_context("chrome")
        return {"legacy_feeds": legacy_feeds, "feeds": names, "home": home, "two_sites": diagnostic_samples(client)}
    finally:
        client.cleanup()


def measure_idle(args, server, delay, index):
    client = browser(args, f"idle-{index}", {"javascript.options.compact_on_user_inactive_delay": delay})
    try:
        start(client)
        client.set_context("content")
        for i, site in enumerate(SITES[:2]):
            if i:
                client.switch_to_window(client.open(type="tab")["handle"])
            client.navigate(f"http://{site}:{server.server_port}/idle")
            assert client.title == site
        client.find_element("css selector", "h3").click()
        client.set_context("chrome")
        active = diagnostic_samples(client)
        client.minimize_window()
        time.sleep(60)
        minimized = diagnostic_samples(client)
        client.execute_script("Services.wm.getMostRecentWindow('navigator:browser').restore();")
        client.set_context("content")
        assert client.title == SITES[1]
        opened = time.monotonic()
        client.switch_to_window(client.open(type="tab")["handle"])
        client.navigate(f"http://{SITES[2]}:{server.server_port}/resume")
        assert client.title == SITES[2]
        return {"delay_ms": delay, "active": active, "minimized": minimized,
                "open_after_resume_ms": round(1000 * (time.monotonic() - opened))}
    finally:
        client.cleanup()


def measure(args, server, name, index):
    limit, viewers = PROFILES[name]
    prefs = legacy_preferences() if name == "previous" else {}
    if limit is not None:
        prefs.update({"dom.ipc.processCount.webIsolated": limit, "browser.sessionhistory.max_total_viewers": viewers})
    client = browser(args, f"browse-{index}-{name}", prefs)
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
        start(client)
        client.set_context("content")
        client.navigate("about:home")
        wait_for(lambda: client.execute_script("return !!document.querySelector('#query');"))
        client.set_context("chrome")
        wait_for(lambda: client.execute_script("""
          const { AboutNewTab } = ChromeUtils.importESModule("resource:///modules/AboutNewTab.sys.mjs");
          return AboutNewTab.activityStream?.store.feeds.has("feeds.system.topsites");
        """), timeout=45)
        result = {"profile": name, "home": snapshot()}
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
    parser.add_argument("--profiles", nargs="+", choices=list(PROFILES), default=["previous", "default", "single", "single", "default", "previous"])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--idle", action="store_true")
    group.add_argument("--home", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if args.home:
            for index, legacy in enumerate([True, False, False, True]):
                result = measure_home(args, server, legacy, index)
                (args.output / f"home-{index}.json").write_text(json.dumps(result, indent=2))
                print("old feeds", legacy, "count", len(result["feeds"]), "home", result["home"]["total"],
                      "two sites", result["two_sites"]["total"], flush=True)
            raise SystemExit(0)
        if args.idle:
            for index, delay in enumerate([300000, 30000, 30000, 300000]):
                result = measure_idle(args, server, delay, index)
                (args.output / f"idle-{index}.json").write_text(json.dumps(result, indent=2))
                print("delay", delay, "active", result["active"]["total"], "minimized", result["minimized"]["total"],
                      "open after restore ms", result["open_after_resume_ms"], flush=True)
            raise SystemExit(0)
        for index, name in enumerate(args.profiles):
            result = measure(args, server, name, index)
            (args.output / f"browse-{index}-{name}.json").write_text(json.dumps(result, indent=2))
            print(name, {phase: result[phase]["total"]["private_working_set_mib"] for phase in
                         ("home", "two_sites", "seven_tabs", "after_navigation", "minimized")},
                  "median open ms", statistics.median(result["opens_ms"][1:]),
                  "back-cache hits", sum(row["cached"] for row in result["backs"]), flush=True)
