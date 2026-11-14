import json
from .errors import require, missing, revision_conflict
from .timebase import ticks, intersect
from .values import identifier, now, canonical, digest, object_body, text, number, integer

# 遮挡以来源归一化坐标（0..1）与来源时间（秒输入、微秒存储）表达。
# 版本是完整清单快照：写入后不可变，渲染单在入队时冻结具体版本。
MAX_REDACTIONS = 16
MAX_KEYFRAMES = 32


class Redactions:
    def __init__(self, db, assets):
        self.db, self.assets = db, assets

    def _frame(self, raw, index):
        object_body(raw, ("time", "x", "y", "width", "height"), ("time", "x", "y", "width", "height"))
        frame = {"time_tick": ticks(raw["time"], "redaction frame time"),
                 "x": number(raw["x"], "x", 0, 1), "y": number(raw["y"], "y", 0, 1),
                 "width": number(raw["width"], "width", 0, 1),
                 "height": number(raw["height"], "height", 0, 1)}
        require(frame["x"] + frame["width"] <= 1 + 1e-9 and frame["y"] + frame["height"] <= 1 + 1e-9,
                "redaction_bounds", "A redaction rectangle must stay inside the source frame", index=index)
        require(frame["width"] > 0 and frame["height"] > 0, "redaction_bounds",
                "A redaction rectangle needs positive size", index=index)
        return frame

    def normalize(self, raw, asset):
        object_body(raw, ("label", "redactions"), ("redactions",))
        entries = raw["redactions"]
        require(isinstance(entries, list) and 1 <= len(entries) <= MAX_REDACTIONS, "redaction_count",
                f"Expected 1..{MAX_REDACTIONS} redaction entries")
        duration = asset["metadata"]["duration_tick"]
        document = []
        for index, item in enumerate(entries):
            object_body(item, ("label", "start", "end", "frames", "fill"), ("start", "end", "frames"))
            label = text(item.get("label", f"redaction-{index+1}"), "redaction label", 120)
            fill = item.get("fill", "black")
            require(fill in ("black",), "redaction_fill", "Unsupported redaction fill", index=index)
            start = ticks(item["start"], "redaction start")
            end = ticks(item["end"], "redaction end")
            require(0 <= start < end <= duration, "redaction_time",
                    "Redaction window must lie inside the source media", index=index)
            frames = item["frames"]
            require(isinstance(frames, list) and 1 <= len(frames) <= MAX_KEYFRAMES, "redaction_frames",
                    f"Expected 1..{MAX_KEYFRAMES} key frames", index=index)
            normalized = [self._frame(frame, index) for frame in frames]
            for frame in normalized:
                require(start <= frame["time_tick"] <= end, "redaction_time",
                        "Key frame time must stay inside its redaction window", index=index)
            times = [frame["time_tick"] for frame in normalized]
            require(all(left < right for left, right in zip(times, times[1:])),
                    "redaction_time", "Key frame times must be strictly ordered", index=index)
            document.append({"id": identifier("redact"), "label": label, "fill": fill,
                             "start_tick": start, "end_tick": end, "frames": normalized})
        return {"asset": asset["id"], "schema": 1, "redactions": document}

    def put(self, asset_id, raw, expected):
        integer(expected, "expected_version", 0)
        with self.db.write_unit() as db:
            asset = self.assets.get(asset_id, db, ready=True)
            require(asset["metadata"]["has_video"], "video_required",
                    "Time-based visual redactions require a video source", 422)
            head = db.execute("SELECT MAX(version) FROM redaction_sets WHERE asset=?", (asset_id,)).fetchone()[0] or 0
            if head != expected:
                raise revision_conflict(expected, head)
            document = self.normalize(raw, asset)
            version = head + 1
            redaction_id = identifier("redset")
            db.execute("INSERT INTO redaction_sets VALUES(?,?,?,?,?,?)",
                       (redaction_id, asset_id, version, canonical(document), digest(document), now()))
            self.db.event(db, asset_id, "redactions_written",
                          {"set": redaction_id, "version": version, "count": len(document["redactions"]),
                           "hash": digest(document)})
            return self.get(asset_id, version, db)

    def get(self, asset_id, version=None, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(asset_id, version, read)
        self.assets.get(asset_id, db=db)
        if version is None:
            row = db.execute("SELECT * FROM redaction_sets WHERE asset=? ORDER BY version DESC LIMIT 1",
                             (asset_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM redaction_sets WHERE asset=? AND version=?",
                             (asset_id, integer(version, "version", 1))).fetchone()
        if row is None:
            raise missing("redaction set", f"{asset_id}@{version}")
        return {"id": row["id"], "asset": asset_id, "version": row["version"],
                "hash": row["hash"], "document": json.loads(row["document"]), "created": row["created"]}

    def history(self, asset_id):
        with self.db.read() as db:
            self.assets.get(asset_id, db=db)
            rows = db.execute("SELECT version,hash,created,document FROM redaction_sets "
                              "WHERE asset=? ORDER BY version DESC LIMIT 100", (asset_id,))
            return [{"version": row["version"], "hash": row["hash"], "created": row["created"],
                     "redactions": len(json.loads(row["document"])["redactions"])} for row in rows]

    def coverage(self, asset_id, version, slices):
        """把来源级遮挡投影到卷内所有引用该来源的镜段（输出时间轴，微秒）。"""
        redset = self.get(asset_id, version)
        covered = []
        for entry in redset["document"]["redactions"]:
            for index, item in enumerate(slices):
                if item["asset"] != asset_id:
                    continue
                overlap = intersect(entry["start_tick"], entry["end_tick"],
                                    item["in_tick"], item["out_tick"])
                if overlap is None:
                    continue
                source_start, source_end = overlap
                offset = item["at_tick"] - item["in_tick"]
                covered.append({"slice": index, "asset": asset_id, "redaction": entry["id"], "label": entry["label"],
                                "fill": entry["fill"],
                                "source_start_tick": source_start, "source_end_tick": source_end,
                                "output_start_tick": source_start + offset,
                                "output_end_tick": source_end + offset,
                                "frames": entry["frames"]})
        covered.sort(key=lambda x: (x["output_start_tick"], x["redaction"]))
        return covered
