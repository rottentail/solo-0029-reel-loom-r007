from fractions import Fraction
import av
from .errors import require
from .timebase import seconds, TICKS_PER_SECOND
from .redaction_preview import interpolate, fill_rectangle


class Redactor:
    """对已按版式编码的成片逐帧烧入来源级时域遮挡。

    遮挡矩形以来源归一化坐标表达，这里按版式的 contain/cover 几何映射到输出像素，
    因此缩放、裁切或目标尺寸变化都不会让遮挡偏离指定的来源画面区域。
    """

    def __init__(self, settings):
        self.settings = settings

    def has_video_redactions(self, snapshot):
        return snapshot["look"]["spec"]["kind"] == "video" and bool(snapshot.get("redactions"))

    def apply(self, snapshot, base_path, output_path):
        coverage = snapshot["redactions"]
        spec = snapshot["look"]["spec"]
        out_width, out_height = spec["width"], spec["height"]
        with av.open(str(base_path), options={"protocol_whitelist": "file,pipe"}) as source:
            video_stream = next((stream for stream in source.streams if stream.type == "video"), None)
            audio_stream = next((stream for stream in source.streams if stream.type == "audio"), None)
            require(video_stream is not None, "redaction_render", "Redaction target has no video stream")
            with av.open(str(output_path), "w", options={"movflags": "+faststart"}) as target:
                video_out = target.add_stream("libx264", rate=Fraction(spec["frame_rate"]))
                video_out.width, video_out.height = out_width, out_height
                video_out.pix_fmt = "yuv420p"
                video_out.time_base = video_stream.time_base
                video_out.options = {"preset": "veryfast", "crf": str(spec["crf"])}
                audio_out = None
                if audio_stream is not None:
                    audio_out = target.add_stream("aac", rate=spec["sample_rate"])
                    audio_out.time_base = audio_stream.time_base
                decoded_frames = 0
                for packet in source.demux(video_stream, *([audio_stream] if audio_stream else [])):
                    for frame in packet.decode():
                        if packet.stream.type == "video":
                            self._redact_frame(frame, coverage)
                            for encoded in video_out.encode(frame):
                                target.mux(encoded)
                            decoded_frames += 1
                        elif audio_out is not None:
                            for encoded in audio_out.encode(frame):
                                target.mux(encoded)
                for encoded in video_out.encode(None):
                    target.mux(encoded)
                if audio_out is not None:
                    for encoded in audio_out.encode(None):
                        target.mux(encoded)
        require(decoded_frames > 0, "redaction_render", "Redaction pass decoded no video frames")

    def _redact_frame(self, frame, coverage):
        output_second = float(frame.pts * frame.time_base) if frame.pts is not None else 0.0
        for item in coverage:
            start = seconds(item["output_start_tick"])
            end = seconds(item["output_end_tick"])
            if not (start <= output_second < end):
                continue
            source_second = seconds(item["source_start_tick"]) + (output_second - start)
            source_tick = round(source_second * TICKS_PER_SECOND)
            x, y, width, height = interpolate(item["frames"], source_tick)
            rectangle = self._map_rectangle(item, x, y, width, height, frame.width, frame.height)
            if rectangle is not None:
                fill_rectangle(frame, *rectangle)

    def _map_rectangle(self, item, x, y, width, height, frame_width, frame_height):
        source = self._source_metadata(item)
        source_width, source_height = source["width"], source["height"]
        fit = item.get("fit", "contain")
        left = x * source_width
        right = (x + width) * source_width
        top = y * source_height
        bottom = (y + height) * source_height
        if fit == "contain":
            scale = min(frame_width / source_width, frame_height / source_height)
            rendered_width, rendered_height = source_width * scale, source_height * scale
            offset_x, offset_y = (frame_width - rendered_width) / 2, (frame_height - rendered_height) / 2
            points = ((left, top), (right, top), (left, bottom), (right, bottom))
            xs = [offset_x + px * scale for px, _ in points]
            ys = [offset_y + py * scale for _, py in points]
            return min(xs), min(ys), max(xs), max(ys)
        scale = max(frame_width / source_width, frame_height / source_height)
        rendered_width, rendered_height = source_width * scale, source_height * scale
        crop_x, crop_y = (rendered_width - frame_width) / 2, (rendered_height - frame_height) / 2
        points = ((left, top), (right, top), (left, bottom), (right, bottom))
        xs = [px * scale - crop_x for px, _ in points]
        ys = [py * scale - crop_y for _, py in points]
        x0, x1 = max(0, min(xs)), min(frame_width, max(xs))
        y0, y1 = max(0, min(ys)), min(frame_height, max(ys))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _source_metadata(self, item):
        return item["source_metadata"]
