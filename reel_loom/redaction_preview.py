import av
from .errors import require
from .timebase import seconds
from .values import object_body, integer


def interpolate(frames, time_tick):
    """按关键帧线性插值得到某来源时刻的归一化矩形。"""
    if time_tick <= frames[0]["time_tick"]:
        frame = frames[0]
        return frame["x"], frame["y"], frame["width"], frame["height"]
    if time_tick >= frames[-1]["time_tick"]:
        frame = frames[-1]
        return frame["x"], frame["y"], frame["width"], frame["height"]
    for left, right in zip(frames, frames[1:]):
        if left["time_tick"] <= time_tick <= right["time_tick"]:
            ratio = ((time_tick - left["time_tick"]) /
                     (right["time_tick"] - left["time_tick"]))
            return tuple(left[field] + (right[field] - left[field]) * ratio
                         for field in ("x", "y", "width", "height"))


def fill_rectangle(frame, x0, y0, x1, y1):
    """在 yuv420p 平面上以不透明黑填充像素矩形，坐标按来源像素取整并夹取。"""
    width, height = frame.width, frame.height
    px0 = max(0, min(width, int(x0)))
    px1 = max(0, min(width, int(x1)))
    py0 = max(0, min(height, int(y0)))
    py1 = max(0, min(height, int(y1)))
    if px1 <= px0 or py1 <= py0:
        return
    y_plane, u_plane, v_plane = (memoryview(plane) for plane in frame.planes)
    y_stride = frame.planes[0].line_size
    for row in range(py0, py1):
        offset = row * y_stride
        y_plane[offset+px0:offset+px1] = b"\x00" * (px1 - px0)
    cx0, cx1 = px0 // 2, (px1 + 1) // 2
    cy0, cy1 = py0 // 2, (py1 + 1) // 2
    chroma_stride = frame.planes[1].line_size
    for row in range(cy0, cy1):
        offset = row * chroma_stride
        u_plane[offset+cx0:offset+cx1] = b"\x80" * (cx1 - cx0)
        v_plane[offset+cx0:offset+cx1] = b"\x80" * (cx1 - cx0)


class RedactionPreviews:
    def __init__(self, db, assets, blobs, processes, artifacts, redactions):
        self.db, self.assets, self.blobs = db, assets, blobs
        self.processes, self.artifacts, self.redactions = processes, artifacts, redactions

    def _sample_times(self, entries):
        samples = []
        for entry in entries:
            start, end = entry["start_tick"], entry["end_tick"]
            points = [start + (end - start) * index // 4 for index in range(5)]
            for time_tick in points:
                samples.append((time_tick, entry))
        # 去重并按来源时间排序，保证接触表从早到晚可读。
        unique = {}
        for time_tick, entry in samples:
            unique.setdefault(time_tick, []).append(entry)
        return sorted(unique.items())

    def build(self, asset_id, body):
        object_body(body, ("version", "cell_width", "columns"))
        cell_width = integer(body.get("cell_width", 240), "cell_width", 64, 640)
        columns = integer(body.get("columns", 3), "columns", 1, 6)
        version = body.get("version")
        redset = self.redactions.get(asset_id, version)
        asset = self.assets.get(asset_id, ready=True)
        require(asset["metadata"]["has_video"], "video_required", "Preview requires a video source")
        entries = redset["document"]["redactions"]
        samples = self._sample_times(entries)
        source_path = self.blobs.resolve(asset["blob"], verify=True)
        source_width, source_height = self._source_dimensions(asset)
        cell_height = max(2, round(cell_width * source_height / source_width / 2) * 2)
        rows = (len(samples) + columns - 1) // columns
        sheet_width, sheet_height = columns * cell_width, rows * cell_height
        frames_taken = self._render_cells(source_path, samples, cell_width, cell_height)
        require(len(frames_taken) == len(samples), "preview_frames",
                "Could not decode every requested preview frame",
                expected=len(samples), actual=len(frames_taken))
        with self.blobs.temporary(".jpg") as output:
            self._compose_jpeg(frames_taken, columns, sheet_width, sheet_height,
                               cell_width, cell_height, output)
            metadata = {"kind": "redaction_preview", "asset": asset_id, "version": redset["version"],
                        "redaction_set": redset["id"], "columns": columns, "rows": rows,
                        "cell_width": cell_width, "cell_height": cell_height,
                        "sample_seconds": [seconds(time_tick) for time_tick, _ in samples],
                        "source_blob": asset["blob"], "mime": "image/jpeg"}
            artifact = self.artifacts.add(asset_id, "redaction_preview", output, metadata)
        return {**artifact, "samples": [{"source_second": seconds(time_tick),
                                         "labels": [entry["label"] for entry in active]}
                                        for time_tick, active in samples]}

    def _source_dimensions(self, asset):
        from .probe import select_stream
        video = select_stream(asset["metadata"], "video")
        return video["width"], video["height"]

    def _decode_at(self, container, video_stream, target_tick):
        target_second = seconds(target_tick)
        container.seek(int(target_second / video_stream.time_base), stream=video_stream)
        best = None
        for frame in container.decode(video_stream):
            if frame.pts is None:
                continue
            current = float(frame.pts * frame.time_base)
            if current <= target_second + 1e-9:
                best = frame
            if current >= target_second:
                return frame
        require(best is not None, "preview_frames", "No decodable frame near the requested time")
        return best

    def _render_cells(self, source_path, samples, cell_width, cell_height):
        cells = []
        with av.open(str(source_path), options={"protocol_whitelist": "file,pipe"}) as container:
            video_stream = container.streams.video[0]
            for time_tick, entries in samples:
                frame = self._decode_at(container, video_stream, time_tick)
                frame = frame.reformat(format="yuv420p")
                for entry in entries:
                    x, y, width, height = interpolate(entry["frames"], time_tick)
                    fill_rectangle(frame, x * frame.width, y * frame.height,
                                   (x + width) * frame.width, (y + height) * frame.height)
                cell = frame.reformat(width=cell_width, height=cell_height, format="yuv420p")
                cells.append(cell)
        return cells

    def _compose_jpeg(self, cells, columns, sheet_width, sheet_height, cell_width, cell_height, output):
        sheet = av.VideoFrame(sheet_width, sheet_height, "yuv420p")
        planes = [memoryview(plane) for plane in sheet.planes]
        strides = [plane.line_size for plane in sheet.planes]
        for plane in planes:
            plane[:] = b"\x10" * len(plane)
        for index, cell in enumerate(cells):
            row, column = divmod(index, columns)
            top, left = row * cell_height, column * cell_width
            self._paste(planes, strides, cell, left, top, cell_width, cell_height)
        jpeg_frame = sheet.reformat(format="yuvj420p")
        with av.open(str(output), "w", format="mjpeg") as container:
            stream = container.add_stream("mjpeg")
            stream.width, stream.height, stream.pix_fmt = sheet_width, sheet_height, "yuvj420p"
            stream.options = {"q:v": "3"}
            for packet in stream.encode(jpeg_frame) + stream.encode(None):
                container.mux(packet)

    def _paste(self, planes, strides, cell, left, top, width, height):
        cell_planes = [memoryview(plane) for plane in cell.planes]
        cell_strides = [plane.line_size for plane in cell.planes]
        for plane_index, (plane, cell_plane) in enumerate(zip(planes, cell_planes)):
            scale = 1 if plane_index == 0 else 2
            dst_stride, src_stride = strides[plane_index], cell_strides[plane_index]
            for row in range(height // scale):
                dst = (top // scale + row) * dst_stride + left // scale
                src = row * src_stride
                plane[dst:dst + width // scale] = cell_plane[src:src + width // scale]
