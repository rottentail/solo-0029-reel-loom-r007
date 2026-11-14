"""A bounded production walkthrough using generated audiovisual media and HTTP."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
from .config import Settings
from .application import Application
from .api import Api
from .http import MediaHttpServer
from .client import Client
from .errors import LoomError, require

WORKFLOWS = ("ingest", "revisions", "looks", "render", "signal", "waveform", "loudness",
             "sheet", "captions", "imprint", "cancel-retry", "recovery", "resources", "matrix",
             "conformance", "lineage", "redaction")


def expect_error(action, code):
    try:
        action()
    except LoomError as exc:
        require(exc.code == code, "walkthrough_error", "Unexpected workflow rejection", expected=code, actual=exc.code)
        return exc.as_dict()
    raise RuntimeError("The walkthrough expected an operation to be rejected: " + code)


class Walkthrough:
    def __init__(self, settings):
        self.settings = settings
        self.app = Application(settings, start_workers=False)
        self.server = MediaHttpServer(("127.0.0.1", 0), Api(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}/loom/v1")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def setup(self):
        with self.app.blobs.temporary(".mp4") as sample:
            self.app.processes.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                                    "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
                                    "-af", "volume=enable='between(t,1,1.5)':volume=0",
                                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)])
            self.content = sample.read_bytes()
        self.asset = self.client.ingest(self.content, "harbor-tone")
        self.slices = [{"asset": self.asset["id"], "in": .2, "out": 1.2, "label": "开场"},
                       {"asset": self.asset["id"], "in": 1.6, "out": 2.8, "label": "收束"}]
        self.reel = self.client.create_reel("回声样片", self.slices, "demo-reel")
        self.look = self.client.put_look("preview", {"kind": "video", "width": 320, "height": 180})
        self.audio_look = self.client.put_look("listening", {"kind": "audio", "sample_rate": 48000})
        self.ticket = self.client.render(self.reel["id"], "preview", key="demo-render")
        return self

    def finish(self):
        ticket = self.client.request("GET", "/tickets/"+self.ticket["id"])
        if ticket["state"] == "queued":
            self.app.renderer.run_once()
        ticket = self.client.wait(self.ticket["id"], timeout=5)
        require(ticket["state"] == "succeeded", "walkthrough_render", "Walkthrough render did not succeed", ticket=ticket)
        return ticket

    def run(self, name):
        c, a = self.client, self.asset["id"]
        if name == "ingest":
            duplicate = c.ingest(self.content, "duplicate-name")
            require(duplicate["id"] == a, "walkthrough_dedup", "Identical ready media should be reused")
            failure = expect_error(lambda: c.ingest(b"not-media", "bad"), "unreadable_media")
            return {"asset": self.asset, "deduplicated": True, "failure": failure}
        if name == "revisions":
            updated = c.replace_reel(self.reel["id"], 1, "回声样片·修订", list(reversed(self.slices)))
            old = c.request("GET", "/reels/"+self.reel["id"]+"?revision=1")
            require(old["hash"] == self.reel["hash"] and updated["revision"] == 2,
                    "walkthrough_revision", "Historical reel changed")
            failure = expect_error(lambda: c.replace_reel(self.reel["id"], 1, "旧稿", self.slices), "revision_conflict")
            return {"head": updated["head"], "historical_hash": old["hash"], "failure": failure}
        if name == "looks":
            new = c.put_look("preview", {"kind": "video", "width": 640, "height": 360}, 1)
            ticket = c.request("GET", "/tickets/"+self.ticket["id"])
            require(ticket["look_version"] == 1 and new["version"] == 2, "walkthrough_look", "Queued look drifted")
            failure = expect_error(lambda: c.put_look("preview", {"kind":"video"}, 1), "revision_conflict")
            return {"new_version": 2, "frozen_version": ticket["look_version"], "failure": failure}
        if name == "render":
            ticket = self.finish()
            body = {"reel": self.reel["id"], "look": "preview"}
            replay = c.request("POST", "/tickets", body, {"Idempotency-Key":"demo-render"})
            require(replay["id"] == ticket["id"], "walkthrough_idempotency", "Render replay changed identity")
            failure = expect_error(lambda: c.request("POST", "/tickets", {**body,"look":"listening"},
                                                   {"Idempotency-Key":"demo-render"}), "idempotency_conflict")
            audio = c.render(self.reel["id"], "listening")
            self.app.renderer.run_once()
            audio = c.wait(audio["id"], 5)
            require(audio["state"] == "succeeded", "walkthrough_audio", "Audio rendition did not succeed")
            return {"video": ticket, "audio": audio, "failure": failure}
        if name in ("signal", "waveform", "loudness"):
            result = c.request("POST", f"/assets/{a}/{name}", {})
            replay = c.request("POST", f"/assets/{a}/{name}", {})
            require(result["id"] == replay["id"], "walkthrough_analysis", "Analysis cache should be content/settings stable")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/{name}", {"unsupported": 1}), "unknown_fields")
            return {"analysis": result, "cached": True, "failure": failure}
        if name == "sheet":
            result = c.request("POST", f"/assets/{a}/contact-sheet", {"count": 6, "columns": 3})
            media = c.request("GET", "/artifacts/"+result["id"]+"/content", raw=True)
            require(media.startswith(b"\xff\xd8"), "walkthrough_sheet", "Contact sheet is not JPEG")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/contact-sheet", {"count":1,"columns":3}), "sheet_layout")
            return {"artifact": result, "bytes": len(media), "failure": failure}
        if name == "captions":
            track = c.request("POST", f"/assets/{a}/cues", {"language":"zh-CN",
                              "srt":"1\n00:00:00,000 --> 00:00:02,900\n潮声\n"})
            projected = c.request("POST", f'/reels/{self.reel["id"]}/captions', {"revision":1,"tracks":[track["id"]]})
            require(len(projected["cues"]) == 2, "walkthrough_captions", "A cue spanning cuts must project to both slices")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/cues", {"language":"zh","srt":"broken"}), "cue_format")
            return {"projection": projected, "track": track, "failure": failure}
        if name == "imprint":
            failure = expect_error(lambda: c.imprint(self.ticket["id"]), "imprint_state")
            ticket = self.finish()
            imprint = c.imprint(ticket["id"])
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            self.client.replace_reel(self.reel["id"], 1, "新的草剪", list(reversed(self.slices)))
            unchanged = c.imprint(ticket["id"])
            require(imprint["id"] == unchanged["id"], "walkthrough_imprint", "Upstream editing changed frozen imprint")
            return {"imprint": imprint, "verified": verified, "failure": failure}
        if name == "cancel-retry":
            cancelled = c.request("POST", f'/tickets/{self.ticket["id"]}/cancel')
            require(cancelled["state"] == "cancelled", "walkthrough_cancel", "Queued render did not cancel")
            retried = c.request("POST", f'/tickets/{self.ticket["id"]}/retry')
            ticket = self.finish()
            failure = expect_error(lambda: c.request("POST", f'/tickets/{self.ticket["id"]}/retry'), "retry_state")
            require(retried["attempt"] == 2, "walkthrough_attempt", "Retry attempt was not incremented")
            return {"ticket": ticket, "failure": failure}
        if name == "recovery":
            claimed = self.app.tickets.claim()
            self.close()
            with Application(self.settings, start_workers=False) as restarted:
                recovered = restarted.tickets.get(claimed["id"])
                require(recovered["state"] == "failed" and recovered["error"]["error"] == "worker_interrupted",
                        "walkthrough_recovery", "Interrupted render was not recovered")
                restarted.tickets.retry(claimed["id"])
                finished = restarted.renderer.run_once()
                require(finished["state"] == "succeeded", "walkthrough_recovery", "Recovered render could not retry")
                return {"interrupted": recovered, "retried_state": finished["state"]}
        if name == "resources":
            c.request("POST", f"/assets/{a}/retire")
            failure = expect_error(lambda: c.create_reel("退役来源", self.slices), "asset_retired")
            ticket = self.finish()
            sweep = c.request("POST", "/resources/sweep", {"dry_run":False})
            status = c.request("GET", "/resources")
            require(status["consistent"] and not sweep["removed"], "walkthrough_resources", "Referenced source was collected")
            return {"reconcile":status,"sweep":sweep,"render_state":ticket["state"],"failure":failure}
        if name == "matrix":
            body = {"reel":self.reel["id"],"looks":[{"id":"preview"},{"id":"listening"}]}
            matrix = c.request("POST", "/matrices", body, {"Idempotency-Key":"demo-matrix"})
            before = len(c.request("GET", "/tickets")["items"])
            failure = expect_error(lambda: c.request("POST", "/matrices", {**body,"looks":[{"id":"preview"},{"id":"missing-look"}]}), "not_found")
            require(len(c.request("GET", "/tickets")["items"]) == before, "walkthrough_matrix", "Invalid matrix committed partial work")
            while self.app.renderer.run_once() is not None:
                pass
            finished = c.request("GET", "/matrices/"+matrix["id"])
            require(finished["state"] == "succeeded", "walkthrough_matrix", "Matrix did not complete")
            return {"matrix": finished,"failure":failure}
        if name == "conformance":
            ticket = self.finish()
            artifact = ticket["artifacts"][0]
            result = c.request("GET", f'/artifacts/{artifact["id"]}/conformance')
            require(result["valid"], "walkthrough_conformance", "Rendered media violates frozen look", report=result)
            failure = expect_error(lambda: c.request("GET", "/artifacts/missing/conformance"), "not_found")
            return {"conformance":result,"failure":failure}
        if name == "lineage":
            self.finish()
            imprint = c.imprint(self.ticket["id"])
            result = c.request("GET", f'/tickets/{self.ticket["id"]}/lineage')
            require(any(n["id"] == imprint["id"] for n in result["nodes"]), "walkthrough_lineage", "Imprint missing from lineage")
            failure = expect_error(lambda: c.request("GET", "/tickets/missing/lineage"), "not_found")
            return {"lineage":result,"failure":failure}
        if name == "redaction":
            asset_id = self.asset["id"]
            document = {"redactions": [
                {"label": "固定角标", "start": 0.3, "end": 1.4,
                 "frames": [{"time": 0.3, "x": 0.10, "y": 0.10, "width": 0.25, "height": 0.25}]},
                {"label": "移动敏感区", "start": 0.2, "end": 2.8,
                 "frames": [{"time": 0.2, "x": 0.05, "y": 0.55, "width": 0.18, "height": 0.25},
                            {"time": 2.8, "x": 0.72, "y": 0.55, "width": 0.18, "height": 0.25}]}]}
            redset = c.request("PUT", f"/assets/{asset_id}/redactions", document, {"If-Match": "0"})
            require(redset["version"] == 1 and len(redset["document"]["redactions"]) == 2,
                    "walkthrough_redaction", "Redaction set was not versioned")
            stale = expect_error(lambda: c.request("PUT", f"/assets/{asset_id}/redactions", document,
                                                   {"If-Match": "0"}), "revision_conflict")
            preview = c.request("POST", f"/assets/{asset_id}/redactions/preview", {"version": 1})
            jpeg = c.request("GET", "/artifacts/"+preview["id"]+"/content", raw=True)
            require(jpeg.startswith(b"\xff\xd8") and preview["samples"],
                    "walkthrough_redaction", "Preview must be a JPEG covering every redaction window")
            # 同一来源在卷内出现两次；引用该来源的镜段都必须独立产生遮挡覆盖。
            covered = self.app.redactions.coverage(asset_id, 1, self.reel["document"]["slices"])
            require(len(covered) >= 2 and any(item["slice"] == 1 for item in covered),
                    "walkthrough_redaction",
                    "Every slice using the source must independently carry the redaction", count=len(covered))
            # 音频版式承载不了画面遮挡，入队前全有全无拒绝。
            audio_reject = expect_error(
                lambda: c.request("POST", "/tickets",
                                  {"reel": self.reel["id"], "look": "listening",
                                   "redactions": {asset_id: 1}}), "redaction_audio")
            ticket = c.request("POST", "/tickets",
                               {"reel": self.reel["id"], "look": "preview", "redactions": {asset_id: 1}})
            require(ticket["snapshot"]["redaction_sets"] == [
                {"asset": asset_id, "version": 1, "id": redset["id"], "hash": redset["hash"]}],
                "walkthrough_redaction", "The queued ticket must freeze the redaction version")
            # 先排空 FIFO 队列中 setup 阶段排队的既有渲染单，再执行本工作流自己的遮挡单。
            while True:
                claimed = self.app.tickets.claim()
                if claimed is None or claimed["id"] == ticket["id"]:
                    break
                self.app.renderer.execute(claimed)
            require(claimed is not None and claimed["id"] == ticket["id"], "walkthrough_redaction",
                    "The redaction ticket must keep its own frozen place in the queue")
            self.app.renderer.execute(claimed)
            finished = self.app.tickets.get(ticket["id"])
            require(finished["state"] == "succeeded", "walkthrough_redaction",
                    "Redacted render did not succeed", ticket=finished)
            artifact = finished["artifacts"][0]
            require(artifact["metadata"].get("redactions_applied") == len(covered),
                    "walkthrough_redaction", "Committed rendition must record applied coverage")
            conformance = c.request("GET", f'/artifacts/{artifact["id"]}/conformance')
            require(conformance["valid"], "walkthrough_redaction", "Redacted rendition failed full decode")
            imprint = c.imprint(finished["id"])
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            require(verified["valid"], "walkthrough_redaction", "Redacted imprint must verify")
            lineage = c.request("GET", f'/tickets/{finished["id"]}/lineage')
            require(any(n["kind"] == "redaction_set" for n in lineage["nodes"]),
                    "walkthrough_redaction", "Lineage must record the frozen redaction set")
            # 修改遮挡形成新版本；已冻结渲染单的指纹与遮挡版本保持不变。
            changed = {"redactions": [document["redactions"][0]]}
            version_two = c.request("PUT", f"/assets/{asset_id}/redactions", changed, {"If-Match": "1"})
            require(version_two["version"] == 2 and finished["snapshot"]["redaction_sets"][0]["version"] == 1,
                    "walkthrough_redaction", "Editing redactions must not move a frozen ticket")
            return {"redaction_set": redset, "preview_samples": len(preview["samples"]),
                    "coverage": len(covered), "ticket": finished["id"],
                    "redactions_applied": artifact["metadata"]["redactions_applied"],
                    "failures": [stale, audio_reject]}
        raise ValueError("Unknown walkthrough: " + name)


def main():
    parser = argparse.ArgumentParser(description="Run a bounded audiovisual production walkthrough")
    parser.add_argument("--workflow", choices=WORKFLOWS, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reel-loom-demo-") as folder:
        settings = replace(Settings.from_env(), root=Path(folder), render_timeout=30)
        demo = Walkthrough(settings)
        try:
            demo.setup()
            result = demo.run(args.workflow)
            print(json.dumps({"workflow":args.workflow,"ok":True,"result":result}, ensure_ascii=False))
        finally:
            if args.workflow != "recovery":
                demo.close()


if __name__ == "__main__":
    main()
