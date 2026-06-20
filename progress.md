# Babel — Progress

Универсальный конвертер EDA-форматов (Eagle ↔ KiCad ↔ Altium) через XML-IR.
Область применимости: микроконтроллерный мир (STM32 144 пина — ок; FPGA/процессоры
на 400–1000 пинов BGA — вне области, см. ниже почему multi-gate всё равно нужен).

Единицы IR: **целые микрометры (µm), int32, Y вверх**.

## Архитектура IR (актуальная схема)

Полное описание — в [ir_schema.md](ir_schema.md). Ключевое:

### Пул символов + gate
- Символы живут в библиотечном пуле: `<library><symbols><symbol name="..."/></symbols>`.
  Описываются один раз, на них ссылаются компоненты (дедуп: 10 транзисторов → один
  `<symbol name="NPN">`).
- **Одногейтовый (single-mode) компонент**: `<component name="R" prefix="R" symbol="R">`
  — есть атрибут `symbol`, нет `<gate>`, пины в pin-mapping голые (`pin="1"`).
- **Многогейтовый (multi-mode) компонент**: нет атрибута `symbol`, есть дочерние
  `<gate name="A" symbol="OPAMP"/>`, пины в pin-mapping с префиксом гейта
  (`pin="A.OUT"`).
- **Правило определения режима** (в [babel/ir_util.py](babel/ir_util.py)):
  `comp.get('symbol') is None and comp.find('gate') is not None` → multi-mode.
  При парсинге: `len(gates) > 1` → multi-mode.
- Почему не схлопнуть multi в один символ: следующий шаг — импорт целых схем; гейты
  могут быть на РАЗНЫХ страницах, мердж исказит схему. Это не усложнение, а отражение
  действительности.

### Сознательно НЕ хранится
- Eagle gate `add` (next/always/request/can) и `swaplevel` — нужны только редактору схем,
  не для конвертации. У человека, использующего такие компоненты, хватит знаний вручную.
- Цвета и заливки. `rectangle` → 4 линии, `circle` → arc (start=0 sweep=360).
  Прямоугольники с заливкой встречаются в основном в Altium-символах ИС и в Eagle-символе
  транзистора; транзистор от замены заливки на контур не страдает. IR не хранит цветов,
  поэтому заливка без цвета создавала бы артефакты.
- `G$1` — Eagle-автоимя гейта (как `N$1` для цепей). В IR не копируется: у single-mode
  гейта вообще нет имени (только атрибут `symbol`). Это упрощает IR.

### Терминология
- Используем `name`, а не `id` — называем вещи своими именами, а не IT-терминами.
  `name` компонента/футпринта = реальное уникальное имя.

### TH-пады
- Eagle даёт оба свойства напрямую: `drill` (диаметр ОТВЕРСТИЯ) + `diameter` (диаметр
  медного кольца/пада). Оба — часть IR.
- Altium не имеет концепции TH-пада: это SMD-пад с просверленной дыркой. Обычно круглый
  (sizeX=sizeY); если нет — берём МЕНЬШЕЕ из двух как `diameter`.
- KiCad имеет отдельный тип TH-падов («своя шиза» — разберёмся при написании парсера).
- Фоллбэк при экспорте, если `diameter` нет: `drill × 1.8`.

### model3d
- Только `tx ty tz rx ry rz` (соглашение Eagle, повороты ZYX). Имя файла не хранится —
  выводится из имени футпринта.

## Состояние модулей

Все консьюмеры приведены к схеме пул-символов + gate и проверены:

| Модуль | Статус |
|---|---|
| [babel/ir_util.py](babel/ir_util.py) | готов (`symbol_pool`, `component_gates`, `is_multi_gate`) |
| [babel/eagle_parser.py](babel/eagle_parser.py) | готов (полностью переписан) |
| [babel/eagle_exporter.py](babel/eagle_exporter.py) | готов (правильно строит multi-gate gates+connects) |
| [babel/altium_parser.py](babel/altium_parser.py) | **готов (доделан в этой сессии)** |
| [babel/altium_exporter.py](babel/altium_exporter.py) | готов (multi-gate флэттится с warning) |
| [babel/kicad_exporter.py](babel/kicad_exporter.py) | готов (multi-gate флэттится с warning) |
| [babel/svg_renderer.py](babel/svg_renderer.py) | готов (мерджит все гейты пула) |
| [babel/app.py](babel/app.py) | готов |

## Сделано в этой сессии

1. **Доделан [babel/altium_parser.py](babel/altium_parser.py)** — последний непереведённый
   парсер. Был последним «embedded `<symbol>` в компоненте», теперь:
   - `_convert_symbol(sym, sym_name)` возвращает отдельный `<symbol name=...>` для пула
     (раньше делал `ET.SubElement(comp_el, 'symbol')`).
   - `_convert_component(comp, schlib_cache, pcblib_cache, pool, symbols_el)` —
     регистрирует символ в пуле один раз (дедуп по имени), ссылается через
     `comp_el.set('symbol', sym_name)`.
   - `convert()` создаёт `<symbols>` первым ребёнком `<library>`, заводит `pool: dict`.
   - Исправлены остатки `id=` → `name=` на `<component>` и `<footprint>`.
   - Счётчик компонентов теперь `lib_el.findall('component')` (не считает `<symbols>`).
   - `_do_model_extraction` и find-запросы уже использовали `@name` — не трогали.
     `model.id` на строке ~392 — это GUID Altium-модели, не IR-id, оставлен.

2. **Исправлен [babel/kicad_exporter.py](babel/kicad_exporter.py)** — санитизация имён
   футпринтов с недопустимыми для имени файла символами (`/`, `:`, и т.п.) перед записью
   `.kicad_mod`. Добавлен `import re`. (Падал на футпринте `SMA / DO-214AC`.)

## Проверка (Altium IntLib → IR → все экспортёры)

`testData/GessorLib/.../gessor_lib.IntLib` → `testData/gessor.ir.xml`:
- 47 компонентов, пул из 47 символов, голые single-mode pin-mapping, ни одного
  embedded `<symbol>` в компонентах.
- Тот же IR прогнан через все три экспортёра без ошибок:
  - Altium SchLib/PcbLib/LibPkg ✓
  - Eagle `.lbr` ✓
  - KiCad `.kicad_sym` + 45 `.kicad_mod` ✓ (`SMA / DO-214AC` → `SMA _ DO-214AC.kicad_mod`)

Раньше проверено на `r_eagle.lbr` (через `r_eagle_exp.lbr`): 4 компонента
(R, NPN-PB, STM32F205RBT6, STM32F105RCT6) = 4 deviceset; ZD и M03 — orphan-символы,
остаются только в пуле (не становятся фейковыми компонентами). Маппинги: R (1→1, 2→2),
NPN-PB (B→1, C→3, E→2).

### Команды для воспроизведения
```bash
cd c:/NoABS/Babel
python -m babel.altium_parser "testData/GessorLib/Project Outputs for gessor_lib/gessor_lib.IntLib" testData/gessor.ir.xml
# затем прогон через экспортёры — см. историю сессии
```

## Сделано в этой сессии (миграция единиц)

**Все 6 модулей переведены на µm. IR теперь хранит целые микрометры.**

Итог миграции:
- **[babel/eagle_parser.py](babel/eagle_parser.py)**: добавлен `_um(mm)=str(round(float(mm)*1000))`;
  все геометрические атрибуты (x, y, width, drill, r, size, pin length) → µm int;
  углы (rot, start, sweep, curve) остались float через `fmt()`.
- **[babel/altium_parser.py](babel/altium_parser.py)**: `_MILS_TO_MM` → `_MILS_TO_UM=25.4`;
  `_mm(mils)` → `_um(mils)=str(round(mils*25.4))`; `_f()` оставлен только для углов/roundness;
  `_INTERNAL_TO_MM` → `_INTERNAL_TO_UM=25.4/10000`; все жёсткие константы в тексте
  (`'1.27'`→`'1270'`, `'2.54'`→`'2540'` и т.д.).
- **[babel/eagle_exporter.py](babel/eagle_exporter.py)**: добавлен `_tomm(um)=float(um)/1000`;
  все IR-атрибуты перед записью в Eagle конвертируются через `_tomm()`; arc-расчёт
  делит cx/cy/r на 1000 перед тригонометрией; model3d tx/ty/tz тоже /1000.
- **[babel/altium_exporter.py](babel/altium_exporter.py)**: `_mils(v)=round(float(v)/25.4)`;
  дефолты `2.54`→`2540`, `0.1`→`100`.
- **[babel/kicad_exporter.py](babel/kicad_exporter.py)**: добавлен `_mm(um)=float(um)/1000`;
  `_ky(y)` теперь возвращает `-float(y)/1000`; все геом. атрибуты через `_f(_mm(...))`;
  дефолты обновлены (`'1.27'`→`'1270'`, `'2.54'`→`'2540'`, `'0.12'`→`'120'`).
- **[babel/svg_renderer.py](babel/svg_renderer.py)**: добавлен `_v(um)=float(um)/1000`;
  renderer по-прежнему работает в мм-пространстве, но читает IR через `_v()`;
  дефолты обновлены (`'0.1524'`→`'152'`, `'2.54'`→`'2540'`, `'1.0'`→`'1000'`).

### Проверка (µm-IR round-trip)

```
python -m babel.altium_parser "testData/GessorLib/.../gessor_lib.IntLib" testData/gessor.ir.xml
# → 47 компонентов, все координаты целые µm
python -m babel.eagle_exporter testData/gessor.ir.xml testData/gessor_roundtrip.lbr  # ✓
python -m babel.kicad_exporter testData/gessor.ir.xml testData/gessor_kicad          # ✓ 47 sym, 45 fp
python -m babel.altium_exporter testData/gessor.ir.xml testData/gessor_out           # ✓ 47 components
python -m babel.eagle_parser testData/r_eagle.lbr testData/r_eagle.ir.xml            # ✓
python -m babel.eagle_exporter testData/r_eagle.ir.xml testData/r_eagle_exp.lbr      # ✓
# Spot-check: wire x1="-2540" → Eagle -2.54mm ✓; smd dx=500 → Eagle 0.5mm ✓
```

## 3D-модели — роадмап (решения зафиксированы, код не написан)

Конвенции уже записаны в [ir_schema.md](ir_schema.md), раздел `<model3d>`. Кратко:
- **Размещение = `T·R`**: поворот вокруг начала модели, затем трансляция `(tx,ty,tz)` в
  **фрейме платы** (оси сдвига от поворота не плывут; `tz` = высота над платой). MCAD-стандарт.
- **Только STEP, 1:1.** `scale` не поддерживается (всегда `1 1 1`; ненулевой scale при
  импорте из KiCad → warning).
- **Sidecar = канон IR**: IR байты не несёт, привязка по имени корпуса. Нет файла →
  модель теряется, плата остаётся (приемлемо).
- **Встраивание при экспорте**: Altium → в `.PcbLib`; KiCad ≥ 10 → `embedded_files` в
  `.kicad_mod`; Eagle → sidecar рядом с `.lbr`.

Текущее состояние кода: `model3d` **пишется только Eagle-экспортёром** (как комментарий);
[altium_exporter.py](babel/altium_exporter.py) и [kicad_exporter.py](babel/kicad_exporter.py)
его **пропускают**.

Что предстоит (в этом порядке логично делать):

1. ~~**Переход IR-поворотов на MCAD-конвенцию**~~ **✓ Сделано.**
   IR хранит MCAD intrinsic XYZ (как Altium). Altium-парсер — pass-through.
   Eagle-парсер/экспортёр: `_eagle_to_mcad_rot` / `_mcad_to_eagle_rot` (симметричный
   `Rx(±90°)` + ZYX↔XYZ). `_altium_to_eagle_rot` удалён.
2. **KiCad-углы** — отдельный конвертер (KiCad `rotate (xyz)`, градусы, фиксированный
   порядок/знаки — классика «STEP Z-up → rotate X −90»). Порядок/знаки прибить эмпирически
   round-trip-тестом.
3. **Реализация встраивания/извлечения**:
   - altium_exporter: встроить sidecar-STEP в `.PcbLib` (сейчас `model3d` пропускается).
   - kicad_exporter: писать `(model …)` + `embedded_files` (v10+); путь к модели
     синтезировать из имени корпуса (в IR пути нет).
   - kicad_parser (когда будет): извлекать `embedded_files`/внешнюю ссылку в sidecar,
     отбрасывать путь, привязка по имени; ненулевой scale → warning.

## Решение: Altium-таргет = DBLIB (не деградируем в «толстый» IntLib)

Обсуждали уход на голый self-contained IntLib (копии символов на компонент, без Excel/ODBC),
т.к. DBLIB исторически поздний и многих отпугивает. **Решение — остаёмся на DBLIB**
(SchLib один-на-пул-символ + xlsx = компоненты, дедуп сохранён). Принцип: не деградировать
логичную IR-структуру ради бедности тула; нормализованные библиотеки делают проект
логичнее. **TODO:** при DBLIB-экспорте выдавать пользователю предупреждение, что для работы
нужна привязка к xlsx через ACE OLEDB (это требование Altium, не наш каприз). См. memory
`feedback_no_pandering.md`.

## Отложено (deferred) — следующие шаги

1. **Переработка экспорта символов Altium: явный pin→pad маппинг + полноценный multi-gate.**
   Сейчас экспортёр Altium и KiCad флэттят multi-gate в один символ с warning, а pin→pad
   маппинг теряет лишние пады. В тестовых данных multi-gate нет, путь непротестирован.
   Eagle-экспортёр и multi-gate, и multi-pad делает корректно.

   **Задание (Altium):**

   **(a) Явный pin→pad маппинг через `MAP_DEFINER` (поддержка «много падов на пин»).**
   - Решение: маппинг — явная connect-таблица на компоненте (как Eagle `<connect>`),
     НЕ полагаемся на неявное совпадение имён пад=пин. См. [ir_schema.md](ir_schema.md)
     раздел «Один пин → несколько падов».
   - IR-канон: пады на один пин перечислены через пробел в одном `<map pad="4 9" pin=...>`.
     Имена падов **без пробелов** (пробел = разделитель списка). Зафиксировано в схеме.
   - Формат Altium (проверено в altium-monkey, запись `MAP_DEFINER`,
     [altium_record_sch__implementation.py:221](file:///C:/Users/j3qq4hch/miniconda3/Lib/site-packages/altium_monkey/altium_record_sch__implementation.py#L221)):
     `DesIntf` = designator пина символа; `DesImpCount` + `DesImp0..N` = **список** падов.
     То есть Altium нативно умеет «1 пин → N падов» без переименования падов футпринта.
     Надо найти, как `altium_schlib`/`AltiumSymbol` принимает `MAP_DEFINER` на запись, и
     эмитить его явно для каждого пина.
   - Фиксы в коде:
     - [altium_exporter.py:117](babel/altium_exporter.py#L117) `_pin_des_map` — строить
       `{pin: [pads]}` (группировка), а не `{pin: pad}` (перезапись затирает лишние пады).
     - убрать `.split()[0]` на [строке 126](babel/altium_exporter.py#L126) — брать **весь**
       список падов из `pad.split()`, а не первый.
     - в `_add_gate_to_symbol` пин эмитится с designator из первого пада (для отрисовки),
       но полная связь пин→пады уходит в `MAP_DEFINER`, не в designator пина.

   **(b) Полноценный multi-gate (parts) — altium-monkey всё умеет, путь ясен:**
   - DbLib-строка multi-gate НЕ выражает — и не должна. Multi-part живёт в SchLib-символе
     (`Part Count > 1`), а строка таблицы ссылается на него по имени через `Library Ref`.
     Altium сам раскидывает `U1A/U1B/…`.
   - API подтверждён: `AltiumSymbol.set_part_count(N)`; `AltiumSchPin(owner_part_id=i)`;
     у `add_line/add_arc/add_rectangle/add_designator/add_parameter` есть `owner_part_id`
     (дефолт `-1` = общая для всех частей). Части 1-индексные.
   - Архитектура экспортёра: **одногейтовые** — как сейчас, `Library Ref` → пул-символ
     (дедуп, `owner_part_id=-1`). **Многогейтовые** — эмитить ОТДЕЛЬНЫЙ multi-part
     SchLib-компонент с именем компонента (напр. `LM358`), `set_part_count(N)`, каждый
     IR-гейт → часть `i`: его пул-символьная геометрия и пины с `owner_part_id=i`;
     pad→`ГЕЙТ.пин` даёт designator пина внутри части. Строка DbLib: `Library Ref`=имя
     компонента. (Multi-part в Altium per-component, дедуп к ним и не применим.)
   - Блокеры multi-gate: [altium_exporter.py:263](babel/altium_exporter.py#L263)
     `sym_name = gates[0][1]` — берёт лишь первый гейт; и [`_add_gate_to_symbol`](babel/altium_exporter.py#L99)
     валит всё в одну часть без `owner_part_id`.

   **(c) Multi-gate KiCad (units)** — гейты → units одного символа в `.kicad_sym`
   (сейчас тоже флэттится с warning). Делать после Altium.

   > Один и тот же паттерн «берём только первое» сидит и в multi-gate (первый гейт), и в
   > pin-mapping (первый пад). Правятся вместе.
2. **Импорт целых схем Eagle** (.sch + .brd) — ради этого и сохраняем multi-gate.
3. **KiCad-парсер/импортёр** — включая «шизу» TH-падов KiCad.
4. **Сверить имена слоёв в [ir_schema.md](ir_schema.md)**: линтер поменял на
   `silk_top`/`silk_bottom` (строки ~196–197), но код использует
   `silkscreen`/`bottom_silk`. Привести к одному варианту.
5. **Настоящая иерархия KiCad** (один лист ×N) — отложена (см. memory
   `project_deferred_hierarchy.md`).
6. **PI/SI аннотации на пинах** (consumption и др.) — отложены до проектирования swift
   (memory `project_swift_pi.md`).
7. **Altium DbLib → IR (асимметрия импорта/экспорта).** Сейчас `altium_parser.py` читает
   только IntLib; экспорт же сознательно выдаёт DbLib (см. решение выше). Нужно научиться
   читать и DbLib: распарсить `.DbLib` (ini, пути к SchLib/PcbLib/xlsx + маппинг колонок),
   прочитать SchLib/PcbLib через уже использующиеся `AltiumSchLib`/`AltiumPcbLib`, прочитать
   таблицу через `openpyxl`, и собрать `<component>` на каждую строку xlsx (Library Ref →
   символ, Footprint Ref → футпринт). По сути инверсия `_build_component_rows` +
   `_export_schlib_symbol` + `_export_footprint` из `altium_exporter.py`.
8. **Дедупликация символов при импорте IntLib.** SchLib не умеет «один символ — много
   компонентов» — каждая запись в каталоге хранит полную копию геометрии, даже если она
   визуально идентична соседней (типичный кейс — вендорский каталог, символ скопирован
   в N карточек разных номиналов/корпусов). План: после парсинга каждого символа в IR
   канонизировать его представление (нормализованный XML) и хешировать; компоненты с
   одинаковым хешем символа схлопывать в одну запись `<symbols>`-пула. Не поможет для
   «нарисовано похоже, но не идентично» — это не чинится автоматически.

## Полезные детали API
- `AltiumSchLib(path)` — конструктор; `AltiumSchLib.get_symbol_names(path)` — статик,
  нужен путь к файлу.
- `AltiumPcbLib.from_file(path)` + `find_footprint(name)` (нет `get_footprint`).
- cp1251-ошибка консоли на символе `→` безвредна, файл пишется корректно (UTF-8).
