"""Модель пера — граница меди полигона.

[polygon.md](../spec/polygon.md) хранит **осевую линию круглого пера**
толщиной `width`, а не край меди: «кромка меди торчит на `width/2` наружу от
осевой — это не погрешность, а честная геометрия хранимого». Инструмент, у
которого полигон это зона с точной границей (KiCad, Altium), обязан получить
контур, **раздвинутый на `width/2` наружу**, иначе его медь окажется уже
исходной ровно на перо.

Граница следа круглого пера — это сумма Минковского контура с диском, и у неё
есть замкнутая форма: каждое ребро сдвигается по своей нормали, на выпуклом
углу появляется дуга радиуса `width/2`, на вогнутом — сдвинутые рёбра
пересекаются и подрезаются. **Дуга при этом остаётся дугой** (сдвиг дуги —
концентрическая дуга), и это главный довод против готовой библиотеки: Clipper
и shapely работают только с отрезками, то есть скруглили бы каждую дугу в
ломаную из десятков точек и разрушили ровно ту геометрию, ради точности
которой всё и затевалось.

Здесь, а не в `kicad/`, потому что вопрос задаёт сам полигон: [Altium
спросит то же самое](conversion-altium.md).
"""

from __future__ import annotations

import math

EPS = 1e-6

# ('line' | 'arc', x1, y1, x2, y2, curve_deg) — рёбра в µm с плавающей точкой:
# сдвиг на пол-пера не обязан попадать в целый микрон.
Edge = tuple[str, float, float, float, float, float]


def arc_center(x1: float, y1: float, x2: float, y2: float,
                curve_deg: float) -> tuple[float, float, float] | None:
    """(cx, cy, r) дуги; None для вырожденной хорды. Та же математика, что у
    `graphics.Arc.center` — центр на серединном перпендикуляре хорды, знак
    `curve` выбирает сторону, — но в float-µm и с радиусом, который здесь
    нужен на каждом шагу."""
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    if chord < 1e-10 or not curve_deg:
        return None
    a = math.radians(abs(curve_deg))
    r = chord / (2 * math.sin(a / 2))
    d = r * math.cos(a / 2)
    sign = 1 if curve_deg > 0 else -1
    return ((x1 + x2) / 2 + sign * d * (-dy / chord),
            (y1 + y2) / 2 + sign * d * (dx / chord), r)


def contour_area(vertices) -> float:
    """Знаковая площадь замкнутого контура (против часовой — положительная):
    формула шнурков по хордам плюс поправка круговым сегментом на каждой
    дуге."""
    area = 0.0
    n = len(vertices)
    for i, (x1, y1, curve) in enumerate(vertices):
        x2, y2, _ = vertices[(i + 1) % n]
        area += (x1 * y2 - x2 * y1) / 2
        if curve:
            c = arc_center(x1, y1, x2, y2, curve)
            if c is not None:
                a = math.radians(abs(curve))
                seg = c[2] * c[2] / 2 * (a - math.sin(a))
                area += seg if curve > 0 else -seg
    return area


def _geo(e: Edge):
    """('line', p1, p2) либо ('arc', центр, R, начальный угол, разворот, p1, p2)."""
    kind, x1, y1, x2, y2, curve = e
    if kind == "line" or not curve:
        return ("line", (x1, y1), (x2, y2))
    cx, cy, r = arc_center(x1, y1, x2, y2, curve)
    return ("arc", (cx, cy), r, math.degrees(math.atan2(y1 - cy, x1 - cx)),
            curve, (x1, y1), (x2, y2))


def _on_arc(geo, px: float, py: float) -> bool:
    _, (cx, cy), r, a1, sweep, _, _ = geo
    a = math.degrees(math.atan2(py - cy, px - cx))
    d = (a - a1) % 360 if sweep > 0 else (a1 - a) % 360
    return d <= abs(sweep) + 1e-7


def _on_line(geo, px: float, py: float) -> bool:
    _, (x1, y1), (x2, y2) = geo
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 < EPS:
        return False
    t = ((px - x1) * dx + (py - y1) * dy) / l2
    return (-1e-9 <= t <= 1 + 1e-9
            and abs((px - x1) * dy - (py - y1) * dx) / math.sqrt(l2) < 1e-3)


def _intersections(ea: Edge, eb: Edge) -> list[tuple[float, float]]:
    """Точки пересечения двух рёбер, ограниченные обоими отрезками/дугами."""
    ga, gb = _geo(ea), _geo(eb)
    pts: list[tuple[float, float]] = []
    if ga[0] == "line" and gb[0] == "line":
        (x1, y1), (x2, y2) = ga[1], ga[2]
        (x3, y3), (x4, y4) = gb[1], gb[2]
        den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
        if abs(den) > EPS:
            t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
            pts.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    elif ga[0] == "line" or gb[0] == "line":
        line, arc = (ga, gb) if ga[0] == "line" else (gb, ga)
        (x1, y1), (x2, y2) = line[1], line[2]
        (cx, cy), r = arc[1], arc[2]
        dx, dy = x2 - x1, y2 - y1
        fx, fy = x1 - cx, y1 - cy
        a = dx * dx + dy * dy
        b = 2 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - r * r
        disc = b * b - 4 * a * c
        if a > EPS and disc >= 0:
            sq = math.sqrt(disc)
            for t in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)):
                pts.append((x1 + t * dx, y1 + t * dy))
    else:
        (c1x, c1y), r1 = ga[1], ga[2]
        (c2x, c2y), r2 = gb[1], gb[2]
        d = math.hypot(c2x - c1x, c2y - c1y)
        if d > EPS and abs(r1 - r2) - 1e-9 <= d <= r1 + r2 + 1e-9:
            a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
            h = math.sqrt(max(r1 * r1 - a * a, 0.0))
            mx = c1x + a * (c2x - c1x) / d
            my = c1y + a * (c2y - c1y) / d
            ux, uy = (c2y - c1y) / d, -(c2x - c1x) / d
            pts.append((mx + h * ux, my + h * uy))
            if h > EPS:
                pts.append((mx - h * ux, my - h * uy))
    out = []
    for px, py in pts:
        on_a = _on_line(ga, px, py) if ga[0] == "line" else _on_arc(ga, px, py)
        on_b = _on_line(gb, px, py) if gb[0] == "line" else _on_arc(gb, px, py)
        if on_a and on_b:
            out.append((px, py))
    return out


def _tangents(e: Edge):
    """Единичные касательные в начале и конце ребра."""
    kind, x1, y1, x2, y2, curve = e
    if kind == "line" or not curve:
        dx, dy = x2 - x1, y2 - y1
        l = math.hypot(dx, dy) or 1.0
        t = (dx / l, dy / l)
        return t, t
    cx, cy, r = arc_center(x1, y1, x2, y2, curve)
    s = 1.0 if curve > 0 else -1.0

    def tang(px, py):
        return (-s * (py - cy) / r, s * (px - cx) / r)

    return tang(x1, y1), tang(x2, y2)


def _length(e: Edge) -> float:
    kind, x1, y1, x2, y2, curve = e
    if kind == "arc" and curve:
        c = arc_center(x1, y1, x2, y2, curve)
        if c is not None:
            return c[2] * math.radians(abs(curve))
    return math.hypot(x2 - x1, y2 - y1)


def _offset_edge(e: Edge, r: float) -> Edge | None:
    """Ребро, сдвинутое ВПРАВО по ходу на `r` (на контуре против часовой это
    наружу). У дуги сдвигается радиус: центр остаётся на месте."""
    kind, x1, y1, x2, y2, curve = e
    if kind == "line" or not curve:
        dx, dy = x2 - x1, y2 - y1
        l = math.hypot(dx, dy)
        if l < EPS:
            return None
        nx, ny = dy / l, -dx / l
        return ("line", x1 + nx * r, y1 + ny * r, x2 + nx * r, y2 + ny * r, 0.0)
    cx, cy, radius = arc_center(x1, y1, x2, y2, curve)
    # Дуга против часовой (curve>0) держит центр СЛЕВА по ходу — сдвиг вправо
    # растит радиус; по часовой — центр справа, радиус убывает.
    r2 = radius + r if curve > 0 else radius - r
    if r2 <= EPS:
        raise ValueError(f"сдвиг контура: радиус вогнутой дуги {radius:.1f} мкм не больше "
                          f"радиуса пера {r:.1f} мкм — результат вырожден")

    def scale(px, py):
        return cx + (px - cx) * r2 / radius, cy + (py - cy) * r2 / radius

    ox1, oy1 = scale(x1, y1)
    ox2, oy2 = scale(x2, y2)
    return ("arc", ox1, oy1, ox2, oy2, curve)


def _trim(e: Edge, px: float, py: float, at_end: bool) -> Edge:
    """Подрезать ребро до точки: дуга пересчитывает разворот к оставшемуся концу."""
    kind, x1, y1, x2, y2, curve = e
    if kind == "line" or not curve:
        return ("line", x1, y1, px, py, 0.0) if at_end else ("line", px, py, x2, y2, 0.0)
    cx, cy, _ = arc_center(x1, y1, x2, y2, curve)
    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
    ap = math.degrees(math.atan2(py - cy, px - cx))
    if at_end:
        sweep = (ap - a1) % 360 if curve > 0 else -((a1 - ap) % 360)
        return ("arc", x1, y1, px, py, sweep)
    sweep = (a2 - ap) % 360 if curve > 0 else -((ap - a2) % 360)
    return ("arc", px, py, x2, y2, sweep)


def offset_contour(vertices, r: float) -> list[Edge]:
    """Замкнутый контур `[(x, y, curve_deg_к_следующей), ...]`, сдвинутый
    НАРУЖУ на `r` мкм круглыми стыками — граница меди модели пера. Отдаёт
    рёбра против часовой стрелки.

    Бросает `ValueError` на вырожденном результате (схлопнувшаяся вогнутая
    дуга, несостоявшаяся подрезка, самопересечение) — жёсткий отказ, а не
    догадка: там, где граница честно не существует, соврать хуже, чем
    сказать."""
    if len(vertices) < 3:
        # Не обязательно пусто: две вершины с `curve` — это дуга и её
        # замыкающая хорда, реальная площадь. У модели пера там просто нет
        # угла, который надо скруглять, — это ограничение, а не приговор
        # контуру, и вызывающий говорит об этом своими словами.
        raise ValueError("сдвиг контура: меньше трёх вершин (дуга со своей хордой — "
                          "настоящая область, но здесь не сдвигается)")
    if contour_area(vertices) < 0:
        n = len(vertices)
        vertices = [(vertices[(i + 1) % n][0], vertices[(i + 1) % n][1],
                      -vertices[i][2] if vertices[i][2] else 0.0)
                     for i in range(n - 1, -1, -1)]

    edges: list[Edge] = []
    n = len(vertices)
    for i, (x1, y1, curve) in enumerate(vertices):
        x2, y2, _ = vertices[(i + 1) % n]
        if math.hypot(x2 - x1, y2 - y1) < EPS:
            continue
        edges.append(("arc" if curve else "line", x1, y1, x2, y2, float(curve or 0.0)))

    # Разрешение углов с ПОГЛОЩЕНИЕМ РЁБЕР: ребро осевой, которое короче
    # локального вогнутого перекрытия, съедается пером целиком — настоящая
    # граница просто не содержит его образа, ровно так это и рисует Eagle.
    # Когда вогнутая подрезка не находит пересечения, короткое из двух
    # сдвинутых рёбер выбрасывается и проход начинается заново с чистых
    # сдвигов; каждый перезапуск убирает по ребру, так что процесс конечен.
    while True:
        off: list[tuple[Edge, Edge]] = []
        for e in edges:
            oe = _offset_edge(e, r)
            if oe is not None:
                off.append((e, oe))
        if len(off) < 3:
            raise ValueError("сдвиг контура: под этим пером контур вырождается "
                              "(осталось меньше трёх рёбер)")
        consumed = None
        out: list = []
        m = len(off)
        for i in range(m):
            (e1, o1), (e2, o2) = off[i], off[(i + 1) % m]
            out.append(i)
            p_end, p_start = (o1[3], o1[4]), (o2[1], o2[2])
            if math.hypot(p_start[0] - p_end[0], p_start[1] - p_end[1]) < 1e-3:
                continue
            t1, t2 = _tangents(e1)[1], _tangents(e2)[0]
            vx, vy = e1[3], e1[4]                      # исходный угол
            if t1[0] * t2[1] - t1[1] * t2[0] > 1e-9:
                # выпуклый (поворот влево на контуре против часовой):
                # круглый стык пера вокруг вершины
                a1 = math.degrees(math.atan2(p_end[1] - vy, p_end[0] - vx))
                a2 = math.degrees(math.atan2(p_start[1] - vy, p_start[0] - vx))
                out.append(("join", p_end, p_start, (a2 - a1) % 360))
            else:
                # вогнутый: сдвинутые рёбра перекрылись — подрезать оба
                xs = _intersections(o1, o2)
                if not xs:
                    consumed = e1 if _length(o1) < _length(o2) else e2
                    break
                px, py = min(xs, key=lambda p: math.hypot(p[0] - vx, p[1] - vy))
                off[i] = (e1, _trim(o1, px, py, at_end=True))
                off[(i + 1) % m] = (e2, _trim(o2, px, py, at_end=False))
        if consumed is None:
            break
        edges = [e for e in edges if e is not consumed]

    result: list[Edge] = []
    for item in out:
        if isinstance(item, tuple):
            _, (px1, py1), (px2, py2), sweep = item
            if sweep > 1e-6:
                result.append(("arc", px1, py1, px2, py2, sweep))
        else:
            kind, x1, y1, x2, y2, curve = off[item][1]
            if math.hypot(x2 - x1, y2 - y1) > 1e-3 or abs(curve) > 1e-6:
                result.append((kind, x1, y1, x2, y2, curve))

    k = len(result)
    for i in range(k):
        for j in range(i + 2, k):
            if i == 0 and j == k - 1:
                continue
            if _intersections(result[i], result[j]):
                raise ValueError("сдвиг контура: результат самопересекается "
                                  "(перешеек уже пера) — жёсткий отказ")
    return result


def edge_midpoint(e: Edge) -> tuple[float, float]:
    """Точка на середине ребра — то, что просит трёхточечная форма дуги
    (`(arc (start) (mid) (end))`). Поворотом на половину разворота вокруг
    центра, как `graphics.Arc.midpoint`: сагитта от середины хорды выбрала
    бы не ту дугу из двух при развороте больше 180°."""
    kind, x1, y1, x2, y2, curve = e
    if kind == "line" or not curve:
        return (x1 + x2) / 2, (y1 + y2) / 2
    cx, cy, _ = arc_center(x1, y1, x2, y2, curve)
    half = math.radians(curve / 2)
    dx, dy = x1 - cx, y1 - cy
    return (cx + dx * math.cos(half) - dy * math.sin(half),
            cy + dx * math.sin(half) + dy * math.cos(half))


def _rot(x: float, y: float, cx: float, cy: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    dx, dy = x - cx, y - cy
    return cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)


def shape_contour(shape, grow: float = 0.0) -> list[tuple[float, float, float]]:
    """A `<shape>`'s own outline as a closed contour, optionally grown by
    `grow` µm — vertices `(x, y, curve_deg)`, the same form `offset_contour`
    takes.

    shape.md's `roundness` is a scale, not three shapes: 0 is a rectangle,
    100 a circle (or a stadium when the sides differ), anything between a
    rectangle with that fraction of the short side rounded off. The corner
    is a quarter arc of that radius, so one construction covers all three
    and none of them is a special case."""
    w, h = shape.w / 2 + grow, shape.h / 2 + grow
    if w <= 0 or h <= 0:
        raise ValueError("контур фигуры: она схлопывается при таком сжатии")
    r = min(w, h) * shape.roundness / 100
    cx, cy, deg = shape.x, shape.y, shape.rot / 1000
    if r <= 0:
        pts = [(-w, -h, 0.0), (w, -h, 0.0), (w, h, 0.0), (-w, h, 0.0)]
    else:
        # Corners run counter-clockwise; each straight run ends where its
        # quarter arc begins, and the arc carries +90°.
        pts = [(-w + r, -h, 0.0), (w - r, -h, 90.0),
                (w, -h + r, 0.0), (w, h - r, 90.0),
                (w - r, h, 0.0), (-w + r, h, 90.0),
                (-w, h - r, 0.0), (-w, -h + r, 90.0)]
        # At `roundness=100` the straight runs vanish (a circle, or a
        # stadium along the long side), and each collapses to a pair of
        # coincident vertices. **Drop the one that STARTS the zero-length
        # straight, never its twin** — the twin is what carries the arc.
        # Dropping the wrong one leaves four points with no curve at all,
        # and the circle comes out a diamond; worse, its vertices still sit
        # exactly on the circle, so any check that measures only points
        # calls it correct. Found by the user looking at the board.
        n = len(pts)
        pts = [p for i, p in enumerate(pts)
               if not (p[2] == 0.0
                       and math.hypot(pts[(i + 1) % n][0] - p[0],
                                       pts[(i + 1) % n][1] - p[1]) < EPS)]
    return [(*_rot(px + cx, py + cy, cx, cy, deg), curve) for px, py, curve in pts]


def capsule(x1: float, y1: float, x2: float, y2: float,
             curve_deg: float, width: float) -> list[Edge]:
    """The area a stroked line or arc occupies — its two offset sides plus a
    half-round cap at each end, closed.

    layer-model.md: a line's `width` IS its only area, so this is what an
    anti-line subtracts. It is the same pen boundary `offset_contour`
    computes, for an OPEN path instead of a closed one — the joins at the
    ends become 180° caps rather than corner arcs."""
    r = width / 2
    if r <= 0:
        raise ValueError("капсула: у штриха нулевая ширина, площади нет")
    e = ("arc" if curve_deg else "line", x1, y1, x2, y2, float(curve_deg or 0.0))
    back = ("arc" if curve_deg else "line", x2, y2, x1, y1, -float(curve_deg or 0.0))
    right = _offset_edge(e, r)
    left = _offset_edge(back, r)
    if right is None or left is None:
        raise ValueError("капсула: вырожденный штрих")
    return [right, ("arc", right[3], right[4], left[1], left[2], 180.0),
            left, ("arc", left[3], left[4], right[1], right[2], 180.0)]


def keyhole(outer: list[Edge], inner: list[Edge]) -> list[Edge]:
    """An annulus as ONE closed contour: round the outside, cut across to
    the inside, round it the other way, cut back.

    A zone outline holds a single contour and has no separate hole, so a
    ring has to be sewn shut like this. Not a workaround of ours — it is
    what KiCad's own Eagle import does with a restrict circle (ground
    truth: 89 points spanning radius 1.900..2.100 for a ⌀4 mm circle drawn
    with a 0.2 mm pen)."""
    if not outer or not inner:
        raise ValueError("кольцо: одна из сторон пуста")
    rev = []
    for kind, ax, ay, bx, by, curve in reversed(inner):
        rev.append((kind, bx, by, ax, ay, -curve))
    bridge_out = ("line", outer[-1][3], outer[-1][4], rev[0][1], rev[0][2], 0.0)
    bridge_back = ("line", rev[-1][3], rev[-1][4], outer[0][1], outer[0][2], 0.0)
    return [*outer, bridge_out, *rev, bridge_back]


def contour_edges(vertices) -> list[Edge]:
    """Vertices straight to edges, no offsetting — for a contour that is
    already the boundary."""
    edges: list[Edge] = []
    n = len(vertices)
    for i, (x1, y1, curve) in enumerate(vertices):
        x2, y2, _ = vertices[(i + 1) % n]
        if math.hypot(x2 - x1, y2 - y1) < EPS:
            continue
        edges.append(("arc" if curve else "line", x1, y1, x2, y2, float(curve or 0.0)))
    return edges


def copper_boundary(polygon) -> list[Edge]:
    """Граница меди IR-полигона: его вершины, сдвинутые на `width/2` наружу.
    `curve` вершин хранится в мград, здесь всё считается в градусах."""
    vertices = [(float(v.x), float(v.y), (v.curve or 0) / 1000) for v in polygon.vertices]
    return offset_contour(vertices, polygon.width / 2)


def subtracted_area(g) -> list[Edge]:
    """Область, которую занял бы этот объект на базовом слое, — то самое,
    что вычитает [анти-объект](../spec/layer-model.md): «анти-объект
    вычитает ровно ту область, которую занял бы такой же объект».

    Каждый вид отвечает своим правилом, и все три уже есть выше:

    | объект | область |
    |---|---|
    | полигон | контур пером, наружу на `width/2` |
    | фигура, `outline=0` | сама фигура |
    | фигура, `outline>0` | **кольцо** этой толщины — «замочной скважиной» |
    | линия, дуга | капсула шириной `width` |

    Текста здесь нет и быть не может: площадь буквы — это её глифы, а
    раскладывать шрифт в контуры конвертер не станет. Вызывающий ловит
    `ValueError` и говорит об этом своими словами."""
    from .graphics import Arc, Line, Polygon, Shape

    if isinstance(g, Polygon):
        return copper_boundary(g)
    if isinstance(g, Shape):
        if g.outline <= 0:
            return contour_edges(shape_contour(g))
        return keyhole(contour_edges(shape_contour(g, g.outline / 2)),
                        contour_edges(shape_contour(g, -g.outline / 2)))
    if isinstance(g, (Line, Arc)):
        curve = getattr(g, "curve", 0) or 0
        return capsule(g.x1, g.y1, g.x2, g.y2, curve / 1000, g.width)
    raise ValueError(f"{type(g).__name__} площади не имеет")
