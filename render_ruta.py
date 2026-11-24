"""Renderiza un clip vertical ilustrado a partir de una ruta GeoJSON local."""

import argparse
from bisect import bisect_right
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    reel_scripts = Path(__file__).resolve().parents[2] / "editar-reels-codex" / "scripts"
    if (reel_scripts / "_arranque.py").is_file():
        sys.path.insert(0, str(reel_scripts))
        import _arranque  # Usa el intérprete ya preparado por configurar_equipo.py.

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as error:
    if error.name == "PIL":
        raise SystemExit("Activa VideosAgent o ejecuta el script con el Python del kit que incluye Pillow.") from None
    raise


@dataclass
class Scene:
    title: str
    address: str
    origin: str
    credits: list[str]
    steps: list[tuple[int, str]]
    route: list[tuple[float, float]]
    target: tuple[float, float]
    layers: list[tuple[str, list[tuple[float, float]]]]
    demo: bool


def coordinate(value):
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("Se espera una posición GeoJSON [longitud, latitud].")
    lon, lat = value[:2]
    if (type(lon) not in (int, float) or type(lat) not in (int, float)
            or not (-180 <= lon <= 180 and -90 <= lat <= 90)):
        raise ValueError("Hay una longitud o latitud fuera de rango.")
    return float(lon), float(lat)


def line_coordinates(values, minimum=2):
    if not isinstance(values, list) or len(values) < minimum:
        raise ValueError("La geometría tiene menos posiciones de las necesarias.")
    return [coordinate(value) for value in values]


def clean_text(value, field, limit=140):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Completa el campo {field} con hasta {limit} caracteres.")
    result = " ".join(value.split())
    if not result.isprintable():
        raise ValueError(f"El campo {field} contiene caracteres de control.")
    return result


def text_items(value, field, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} admite hasta {maximum} líneas de texto.")
    return [clean_text(item, field) for item in value]


def load_scene(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Recorta las capas al área del anuncio. El GeoJSON admite hasta 8 MiB.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("La entrada debe ser una FeatureCollection GeoJSON.")
    meta, features = data.get("metadata"), data.get("features")
    if not isinstance(meta, dict) or not isinstance(features, list):
        raise ValueError("Faltan metadata o features.")
    demo = meta.get("demo") is True
    if not demo and (meta.get("verified") is not True or meta.get("commercial_use") is not True):
        raise ValueError("Registro pendiente de verificación de la ruta o de sus derechos comerciales.")
    clean_text(meta.get("source"), "metadata.source", 240)
    clean_text(meta.get("license"), "metadata.license", 120)
    credits = text_items(meta.get("attribution"), "metadata.attribution", 3)
    if not credits:
        raise ValueError("Indica la atribución que debe quedar visible.")
    route, target, layers = None, None, []
    for item in features:
        if not isinstance(item, dict) or item.get("type") != "Feature":
            raise ValueError("Cada elemento de features debe ser un Feature.")
        props, geom = item.get("properties"), item.get("geometry")
        if not isinstance(props, dict) or not isinstance(geom, dict):
            raise ValueError("Cada Feature necesita properties y geometry.")
        role, kind = props.get("role"), geom.get("type")
        values = geom.get("coordinates")
        if role == "route" and kind == "LineString" and route is None:
            route = line_coordinates(values)
        elif role == "destination" and kind == "Point" and target is None:
            target = coordinate(values)
        elif role == "road" and kind == "LineString":
            layers.append((role, line_coordinates(values)))
        elif role in ("park", "water") and kind == "Polygon" and isinstance(values, list) and len(values) == 1:
            outline = line_coordinates(values[0], 4)
            if outline[0] != outline[-1]:
                raise ValueError("Cierra el anillo de cada polígono de contexto.")
            layers.append((role, outline))
        else:
            raise ValueError(f"Capa duplicada o combinación de role/geometría no admitida: {role} / {kind}.")
    if route is None or target is None or len(set(route)) < 2:
        raise ValueError("Proporciona una ruta con longitud y un punto de destino.")
    directions = meta.get("directions", [])
    if not isinstance(directions, list) or len(directions) > 5:
        raise ValueError("metadata.directions admite hasta cinco indicaciones verificadas.")
    steps = []
    for step in directions:
        vertex = step.get("vertex") if isinstance(step, dict) else None
        if (type(vertex) is not int or not 0 <= vertex < len(route)
                or (steps and vertex <= steps[-1][0])):
            raise ValueError("Cada indicación debe tener un vertex válido y estrictamente creciente.")
        steps.append((vertex, clean_text(step.get("text"), "metadata.directions.text")))
    return Scene(
        clean_text(meta.get("title"), "metadata.title", 70),
        clean_text(meta.get("address"), "metadata.address"),
        clean_text(meta.get("origin"), "metadata.origin", 75),
        credits, steps,
        route, target, layers, demo,
    )


def typography(size, bold=False):
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [windows / ("segoeuib.ttf" if bold else "segoeui.ttf"),
                  "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
                  "/System/Library/Fonts/Supplemental/Arial.ttf"]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def wrapped(draw, value, font, width):
    rows, row = [], ""
    for word in value.split():
        if draw.textlength(word, font=font) > width:
            if row:
                rows.append(row)
                row = ""
            piece = ""
            for char in word:
                if draw.textlength(piece + char, font=font) > width:
                    rows.append(piece)
                    piece = ""
                piece += char
            row = piece
        elif row and draw.textlength(row + " " + word, font=font) > width:
            rows.append(row)
            row = word
        else:
            row = f"{row} {word}".strip()
    return rows + ([row] if row else [])


def fitted_text(draw, value, width, max_rows, size, *, bold=False):
    for size_candidate in range(size, max(9, size - 6), -1):
        font = typography(size_candidate, bold)
        rows = wrapped(draw, value, font, width)
        if len(rows) <= max_rows:
            return "\n".join(rows), font
    raise ValueError(f"Acorta el rótulo para que se lea en vertical: {value}")


def ease(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


class Clip:
    def __init__(self, scene, width, height):
        self.scene, self.width, self.height = scene, width, height
        self.scale = width / 720
        px = self.px
        self.margin = px(62)
        self.small = typography(px(19), True)
        self.label = typography(px(24), True)
        self.frame_base = self.make_base()

        start_lon = scene.route[0][0]
        mid_lat = sum(point[1] for point in scene.route) / len(scene.route)
        correction = max(math.cos(math.radians(mid_lat)), 0.00001)

        def project(point):
            lon, lat = point
            return (((lon - start_lon + 180) % 360 - 180) * correction, -lat)

        anchors = [project(point) for point in scene.route + [scene.target]]
        min_x, max_x = min(p[0] for p in anchors), max(p[0] for p in anchors)
        min_y, max_y = min(p[1] for p in anchors), max(p[1] for p in anchors)
        center_x, center_y = (min_x + max_x) / 2, (min_y + max_y) / 2
        _, _, w, h = self.panel
        scale = min((w - px(124)) / max(max_x - min_x, 1e-8),
                    (h - px(136)) / max(max_y - min_y, 1e-8))

        def transform(point):
            x, y = project(point)
            return w / 2 + (x - center_x) * scale, h / 2 + (y - center_y) * scale

        self.route = [transform(point) for point in scene.route]
        self.target = transform(scene.target)
        self.distance = [0.0]
        for start, end in zip(self.route, self.route[1:]):
            self.distance.append(self.distance[-1] + math.dist(start, end))
        if not self.distance[-1]:
            raise ValueError("El trazado proyectado no tiene longitud.")
        self.basemap = self.make_basemap(w, h, transform)
        step_area = ImageDraw.Draw(self.frame_base)
        self.step_labels = [fitted_text(step_area, text, width - 2 * self.margin - px(36), 2, px(23))
                            for _, text in scene.steps]
        self.step_positions = [self.distance[vertex] for vertex, _ in scene.steps]
        self.origin_label = fitted_text(step_area, f"Desde {scene.origin}", width - 2*self.margin - px(36), 2, px(23))

    def px(self, value):
        return max(1, round(value * self.scale))

    def make_basemap(self, width, height, transform):
        image = Image.new("RGB", (width, height), "#e7ede6")
        draw = ImageDraw.Draw(image)
        for pos in range(self.px(40), width, self.px(46)):
            draw.line((pos, 0, pos, height), fill="#d9e4da", width=1)
        for pos in range(self.px(40), height, self.px(46)):
            draw.line((0, pos, width, pos), fill="#d9e4da", width=1)
        for role, points in self.scene.layers:
            if role != "road":
                draw.polygon([transform(p) for p in points],
                             fill="#cddfc8" if role == "park" else "#b5dce0")
        for role, points in self.scene.layers:
            if role == "road":
                road = [transform(p) for p in points]
                draw.line(road, fill="#c5d1c3", width=self.px(13), joint="curve")
                draw.line(road, fill="#fbfaf4", width=self.px(9), joint="curve")
        return image

    def make_base(self):
        scene, px, left = self.scene, self.px, self.margin
        frame = Image.new("RGB", (self.width, self.height), "#102825")
        draw = ImageDraw.Draw(frame)
        suffix = " · DEMO FICTICIA" if scene.demo else ""
        draw.text((left, px(167)), "CÓMO LLEGAR" + suffix, "#9be1bc", font=self.small)
        title, font = fitted_text(draw, scene.title, self.width - left * 2, 2, px(46), bold=True)
        title_start = (left, px(205))
        draw.multiline_text(title_start, title, "#fff9e8", font=font, spacing=px(3))
        _, _, _, title_bottom = draw.multiline_textbbox(title_start, title, font=font, spacing=px(3))
        address, font = fitted_text(draw, scene.address, self.width - left * 2, 2, px(25))
        address_start = (left, title_bottom + px(12))
        _, _, _, address_bottom = draw.multiline_textbbox(address_start, address, font=font, spacing=px(3))
        panel_top = max(px(365), address_bottom + px(16))
        if panel_top > px(435):
            raise ValueError("Acorta el nombre o la dirección para dejar espacio al mapa.")
        draw.multiline_text(address_start, address, "#c5e5d9", font=font, spacing=px(3))
        self.panel = (left, panel_top, self.width - 2 * left, px(894) - panel_top)
        credits = []
        for note in scene.credits:
            credits.extend(wrapped(draw, note, self.small, self.width - left * 2))
        if len(credits) > 4:
            raise ValueError("Acorta las líneas de atribución conservando el crédito y la URL requeridos.")
        draw.multiline_text((left, px(991)), "\n".join(credits), "#e0e7dd", font=self.small, spacing=px(3))
        return frame

    def position(self, progress):
        length = self.distance[-1] * progress
        edge = min(len(self.route) - 2, bisect_right(self.distance, length) - 1)
        fraction = (length - self.distance[edge]) / max(1e-10, self.distance[edge + 1] - self.distance[edge])
        start, end = self.route[edge:edge + 2]
        point = tuple(a + (b - a) * fraction for a, b in zip(start, end))
        return self.route[:edge + 1] + [point], point, length

    def frame(self, relative_time):
        px = self.px
        left, top, width, height = self.panel
        travel = ease((relative_time - .11) / .66)
        zoom = ease((relative_time - .68) / .21) * .64
        target_x, target_y = self.target
        view_w, view_h = width / (1 + zoom), height / (1 + zoom)
        l = min(width - view_w, max(0, (width - view_w) / 2 + (target_x - width / 2) * zoom))
        t = min(height - view_h, max(0, (height - view_h) / 2 + (target_y - height / 2) * zoom))
        tile = self.basemap.transform((width, height), Image.Transform.EXTENT, (l, t, l + view_w, t + view_h),
                                      resample=Image.Resampling.BILINEAR)
        map_draw = ImageDraw.Draw(tile)

        def camera(p):
            return (p[0] - l) * (1 + zoom), (p[1] - t) * (1 + zoom)

        travelled, current, length = self.position(travel)
        route = [camera(p) for p in self.route]
        path = [camera(p) for p in travelled]
        map_draw.line(route, fill="#ffffff", width=px(16), joint="curve")
        map_draw.line(route, fill="#b7cfc1", width=px(8), joint="curve")
        map_draw.line(path, fill="#066753", width=px(11), joint="curve")
        sx, sy = route[0]
        map_draw.ellipse((sx-px(8), sy-px(8), sx+px(8), sy+px(8)), fill="white", outline="#066753", width=px(3))
        if travel < .999:
            sx, sy = camera(current)
            map_draw.ellipse((sx-px(9), sy-px(9), sx+px(9), sy+px(9)), fill="#fbda76", outline="#15372a", width=px(3))
        dx, dy = camera(self.target)
        pin_radius = px(14 + 7 * zoom)
        map_draw.polygon([(dx - pin_radius*.70, dy - pin_radius*.82), (dx, dy),
                          (dx + pin_radius*.70, dy - pin_radius*.82)], fill="#b73c2a")
        map_draw.ellipse((dx-pin_radius, dy-pin_radius*2.32, dx+pin_radius, dy-pin_radius*.32),
                         fill="#b73c2a", outline="#fff9e8", width=px(2))
        pin_center = dy-pin_radius*1.32
        map_draw.ellipse((dx-px(5), pin_center-px(5), dx+px(5), pin_center+px(5)), fill="#fff9e8")
        if relative_time > .77:
            name, font = "Destino", self.small
            label_w = round(map_draw.textlength(name, font=font)) + px(24)
            x = min(width - label_w - px(14), max(px(14), dx - label_w/2))
            y = min(height-px(54), max(px(14), dy - pin_radius*2 - px(42)))
            map_draw.rounded_rectangle((x, y, x+label_w, y+px(34)), radius=px(8), fill="#102825")
            map_draw.text((x+px(12), y+px(3)), name, fill="#fff9e8", font=font)
        map_draw.text((px(16), px(16)), "↑ N", font=self.small, fill="#15372a")

        image = self.frame_base.copy()
        image.paste(tile, (left, top))
        overlay = ImageDraw.Draw(image)
        if travel >= 1:
            label, font = "Destino", self.label
        else:
            step_index = bisect_right(self.step_positions, length) - 1
            label, font = self.step_labels[step_index] if step_index >= 0 and travel else self.origin_label
        overlay.multiline_text((left, px(924)), label, "#fff9e8", font=font, spacing=px(2))
        return image


def render(clip, output, seconds, fps):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("Se requiere el FFmpeg del kit en PATH.")
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Elige una nueva ruta de salida con extensión .mp4.")
    output.parent.mkdir(parents=True, exist_ok=True)
    total = round(seconds * fps)
    with tempfile.TemporaryDirectory(prefix=".ruta-", dir=output.parent) as temporary:
        video = Path(temporary) / "clip.mp4"
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s:v", f"{clip.width}x{clip.height}", "-r", str(fps), "-i", "pipe:0", "-an",
                "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(video)]
        with subprocess.Popen(args, stdin=subprocess.PIPE) as proc:
            try:
                for frame in range(total):
                    proc.stdin.write(clip.frame(frame / max(1, total - 1)).tobytes())
            except BrokenPipeError:
                raise ValueError("FFmpeg interrumpió el render. Revisa el error de codificación.") from None
            finally:
                proc.stdin.close()
            if proc.wait():
                raise ValueError("No se completó el render con FFmpeg.")
        with video.open("rb") as source:
            with output.open("xb") as target:
                try:
                    shutil.copyfileobj(source, target)
                except OSError:
                    target.close()
                    output.unlink(missing_ok=True)
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datos", required=True, type=Path, help="FeatureCollection GeoJSON local")
    parser.add_argument("--salida", type=Path, help="ruta MP4 nueva, preferentemente en EXPORTS/<proyecto>/trabajo")
    parser.add_argument("--validar", action="store_true", help="verificar datos sin renderizar")
    parser.add_argument("--alto", type=int, choices=(960, 1280, 1920), default=1280)
    parser.add_argument("--fps", type=int, choices=(24, 30), default=30)
    parser.add_argument("--segundos", type=float, default=7.0)
    args = parser.parse_args()
    if not 1 <= args.segundos <= 30:
        parser.error("--segundos debe estar entre 1 y 30.")
    if not args.validar and not args.salida:
        parser.error("indica --salida o --validar.")
    try:
        scene = load_scene(args.datos)
        clip = Clip(scene, args.alto * 9 // 16, args.alto)
        if args.validar:
            print("GeoJSON válido y textos comprobados. Demo ficticia." if scene.demo else
                  "GeoJSON válido y textos comprobados. Conserva el registro de verificación de la fuente.")
        else:
            render(clip, args.salida, args.segundos, args.fps)
            print(f"MP4 creado: {args.salida}")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
