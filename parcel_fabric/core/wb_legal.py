"""Copied from COGO Workbench (same author, GPL-2.0-or-later). Keep in sync by hand."""
"""Conservative, offline extraction of explicit metes-and-bounds calls."""
from dataclasses import dataclass
import re

from .wb_engine import Course, angle, bearing, traverse


NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
UNIT = r"(?:us\s+survey\s+feet|international\s+feet|feet|foot|ft\.?|meters?|metres?|m\b|')"
QUADRANT = re.compile(r'''\b(north|south|n|s)\s*(\d[\w\s.°:'"′″-]{0,100}?)\s*(?<![a-z])(east|west|e|w)\b''', re.I)
CARDINAL = re.compile(r'\b(?:due\s+)?(north|south|east|west|n|s|e|w)\b', re.I)
POB = re.compile(r'\b(?:true\s+)?point\s+of\s+beginning\b|\b(?:T\.?\s*)?P\.?\s*O\.?\s*B\b\.?', re.I)
SPLIT = re.compile(r';|\bthence\b|\b(?=containing\s+[\d.,]+\s+acres?\b)|\n(?=\s*(?:\d+[.)]\s*)?(?:north|south|east|west|[NSEW])(?:\b|(?=\d)))|(?<=[.!?])\s+(?=(?:north|south|east|west)\b|[NS]\s*\d)', re.I)


@dataclass
class LegalCall:
    source: str
    phase: str = 'Boundary'
    course: Course = None
    unit: str = ''
    issue: str = ''


def _angle(text):
    text = re.sub(r'\b(?:degrees?|deg)\.?', ':', text, flags=re.I)
    text = re.sub(r'\b(?:minutes?|min)\.?', ':', text, flags=re.I)
    text = re.sub(r'\b(?:seconds?|sec)\.?', '', text, flags=re.I)
    text = text.translate(str.maketrans({'°': ':', "'": ':', '′': ':', '"': '', '″': ''}))
    text = re.sub(r'\s*:\s*', ':', text).strip(' :')
    angle(text)
    return text


def _directions(text):
    matches = list(QUADRANT.finditer(text))
    if matches:
        result = []
        for match in matches:
            ns, value, ew = match.groups()
            direction = ns[0].upper() + _angle(value) + ew[0].upper()
            bearing(direction)
            result.append((direction, match.start(), match.end()))
        return result
    return [(m[1][0].upper(), m.start(), m.end()) for m in CARDINAL.finditer(text)]


def _unit(text):
    value = text.lower().strip().rstrip('.')
    if value.startswith('us'):
        return 'US survey feet'
    if value.startswith('international'):
        return 'International feet'
    if value in ('feet', 'foot', 'ft', "'"):
        return 'Feet (choose definition)'
    return 'Meters'


def _measurement(text, pattern, label):
    matches = list(re.finditer(pattern + rf'\s*({NUMBER})(?!\d|\.\d)\s*({UNIT})?(?![a-z])', text, re.I))
    if len(matches) > 1:
        raise ValueError(f'Multiple {label} values; separate the calls.')
    if not matches:
        return None
    match = matches[0]
    if not match[2] and re.match(r'[a-z]', text[match.end():].lstrip(), re.I) and not re.match(
            r'(?:and|with|arc|chord|delta|tangent|radius|to|having)\b', text[match.end():].lstrip(), re.I):
        raise ValueError(f'Unrecognized unit after {label}.')
    return match[1].replace(',', ''), _unit(match[2]) if match[2] else ''


def _curve(text, direction, begin):
    turns = {turn.lower() for turn in re.findall(r'\bcurve\s+(?:to\s+)?(?:the\s+)?(left|right)\b', text, re.I)}
    if len(turns) != 1:
        raise ValueError('Curve needs an explicit left or right turn.')
    prefix = text[:begin]
    basis = re.search(r'\b(chord|tangent)\s+(?:bearing(?:\s+of)?|bears?)\s*[:=]?\s*$', prefix, re.I)
    if not basis:
        raise ValueError('Label the curve direction as chord bearing or tangent bearing.')
    radius = _measurement(text, r'\bradius\s*(?:of\s*)?[:=]?', 'radius')
    arc = _measurement(text, r'\barc\s*(?:length|distance)?\s*(?:of\s*)?[:=]?', 'arc length')
    chord = _measurement(text, r'\bchord\s*(?:length|distance)\s*(?:of\s*)?[:=]?', 'chord length')
    delta_match = re.search(r'''\b(?:delta|central\s+angle)\s*(?:of\s*)?[:=]?\s*(\d[\d\s.°:'"′″-]*(?:(?:degrees?|minutes?|seconds?)\b[\d\s.°:'"′″-]*)*)''', text, re.I)
    delta = _angle(delta_match[1]) if delta_match else None
    if not radius or not (arc or chord or delta):
        raise ValueError('Curve needs a radius and an explicit arc length, chord length, or delta.')
    units = {u for value in (radius, arc, chord) if value for u in [value[1]] if u}
    unit = _compatible_unit(units)
    measure, value = ('Arc', arc[0]) if arc else ('Delta', delta) if delta else ('Chord', chord[0])
    if measure == 'Chord' and re.search(r'\bmajor\b', text, re.I):
        raise ValueError('A major arc requires arc length or delta; chord-only input selects the minor arc.')
    course = Course('Curve', direction, value, radius[0], measure,
                    next(iter(turns))[0].upper(), basis[1].title())
    segment = traverse((0, 0), [course])[0]
    if chord and abs(segment.distance - float(chord[0])) > max(0.02, segment.distance * 1e-5):
        raise ValueError('Chord length disagrees with the other curve dimensions (tolerance 0.02 or 0.001%).')
    if delta and abs(segment.delta - angle(delta)) > 1 / 3600:
        raise ValueError('Delta disagrees with the other curve dimensions (tolerance 1 arc-second).')
    return course, unit


def _compatible_unit(units):
    concrete = units - {'Feet (choose definition)'}
    if len(concrete) > 1 or ('Meters' in concrete and 'Feet (choose definition)' in units):
        raise ValueError('Mixed distance units; convert the calls to one unit before importing.')
    return next(iter(concrete or units), '')


def _call(source, phase, forced):
    result = LegalCall(source, phase)
    text = ' '.join(source.split())
    try:
        if re.search(r'\b(?:except(?:ing)?|less\s+and\s+except|spiral|compound\s+curve|reverse\s+curve)\b', text, re.I) or re.search(rf'{NUMBER}\s*(?:chains?|rods?|links?|varas?|yards?)\b', text, re.I):
            raise ValueError('Exception, approximate call, unsupported unit or complex curve; enter this description manually.')
        context = re.match(r'\s*(?:a\s+parcel|situat\w*)\b', text, re.I)
        anchor = re.match(r'\s*(?:beginning\s+at|commenc\w*)\b', text, re.I)
        heading = re.fullmatch(r'(?:proceed\s+)?(?:through\s+)?(?:the\s+)?following\s+courses\s*:', text, re.I)
        annotation = re.match(r'\s*(?:all\s+distances\b|containing\s+[\d.,]+\s+acres?\b)', text, re.I)
        if ((not forced and context) or anchor or heading or annotation) and not re.search(rf'{NUMBER}\s*{UNIT}\b', text, re.I) and not QUADRANT.search(text):
            return result
        if re.search(r'\bmore\s+or\s+less\b', text, re.I):
            raise ValueError('Approximate call; resolve the record distance manually.')
        directions = _directions(text)
        if not directions:
            raise ValueError('No explicit bearing found; supply the missing call or enter it manually.')
        if len(directions) != 1:
            raise ValueError('Multiple directions in one clause; separate calls with semicolons or thence.')
        direction, begin, end = directions[0]
        if re.search(r'\bcurve\b|\bradius\b', text, re.I):
            result.course, result.unit = _curve(text, direction, begin)
        else:
            distance = re.match(rf'\s*[,=:]?\s*(?:(?:for\s+)?(?:a\s+)?distance\s+(?:of\s+)?|for\s+)?({NUMBER})(?!\d|\.\d)\s*(?:({UNIT})(?![a-z]))?', text[end:], re.I)
            if not distance:
                # Legal calls commonly name the line being followed before giving its distance.
                distance = re.search(rf'\b(?:a\s+)?distance\s+of\s+({NUMBER})(?!\d|\.\d)\s*(?:({UNIT})(?![a-z]))?', text[end:], re.I)
                if distance:
                    prefix = text[end:end + distance.start()]
                    if not re.match(r'\s*,?\s*along\b', prefix, re.I) or re.search(rf'{NUMBER}\s*{UNIT}\b', prefix, re.I):
                        raise ValueError('Ambiguous wording between bearing and distance; separate or clarify the call.')
            if not distance:
                raise ValueError('No distance immediately after the bearing; use bearing, distance feet/meters.')
            remainder = text[end + distance.end():]
            if re.match(r'\s*(?:\d|/|,\d)', remainder):
                raise ValueError('Unrecognized distance format; use a decimal number.')
            if not distance[2] and re.match(r'[a-z/]', remainder.lstrip(), re.I) and not re.match(
                    r'(?:to|along|at|and|returning|on)\b', remainder.lstrip(), re.I):
                raise ValueError('Unrecognized distance unit or fraction; use decimal feet or meters.')
            if re.search(rf'{NUMBER}\s*{UNIT}\b|\b(?:then|along|north|south|east|west)\b\s+\d', remainder, re.I):
                raise ValueError('Additional measurements in this clause; separate the calls.')
            result.course = Course('Line', direction, distance[1].replace(',', ''))
            result.unit = _unit(distance[2]) if distance[2] else ''
        traverse((0, 0), [result.course])
    except ValueError as error:
        result.course = None
        result.issue = str(error)
    return result


def parse_description(text, boundary_only=False):
    """Return every clause for review, including unresolved and excluded clauses."""
    if not text.strip():
        raise ValueError('Paste a legal description first.')
    if len(text) > 100000:
        raise ValueError('Paste one parcel description at a time (100,000 characters maximum).')
    text = text.translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '−': '-'}))
    text = text.replace('**', '')
    commence = re.search(r'\bcommenc(?:e|ing|ement)\b', text, re.I)
    parts = [('Boundary', text)]
    ambiguity = ''
    if commence and not boundary_only:
        pob = POB.search(text, commence.end())
        if not pob:
            return [LegalCall(text, issue='Commencement found without an explicit point of beginning. Supply the POB or paste boundary calls only.')]
        if re.search(r'\bthence\b', text[pob.end():], re.I) or QUADRANT.search(text[pob.end():]) or CARDINAL.search(text[pob.end():]):
            parts = [('Commencement', text[:pob.end()]), ('Boundary', text[pob.end():])]
        else:
            ambiguity = ('Commence is followed only by a closing Point of Beginning. '
                'If all calls form the parcel boundary, select “All calls are boundary” and extract again; '
                'otherwise supply the boundary calls after the commencement tie.')
    rows = []
    for phase, part in parts:
        chunks = SPLIT.split(part.lstrip(' ;,.\n'))
        for index, chunk in enumerate(chunks):
            chunk = re.sub(r'^\s*(?:and\s+)?(?:\d+[.)]\s*)?', '', chunk, flags=re.I).strip(' ,;.\n')
            if chunk:
                rows.append(_call(chunk, phase, forced=index > 0))
    boundary = [row for row in rows if row.phase == 'Boundary']
    if ambiguity:
        rows.append(LegalCall('Commencement / closing POB', issue=ambiguity))
    if len(rows) > 2000:
        raise ValueError('Too many clauses (2,000 maximum); paste one parcel at a time.')
    try:
        declared = {_unit(value) for value in re.findall(
            rf'\b(?:all\s+)?distances\s+(?:are\s+)?(?:given\s+)?in\s+({UNIT})', text, re.I)}
        unit = _compatible_unit({row.unit for row in boundary if row.unit} | declared)
        for row in boundary:
            if row.course and not row.unit:
                row.unit = unit
    except ValueError as error:
        rows.append(LegalCall('Distance units', issue=str(error)))
    if not any(row.course for row in boundary):
        rows.append(LegalCall('Boundary', issue='No boundary courses extracted.'))
    return rows
