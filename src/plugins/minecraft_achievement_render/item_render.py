from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import cast

from PIL import Image, ImageChops, ImageDraw

from .resources import (
    ItemIconSpec,
    load_base_item_image,
    load_component_texture,
    resolve_player_skin,
)

ICON_SIZE = (256, 256)
MAX_ALPHA = 255
PERSPECTIVE_EPSILON = 1e-9
ARROW_TINT_BRIGHTNESS = 236
GLINT_TEXTURE_SCALE = 8
GLINT_ROTATION_DEGREES = -10.0
GLINT_COLOR_STRENGTH = 0.6
HEAD_VERTICAL_CENTER = 129.0
HEAD_VERTICAL_SCALE = 1.055
GLINT_CACHE_SIZE = 2
MODEL_CANVAS_SIZE = (512, 512)

DYE_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (249, 255, 254),
    "orange": (249, 128, 29),
    "magenta": (199, 78, 189),
    "light_blue": (58, 179, 218),
    "yellow": (254, 216, 61),
    "lime": (128, 199, 31),
    "pink": (243, 139, 170),
    "gray": (71, 79, 82),
    "light_gray": (157, 157, 151),
    "cyan": (22, 156, 156),
    "purple": (137, 50, 184),
    "blue": (60, 68, 170),
    "brown": (131, 84, 50),
    "green": (94, 124, 22),
    "red": (176, 46, 38),
    "black": (29, 29, 33),
}

# Java potion inventory colors, calibrated against the supplied reference sheet.
POTION_COLORS: dict[str, int] = {
    "water": 0x385DC6,
    "mundane": 0x385DC6,
    "thick": 0x385DC6,
    "awkward": 0x385DC6,
    "regeneration": 0xCD5CAB,
    "swiftness": 0x33EBFF,
    "fire_resistance": 0xFF9900,
    "poison": 0x87A363,
    "healing": 0xF82423,
    "night_vision": 0xC2FF66,
    "weakness": 0x484D48,
    "strength": 0xFFC700,
    "slowness": 0x8BAFE0,
    "leaping": 0xFDFF84,
    "harming": 0xA9656A,
    "water_breathing": 0x98DAC0,
    "invisibility": 0xF6F6F6,
    "luck": 0x59C106,
    "turtle_master": 0x8D82E6,
    "strong_turtle_master": 0x8D85E6,
    "slow_falling": 0xF3CFB9,
    "wind_charged": 0xBDC9FF,
    "weaving": 0x78695A,
    "oozing": 0x99FFA3,
    "infested": 0x8C9B8C,
    # Bedrock-only, retained as a harmless data-pack compatibility alias.
    "decay": 0x736156,
}

_glint_cache: OrderedDict[tuple[str, int], Image.Image] = OrderedDict()

CuboidSpec = tuple[
    tuple[float, float, float],
    tuple[int, int, int],
    tuple[int, int],
]
FacePlan = tuple[
    float,
    tuple[int, int, int, int],
    tuple[int, int],
    tuple[int, int],
    tuple[float, ...],
]


def _rgb(value: int) -> tuple[int, int, int]:
    value &= 0xFFFFFF
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _item_path(item_id: str) -> str:
    return item_id.split(":", maxsplit=1)[-1]


def _alpha_mask(image: Image.Image) -> Image.Image:
    return image.getchannel("A")


def _zero_alpha_mask(value: float) -> int:
    return MAX_ALPHA if value == 0 else 0


def _tint_texture(
    texture: Image.Image,
    color: tuple[int, int, int],
) -> Image.Image:
    source = texture.convert("RGBA")
    solid = Image.new("RGB", source.size, color)
    tinted_rgb = ImageChops.multiply(source.convert("RGB"), solid)
    tinted = tinted_rgb.convert("RGBA")
    tinted.putalpha(source.getchannel("A"))
    return tinted


def _scale_rgb(image: Image.Image, numerator: int, denominator: int) -> Image.Image:
    source = image.convert("RGBA")
    channels = source.convert("RGB").split()
    scaled = tuple(
        channel.point(
            lambda value: min(
                MAX_ALPHA,
                round(cast("float", value) * numerator / denominator),
            ),
        )
        for channel in channels
    )
    result = Image.merge("RGB", scaled).convert("RGBA")
    result.putalpha(source.getchannel("A"))
    return result


def _compose_generated_icon(
    layers: Sequence[tuple[Image.Image, tuple[int, int, int] | None]],
) -> Image.Image:
    if not layers:
        return Image.new("RGBA", ICON_SIZE, (0, 0, 0, 0))
    logical_size = layers[0][0].size
    result = Image.new("RGBA", logical_size, (0, 0, 0, 0))
    for texture, tint in layers:
        layer = texture.convert("RGBA")
        if layer.size != logical_size:
            layer = layer.resize(logical_size, Image.Resampling.NEAREST)
        if tint is not None:
            layer = _tint_texture(layer, tint)
        result.alpha_composite(layer)
    return result.resize(ICON_SIZE, Image.Resampling.NEAREST)


def _banner_color(item_path: str, configured: object) -> str | None:
    if isinstance(configured, str):
        return configured.removeprefix("minecraft:")
    if item_path.endswith("_banner"):
        candidate = item_path.removesuffix("_banner")
        if candidate in DYE_COLORS:
            return candidate
    return None


def _solve_perspective(
    target_points: Sequence[tuple[float, float]],
    source_points: Sequence[tuple[float, float]],
) -> tuple[float, ...]:
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(target_points, source_points, strict=True):
        matrix.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        values.append(u)
        matrix.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        values.append(v)

    count = 8
    for index in range(count):
        pivot_row = max(range(index, count), key=lambda row: abs(matrix[row][index]))
        matrix[index], matrix[pivot_row] = matrix[pivot_row], matrix[index]
        values[index], values[pivot_row] = values[pivot_row], values[index]
        pivot = matrix[index][index]
        if abs(pivot) < PERSPECTIVE_EPSILON:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        for row in range(index + 1, count):
            factor = matrix[row][index] / pivot
            for column in range(index, count):
                matrix[row][column] -= factor * matrix[index][column]
            values[row] -= factor * values[index]

    coefficients = [0.0] * count
    for index in range(count - 1, -1, -1):
        remainder = sum(
            matrix[index][column] * coefficients[column]
            for column in range(index + 1, count)
        )
        coefficients[index] = (values[index] - remainder) / matrix[index][index]
    return tuple(coefficients)


def _rotate_item_point(
    point: tuple[float, float, float],
    rotation: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Apply Minecraft's GUI item rotation order (Z, Y, then X)."""

    x, y, z = point
    x_angle, y_angle, z_angle = (math.radians(value) for value in rotation)
    cosine = math.cos(z_angle)
    sine = math.sin(z_angle)
    x, y = x * cosine - y * sine, x * sine + y * cosine
    cosine = math.cos(y_angle)
    sine = math.sin(y_angle)
    x, z = x * cosine + z * sine, -x * sine + z * cosine
    cosine = math.cos(x_angle)
    sine = math.sin(x_angle)
    y, z = y * cosine - z * sine, y * sine + z * cosine
    return x, y, z


def _project_special_model_point(
    point: tuple[float, float, float],
    *,
    is_shield: bool,
) -> tuple[float, float, float]:
    """Project a vanilla special-model vertex into a roomy work canvas."""

    x, y, z = (coordinate / 16.0 for coordinate in point)
    if is_shield:
        transformed = (x, -y, -z)
        scale = 0.65
        translation = (2.0 / 16.0, 3.0 / 16.0, 0.0)
        rotation = (15.0, -25.0, -5.0)
    else:
        transformed = (
            0.5 + x * (2.0 / 3.0),
            -y * (2.0 / 3.0),
            0.5 - z * (2.0 / 3.0),
        )
        scale = 0.5325
        translation = (0.0, -3.25 / 16.0, 0.0)
        rotation = (30.0, 20.0, 0.0)

    transformed = tuple(value * scale for value in transformed)
    x, y, z = _rotate_item_point(
        cast("tuple[float, float, float]", transformed),
        rotation,
    )
    x += translation[0]
    y += translation[1]
    z += translation[2]
    return 256.0 + x * 256.0, 256.0 - y * 256.0, z


def _cuboid_face_definitions(
    origin: tuple[float, float, float],
    size: tuple[int, int, int],
    uv: tuple[int, int],
) -> tuple[
    tuple[
        tuple[int, int, int, int],
        tuple[tuple[float, float, float], ...],
    ],
    ...,
]:
    """Return Minecraft ModelPart cube faces with their atlas UV rectangles."""

    x0, y0, z0 = origin
    width, height, depth = size
    x1, y1, z1 = x0 + width, y0 + height, z0 + depth
    texture_x, texture_y = uv
    row_top = texture_y
    row_side = texture_y + depth
    row_bottom = row_side + height
    west_x = texture_x
    north_x = west_x + depth
    east_x = north_x + width
    south_x = east_x + depth

    return (
        (
            (west_x, row_side, north_x, row_bottom),
            ((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0)),
        ),
        (
            (north_x, row_side, east_x, row_bottom),
            ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)),
        ),
        (
            (east_x, row_side, south_x, row_bottom),
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
        ),
        (
            (south_x, row_side, south_x + width, row_bottom),
            ((x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (x0, y0, z1)),
        ),
        (
            (north_x, row_top, east_x, row_side),
            ((x0, y0, z1), (x0, y0, z0), (x1, y0, z0), (x1, y0, z1)),
        ),
        (
            (east_x, row_top, east_x + width, row_side),
            ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)),
        ),
    )


@lru_cache(maxsize=8)
def _model_face_plans(
    cuboids: tuple[CuboidSpec, ...],
    *,
    is_shield: bool,
) -> tuple[FacePlan, ...]:
    plans: list[FacePlan] = []
    for origin, size, uv in cuboids:
        for crop_box, points in _cuboid_face_definitions(origin, size, uv):
            projected = tuple(
                _project_special_model_point(point, is_shield=is_shield)
                for point in points
            )
            quad = tuple((point[0], point[1]) for point in projected)
            depth = sum(point[2] for point in projected) / len(projected)
            left = max(0, math.floor(min(point[0] for point in quad)))
            top = max(0, math.floor(min(point[1] for point in quad)))
            right = min(
                MODEL_CANVAS_SIZE[0],
                math.ceil(max(point[0] for point in quad)),
            )
            bottom = min(
                MODEL_CANVAS_SIZE[1],
                math.ceil(max(point[1] for point in quad)),
            )
            local_quad = tuple((x - left, y - top) for x, y in quad)
            width = crop_box[2] - crop_box[0]
            height = crop_box[3] - crop_box[1]
            source_points = (
                (0.0, 0.0),
                (0.0, float(height)),
                (float(width), float(height)),
                (float(width), 0.0),
            )
            plans.append(
                (
                    depth,
                    crop_box,
                    (left, top),
                    (right - left, bottom - top),
                    _solve_perspective(local_quad, source_points),
                ),
            )
    return tuple(sorted(plans, key=lambda plan: plan[0]))


def _render_model_cuboids(
    texture: Image.Image,
    cuboids: tuple[CuboidSpec, ...],
    *,
    is_shield: bool,
) -> Image.Image:
    atlas = texture.convert("RGBA")
    result = Image.new("RGBA", MODEL_CANVAS_SIZE, (0, 0, 0, 0))
    # Transform only each face's bounding box, not a full 512px temporary canvas.
    for _, crop_box, position, size, coefficients in _model_face_plans(
        cuboids,
        is_shield=is_shield,
    ):
        face = atlas.crop(crop_box)
        if face.getchannel("A").getbbox() is None:
            continue
        warped = face.transform(
            size,
            Image.Transform.PERSPECTIVE,
            coefficients,
            Image.Resampling.NEAREST,
        ).convert("RGBA")
        result.alpha_composite(warped, position)
    return result


def _fit_inventory_model(
    image: Image.Image,
    target_box: tuple[int, int, int, int],
) -> Image.Image:
    source_box = image.getchannel("A").getbbox()
    if source_box is None:
        return Image.new("RGBA", ICON_SIZE, (0, 0, 0, 0))
    target_width = target_box[2] - target_box[0]
    target_height = target_box[3] - target_box[1]
    fitted = image.crop(source_box).resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    )
    result = Image.new("RGBA", ICON_SIZE, (0, 0, 0, 0))
    result.alpha_composite(fitted, target_box[:2])
    return result


def _parse_pattern_layers(
    patterns: object,
) -> list[tuple[str, tuple[int, int, int]]]:
    if not isinstance(patterns, list):
        return []
    parsed: list[tuple[str, tuple[int, int, int]]] = []
    for layer in patterns:
        if not isinstance(layer, Mapping):
            continue
        pattern = layer.get("pattern")
        color_name = layer.get("color")
        if not isinstance(pattern, str) or not isinstance(color_name, str):
            continue
        color = DYE_COLORS.get(color_name.removeprefix("minecraft:"))
        if color is not None:
            parsed.append((pattern.removeprefix("minecraft:"), color))
    return parsed


async def _render_banner_or_shield(
    item_path: str,
    components: Mapping[str, object],
    prefix: str,
) -> Image.Image | None:
    is_shield = item_path == "shield"
    patterns = components.get("banner_patterns")
    color_name = _banner_color(item_path, components.get("base_color"))
    if color_name is None and patterns:
        color_name = "white"
    face_color = DYE_COLORS.get(color_name or "white", DYE_COLORS["white"])
    parsed_patterns = _parse_pattern_layers(patterns)
    has_design = not is_shield or color_name is not None or bool(parsed_patterns)
    texture_kind = "shield" if is_shield else "banner"
    if is_shield:
        structure_path = (
            "entity/shield_base.png"
            if has_design
            else "entity/shield_base_nopattern.png"
        )
        structure_cuboids: tuple[CuboidSpec, ...] = (
            ((-6.0, -11.0, -2.0), (12, 22, 1), (0, 0)),
            ((-1.0, -3.0, -1.0), (2, 6, 6), (26, 0)),
        )
        design_cuboids: tuple[CuboidSpec, ...] = (
            ((-6.0, -11.0, -2.0), (12, 22, 1), (0, 0)),
        )
        target_box = (39, 5, 174, 249)
    else:
        structure_path = "entity/banner_base.png"
        structure_cuboids = (
            ((-1.0, -42.0, -1.0), (2, 42, 2), (44, 0)),
            ((-10.0, -44.0, -1.0), (20, 2, 2), (0, 42)),
        )
        design_cuboids = (((-10.0, -44.0, -2.0), (20, 40, 1), (0, 0)),)
        target_box = (73, 10, 186, 243)

    overlay_requests = (
        [
            load_component_texture(f"entity/{texture_kind}/base.png", prefix),
            *(
                load_component_texture(
                    f"entity/{texture_kind}/{pattern_name}.png",
                    prefix,
                )
                for pattern_name, _ in parsed_patterns
            ),
        ]
        if has_design
        else []
    )
    structure, *overlays = await asyncio.gather(
        load_component_texture(structure_path, prefix),
        *overlay_requests,
    )
    if structure is None:
        return None

    result = _render_model_cuboids(
        structure,
        structure_cuboids,
        is_shield=is_shield,
    )
    if has_design:
        if any(texture is None for texture in overlays):
            return None
        colors = [face_color, *(color for _, color in parsed_patterns)]
        # Keep alpha blending before the final LANCZOS resize for pixel parity.
        for texture, color in zip(overlays, colors, strict=True):
            assert texture is not None
            layer = _render_model_cuboids(
                _tint_texture(texture, color),
                design_cuboids,
                is_shield=is_shield,
            )
            result.alpha_composite(layer)
    return _fit_inventory_model(result, target_box)


def _normalized_potion_name(value: str) -> str:
    name = value.removeprefix("minecraft:")
    if name in POTION_COLORS:
        return name
    if name.startswith("long_"):
        return name.removeprefix("long_")
    if name.startswith("strong_"):
        base_name = name.removeprefix("strong_")
        if base_name == "turtle_master":
            return "strong_turtle_master"
        return base_name
    return name


def _potion_color(value: object) -> tuple[int, int, int] | None:
    if isinstance(value, str):
        name = _normalized_potion_name(value)
        return _rgb(POTION_COLORS.get(name, POTION_COLORS["water"]))
    if not isinstance(value, Mapping):
        return None
    custom_color = value.get("custom_color")
    if isinstance(custom_color, int) and not isinstance(custom_color, bool):
        return _rgb(custom_color)
    potion = value.get("potion")
    if isinstance(potion, str):
        name = _normalized_potion_name(potion)
        return _rgb(POTION_COLORS.get(name, POTION_COLORS["water"]))
    return None


async def _render_potion_tint(
    image: Image.Image,
    item_path: str,
    contents: object,
    prefix: str,
) -> Image.Image:
    color = _potion_color(contents)
    if color is None:
        return image

    if item_path == "tipped_arrow":
        head, base = await asyncio.gather(
            load_component_texture("item/tipped_arrow_head.png", prefix),
            load_component_texture("item/tipped_arrow_base.png", prefix),
        )
        if head is not None and base is not None:
            tinted_head = _scale_rgb(
                _tint_texture(head, color),
                ARROW_TINT_BRIGHTNESS,
                MAX_ALPHA,
            )
            return _compose_generated_icon(((tinted_head, None), (base, None)))
        return image

    overlay, bottle = await asyncio.gather(
        load_component_texture("item/potion_overlay.png", prefix),
        load_component_texture(f"item/{item_path}.png", prefix),
    )
    if overlay is not None and bottle is not None:
        return _compose_generated_icon(((overlay, color), (bottle, None)))
    return image


def _should_glint(components: Mapping[str, object]) -> bool:
    if "enchantment_glint_override" in components:
        return components["enchantment_glint_override"] is True
    return bool(components.get("enchantments") or components.get("stored_enchantments"))


def _transform_glint_texture(glint_texture: Image.Image) -> Image.Image:
    source = glint_texture.convert("RGB")
    # Minecraft samples one enlarged glint template over the item.  Repeating a
    # reduced tile creates a dense purple grid that is not present in the
    # inventory icon, so enlarge the template itself before taking the static
    # zero-phase snapshot.
    enlarged = source.resize(
        (
            source.width * GLINT_TEXTURE_SCALE,
            source.height * GLINT_TEXTURE_SCALE,
        ),
        Image.Resampling.BILINEAR,
    )
    transformed = enlarged.rotate(
        GLINT_ROTATION_DEGREES,
        resample=Image.Resampling.BILINEAR,
        expand=True,
    )
    offset_x = (transformed.width - ICON_SIZE[0]) // 2
    offset_y = (transformed.height - ICON_SIZE[1]) // 2
    return transformed.crop(
        (
            offset_x,
            offset_y,
            offset_x + ICON_SIZE[0],
            offset_y + ICON_SIZE[1],
        ),
    )


def _prepare_glint(glint_texture: Image.Image) -> Image.Image:
    glint_rgb = _transform_glint_texture(glint_texture)
    shader_glint = glint_rgb.point(
        lambda value: round(cast("float", value) * GLINT_COLOR_STRENGTH),
    )
    return ImageChops.multiply(shader_glint, shader_glint)


def _blend_glint(image: Image.Image, contribution: Image.Image) -> Image.Image:
    source = image.convert("RGBA")
    blended_rgb = ImageChops.add(source.convert("RGB"), contribution)
    result = blended_rgb.convert("RGBA")
    alpha = _alpha_mask(source)
    result.putalpha(alpha)
    result.paste((0, 0, 0, 0), (0, 0), alpha.point(_zero_alpha_mask))
    return result


def _render_glint(image: Image.Image, glint_texture: Image.Image) -> Image.Image:
    return _blend_glint(image, _prepare_glint(glint_texture))


async def _load_glint(prefix: str) -> Image.Image | None:
    cache_key = (prefix, id(load_component_texture))
    cached = _glint_cache.get(cache_key)
    if cached is not None:
        _glint_cache.move_to_end(cache_key)
        return cached

    texture = await load_component_texture("misc/enchanted_glint_item.png", prefix)
    if texture is None:
        return None
    contribution = _prepare_glint(texture)
    _glint_cache[cache_key] = contribution
    _glint_cache.move_to_end(cache_key)
    if len(_glint_cache) > GLINT_CACHE_SIZE:
        _glint_cache.popitem(last=False)
    return contribution


def _normalize_skin(skin: Image.Image) -> Image.Image:
    skin = skin.convert("RGBA")
    if skin.width == skin.height:
        return skin.resize((64, 64), Image.Resampling.NEAREST)
    if skin.width == skin.height * 2:
        legacy = skin.resize((64, 32), Image.Resampling.NEAREST)
        normalized = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        normalized.alpha_composite(legacy)
        return normalized
    return skin.resize((64, 64), Image.Resampling.NEAREST)


def _bilinear_point(
    quad: Sequence[tuple[float, float]],
    u: float,
    v: float,
) -> tuple[int, int]:
    top_left, top_right, bottom_right, bottom_left = quad
    x = (
        (1 - u) * (1 - v) * top_left[0]
        + u * (1 - v) * top_right[0]
        + u * v * bottom_right[0]
        + (1 - u) * v * bottom_left[0]
    )
    y = (
        (1 - u) * (1 - v) * top_left[1]
        + u * (1 - v) * top_right[1]
        + u * v * bottom_right[1]
        + (1 - u) * v * bottom_left[1]
    )
    return round(x), round(y)


def _draw_texture_face(
    target: Image.Image,
    texture: Image.Image,
    quad: Sequence[tuple[float, float]],
    brightness: float,
) -> None:
    draw = ImageDraw.Draw(target)
    for y in range(texture.height):
        for x in range(texture.width):
            pixel = cast("tuple[int, int, int, int]", texture.getpixel((x, y)))
            if pixel[3] == 0:
                continue
            color = (
                round(pixel[0] * brightness),
                round(pixel[1] * brightness),
                round(pixel[2] * brightness),
                pixel[3],
            )
            u0 = x / texture.width
            u1 = (x + 1) / texture.width
            v0 = y / texture.height
            v1 = (y + 1) / texture.height
            draw.polygon(
                (
                    _bilinear_point(quad, u0, v0),
                    _bilinear_point(quad, u1, v0),
                    _bilinear_point(quad, u1, v1),
                    _bilinear_point(quad, u0, v1),
                ),
                fill=color,
            )


def _stretch_quad_y(
    quad: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            x,
            HEAD_VERTICAL_CENTER + (y - HEAD_VERTICAL_CENTER) * HEAD_VERTICAL_SCALE,
        )
        for x, y in quad
    )


def render_player_head(skin: Image.Image) -> Image.Image:
    """Render a skin head with the reference image's taller cube proportion."""

    skin = _normalize_skin(skin)
    result = Image.new("RGBA", ICON_SIZE, (0, 0, 0, 0))
    top_quad = _stretch_quad_y(((37, 80), (127, 34), (218, 80), (128, 128)))
    side_quad = _stretch_quad_y(((37, 80), (128, 128), (128, 224), (37, 176)))
    front_quad = _stretch_quad_y(((128, 128), (218, 80), (218, 176), (128, 224)))

    _draw_texture_face(result, skin.crop((8, 0, 16, 8)), top_quad, 1.0)
    _draw_texture_face(result, skin.crop((0, 8, 8, 16)), side_quad, 0.66)
    _draw_texture_face(result, skin.crop((8, 8, 16, 16)), front_quad, 0.84)

    hat_top_quad = _stretch_quad_y(((31, 78), (127, 29), (224, 78), (128, 130)))
    hat_side_quad = _stretch_quad_y(((31, 78), (128, 130), (128, 229), (31, 177)))
    hat_front_quad = _stretch_quad_y(((128, 130), (224, 78), (224, 177), (128, 229)))
    _draw_texture_face(result, skin.crop((40, 0, 48, 8)), hat_top_quad, 1.0)
    _draw_texture_face(result, skin.crop((32, 8, 40, 16)), hat_side_quad, 0.66)
    _draw_texture_face(result, skin.crop((40, 8, 48, 16)), hat_front_quad, 0.84)
    return result


async def render_item_icon(
    spec: ItemIconSpec,
    prefix: str = "",
) -> Image.Image:
    """Render the supported visual components on a 256px RGBA icon."""

    components = spec.components
    item_path = _item_path(spec.item_id)

    if item_path == "shield" or item_path.endswith("_banner"):
        image = await _render_banner_or_shield(item_path, components, prefix)
    else:
        image = None
    if image is None:
        image = await load_base_item_image(spec.item_id, prefix)
        if image.size != ICON_SIZE:
            image = image.resize(ICON_SIZE, Image.Resampling.LANCZOS)
        if image.mode != "RGBA":
            image = image.convert("RGBA")

    if item_path == "player_head" and "profile" in components:
        skin = await resolve_player_skin(components["profile"])
        if skin is not None:
            image = render_player_head(skin)

    if "potion_contents" in components and item_path in {
        "potion",
        "splash_potion",
        "lingering_potion",
        "tipped_arrow",
    }:
        image = await _render_potion_tint(
            image,
            item_path,
            components["potion_contents"],
            prefix,
        )

    if _should_glint(components):
        contribution = await _load_glint(prefix)
        if contribution is not None:
            image = _blend_glint(image, contribution)
    return image


__all__ = ["render_item_icon", "render_player_head"]
