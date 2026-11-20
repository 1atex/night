import argparse
import json
import os
import platform
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
from marionette_driver.marionette import Marionette
from rendering import PAGE as RENDER_PAGE
from smoke import wait_for

MIB = 1024 ** 2
METRICS = ("working_set_mib", "private_working_set_mib", "private_commit_mib", "cpu_seconds")
VIDEO = (Path(__file__).parent / "fixtures/watch.webm").read_bytes()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/watch.webm":
            body, content_type = VIDEO, "video/webm"
        elif self.path == "/video":
            body = b'<!doctype html><title>Video fixture</title><video src="/watch.webm" autoplay muted loop playsinline></video>'
            content_type = "text/html"
        elif self.path == "/rendering":
            body, content_type = RENDER_PAGE, "text/html"
        else:
            body = b'''<!doctype html><title>Memory fixture</title><link rel="icon" href="data:,"><h1>Night memory fixture</h1><script>window.allocation = new Uint8Array(32 * 1024 * 1024); window.allocation.fill(73); document.title = 'Ready ' + location.pathname;</script>'''
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def sample(pid):
    values = {"working_set_mib": 0, "private_working_set_mib": 0, "private_bytes_mib": 0, "processes": 0}
    root = psutil.Process(pid)
    for process in [root, *root.children(recursive=True)]:
        try:
            info = process.memory_full_info()
            values["working_set_mib"] += info.rss / 1048576
            values["private_working_set_mib"] += info.uss / 1048576
            values["private_bytes_mib"] += getattr(info, "private", info.uss) / 1048576
            values["processes"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return values


def samples(pid):
    result = []
    for _ in range(5):
        result.append(sample(pid))
        time.sleep(1)
    return {key: round(statistics.median(item[key] for item in result), 2) for key in result[0]}


def process_snapshot(browser):
    descriptors = browser.execute_async_script("""
      const done = arguments[arguments.length - 1];
      ChromeUtils.requestProcInfo().then(root => {
        done([root, ...root.children].map(({pid, type}) => ({pid, type})));
      }, () => done(null));
    """)
    if not descriptors:
        raise RuntimeError("Gecko process information is unavailable")
    by_pid = {item["pid"]: item["type"] for item in descriptors}
    root = psutil.Process(descriptors[0]["pid"])
    rows = []
    for process in [root, *root.children(recursive=True)]:
        try:
            memory = process.memory_full_info()
            cpu = process.cpu_times()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        commit = getattr(memory, "private", None)
        rows.append({
            "pid": process.pid,
            "role": by_pid.get(process.pid, "unclassified"),
            "working_set_mib": memory.rss / MIB,
            "private_working_set_mib": memory.uss / MIB,
            "private_commit_mib": commit / MIB if commit is not None else None,
            "cpu_seconds": cpu.user + cpu.system,
        })
    if not rows or rows[0]["pid"] != root.pid:
        raise RuntimeError("Browser parent memory is unavailable")
    return rows


def diagnostic_samples(browser):
    measurements = []
    for index in range(5):
        if index:
            time.sleep(1)
        measurements.append(process_snapshot(browser))
    roles = sorted({row["role"] for sample in measurements for row in sample})

    def summarize(role=None):
        series = [[row for row in sample if role is None or row["role"] == role] for sample in measurements]
        result = {"processes": statistics.median(map(len, series))}
        for key in METRICS[:-1]:
            result[key] = None if any(row[key] is None for sample in series for row in sample) else round(
                statistics.median(sum(row[key] for row in sample) for sample in series), 2)
        start = {row["pid"]: row["cpu_seconds"] for row in series[0]}
        result["cpu_seconds"] = round(sum(max(0, row["cpu_seconds"] - start[row["pid"]])
                                         for row in series[-1] if row["pid"] in start), 3)
        return result

    return {"total": summarize(), "roles": {role: summarize(role) for role in roles}, "samples": measurements}


def profile_rendering(browser, url, destination):
    browser.execute_script("Services.wm.getMostRecentWindow('navigator:browser').restore();")
    started = browser.execute_async_script("""
      const done = arguments[arguments.length - 1];
      Services.profiler.StartProfiler(262144, 1, ['js', 'stackwalk', 'cpu', 'memory'],
        ['GeckoMain', 'Compositor', 'Renderer', 'MediaDecoder', 'MediaPDecoder'])
        .then(() => done(true), error => done(String(error)));
    """)
    if started is not True:
        raise RuntimeError(f"Could not start Gecko Profiler: {started}")
    try:
        browser.set_context("content")
        browser.navigate(url + "/video")
        wait_for(lambda: browser.execute_script("return document.querySelector('video').currentTime > 0;"))
        time.sleep(4)
        browser.set_context("chrome")
        graphics_memory = browser.execute_async_script(r"""
          const done = arguments[arguments.length - 1], reports = [];
          const manager = Cc['@mozilla.org/memory-reporter-manager;1'].getService(Ci.nsIMemoryReporterManager);
          manager.getReports((process, path, kind, units, bytes) => {
            if (units === 0 && /^(gpu-|gfx\/|explicit\/gfx\/|d3d11-)/.test(path)) {
              reports.push({process: process || 'Parent', path, kind, bytes});
            }
          }, null, () => done(reports), null, true);
        """)
        browser.set_context("content")
        browser.navigate(url + "/rendering")
        wait_for(lambda: browser.execute_script("return typeof window.wrappedJSObject.measure === 'function';"))
        viewport = browser.execute_script("return {width: innerWidth, height: innerHeight, scale: devicePixelRatio};")
        rounds = [browser.execute_script("return window.wrappedJSObject.measure();") for _ in range(7)]
        browser.set_context("chrome")
        written = browser.execute_async_script("""
          const done = arguments[arguments.length - 1];
          Services.profiler.dumpProfileToFileAsync(arguments[0]).then(() => done(true), error => done(String(error)));
        """, script_args=[str(destination.resolve())])
        if written is not True:
            raise RuntimeError(f"Could not save Gecko profile: {written}")
        return {"file": destination.name, "rounds": rounds, "viewport": viewport,
                "playback_graphics_memory": graphics_memory,
                "js_ms_median": statistics.median(row["js"] for row in rounds[1:]),
                "style_layout_ms_median": statistics.median(row["styleLayout"] for row in rounds[1:])}
    finally:
        browser.set_context("chrome")
        browser.execute_async_script("const done = arguments[arguments.length - 1]; Services.profiler.StopProfiler().then(() => done(true));")


def measure(binary, app_ini, url, output, label, diagnostic=False, profile=False):
    prefs = {"night.onboarding.completed": True, "browser.aboutwelcome.enabled": False, "browser.preonboarding.enabled": False, "browser.bypassAutoTriggerActions": True, "termsofuse.bypassNotification": True, "browser.shell.checkDefaultBrowser": False, "browser.startup.homepage_override.mstone": "ignore"}
    if diagnostic:
        prefs["night.memory.enabled"] = False
    browser = Marionette(prefs=prefs, bin=binary, headless=False, app_args=["-app", app_ini] if app_ini else [], gecko_log=str(output / f"memory-{label}-gecko.log"), startup_timeout=60)
    try:
        startup = time.monotonic()
        browser.start_session()
        browser.timeout.script = 30
        browser.set_context("chrome")
        wait_for(lambda: browser.execute_script("return !!Services.wm.getMostRecentWindow('navigator:browser')?.gBrowserInit?.delayedStartupFinished;"), timeout=45)
        if app_ini:
            wait_for(lambda: browser.execute_script("return !!Services.wm.getMostRecentWindow('navigator:browser')?.gNightBrowser;"))
        startup_ms = round((time.monotonic() - startup) * 1000)
        if diagnostic:
            browser.set_window_rect(width=1280, height=900)
            idle = diagnostic_samples(browser)
            browser.set_context("content")
            browser.navigate(url + "/video")
            playback = """
              const video = document.querySelector('video');
              const quality = video.getVideoPlaybackQuality();
              return {width: video.videoWidth, height: video.videoHeight, time: video.currentTime,
                frames: quality.totalVideoFrames, dropped: quality.droppedVideoFrames};
            """
            wait_for(lambda: browser.execute_script(playback)["frames"] > 5, timeout=30)
            video_start = browser.execute_script(playback)
            browser.set_context("chrome")
            playing = diagnostic_samples(browser)
            browser.set_context("content")
            video_end = browser.execute_script(playback)
            assert video_end["frames"] > video_start["frames"], video_end
            browser.set_context("chrome")
        browser.execute_script("""
          const w = Services.wm.getMostRecentWindow('navigator:browser');
          w.gBrowser.selectedBrowser.loadURI(Services.io.newURI(arguments[0] + '/0'), {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});
          for (let i = 1; i < 7; i++) w.gBrowser.addTrustedTab(arguments[0] + '/' + i);
        """, script_args=[url])
        wait_for(lambda: browser.execute_script("""
          return [...Services.wm.getMostRecentWindow('navigator:browser').gBrowser.tabs].filter(t => t.label.startsWith('Ready ')).length === 7;
        """), timeout=45)
        pid = browser.execute_script("return Services.appinfo.processID;")
        if diagnostic:
            active = diagnostic_samples(browser)
            browser.minimize_window()
            time.sleep(3)
            minimized = diagnostic_samples(browser)
            loaded = browser.execute_script("""
              const tabs = Services.wm.getMostRecentWindow('navigator:browser').gBrowser.tabs;
              return Array.from(tabs).filter(tab => !tab.hasAttribute('pending')).length;
            """)
            assert loaded == 7, loaded
            runtime = browser.execute_script("""
              const win = Services.wm.getMostRecentWindow('navigator:browser');
              return {version: Services.appinfo.version, gecko: Services.appinfo.platformVersion,
                buildID: Services.appinfo.platformBuildID, locale: Services.locale.appLocaleAsBCP47,
                compositor: win.windowUtils.layerManagerType, remoteCompositor: win.windowUtils.layerManagerRemote,
                preallocated: Services.prefs.getIntPref('dom.ipc.processPrelaunch.fission.number', -1)};
            """)
            result = {"runtime": runtime, "startup_ms": startup_ms, "loaded_tabs": loaded, "idle": idle,
                      "video": {"memory": playing, "start": video_start, "end": video_end},
                      "active": active, "minimized": minimized}
            if profile:
                result["profile"] = profile_rendering(browser, url, output / f"{label}-profile.json")
            return result
        browser.minimize_window()
        time.sleep(3)
        before = samples(pid)
        unloaded = 0
        if app_ini:
            unloaded = browser.execute_async_script("""
              const done = arguments[arguments.length-1], w = Services.wm.getMostRecentWindow('navigator:browser');
              const { NightMemory } = ChromeUtils.importESModule('resource:///modules/night/NightMemory.sys.mjs');
              let now = 0; const memory = new NightMemory(w, () => false, () => now);
              w.memoryExcepted = w.gBrowser.tabs[1];
              w.memoryPinned = w.gBrowser.tabs[2];
              memory.protect(w.memoryExcepted, true);
              w.gBrowser.pinTab(w.memoryExcepted);
              w.gBrowser.pinTab(w.memoryPinned);
              now = 15 * 60000;
              memory.checkNow().then(async count => { await memory.checkNow(); await memory.checkNow(); memory.destroy(); done(count); }, error => { memory.destroy(); done({error: String(error)}); });
            """)
            assert unloaded == 5, unloaded
        time.sleep(5)
        after = samples(pid)
        states = browser.execute_script("""
          const w = Services.wm.getMostRecentWindow('navigator:browser'), tabs = [...w.gBrowser.tabs];
          return {tabs: tabs.length, sleeping: tabs.filter(t => t.hasAttribute('pending')).length, selectedLoaded: !tabs.find(t => t.selected).hasAttribute('pending'), exceptedLoaded: !(w.memoryExcepted || tabs[1]).hasAttribute('pending'), pinnedSleeping: w.memoryPinned?.hasAttribute('pending') ?? null};
        """)
        assert states["selectedLoaded"] and states["exceptedLoaded"], states
        if app_ini: assert states["sleeping"] == 5 and states["pinnedSleeping"], states
        return {"before": before, "after": after, "unloaded": unloaded, "state": states}
    finally:
        browser.cleanup()
        browser.instance = None


def run(stock, night, app_ini, output, diagnostic, profile):
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = {"platform": platform.platform(), "headless": os.environ.get("MOZ_HEADLESS") == "1", "method": "Browser minimized before measurement; headless mode is recorded separately. Fresh profiles, 7 same-origin local tabs, each allocates and touches 32 MiB. Median of 5 samples at 1 second intervals; all descendants included. Night idle clock advanced 15 minutes; active tab and one explicit exception protected. No forced GC or working-set trimming. Windows USS is private working set; private bytes is commit. This is not a real-site workload or rendering benchmark."}
        if diagnostic:
            result["method"] = "Fresh profiles, no add-ons, localhost VP8 video and seven 32 MiB HTML fixtures. Five samples per phase. All seven tabs remain loaded. Roles from Gecko, bytes and CPU from psutil. CPU delta counts only processes present at both ends of the sample window. Private commit is Windows-only."
        result["firefox"] = measure(stock, None, url, output, "firefox", diagnostic, profile)
        result["night"] = measure(night, app_ini, url, output, "night", diagnostic, profile)
        if diagnostic:
            assert result["firefox"]["runtime"]["gecko"] == result["night"]["runtime"]["gecko"]
        (output / ("process-memory.json" if diagnostic else "memory.json")).write_text(json.dumps(result, indent=2))
        if diagnostic:
            for label in ("firefox", "night"):
                print(label, {phase: result[label][phase]["total"]["private_working_set_mib"]
                              for phase in ("idle", "active", "minimized")}, flush=True)
        else:
            print(json.dumps(result, indent=2), flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock", required=True)
    parser.add_argument("--night", required=True)
    parser.add_argument("--app-ini", required=True)
    parser.add_argument("--output", type=Path, default=Path("test-artifacts"))
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--profile-rendering", action="store_true")
    args = parser.parse_args()
    run(args.stock, args.night, args.app_ini, args.output, args.diagnostic or args.profile_rendering, args.profile_rendering)
