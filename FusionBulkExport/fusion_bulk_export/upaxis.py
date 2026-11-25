"""Which way is up in a design: Y or Z. Imports nothing from Fusion (read with duck typing).

A Fusion design is Y-up or Z-up, depending on the modeling-orientation preference in force when it
was created. The API has no documented property of a design for it, and the preference itself
(generalPreferences.defaultModelingOrientation) only applies to new designs, so it is never used
here. Instead the design's own standard named views are read: in a Z-up design the Top view looks
down -Z and the Front view's up vector is +Z; in a Y-up design they are -Y and +Y.

Everything that decides the answer is in up_axis(), so a better signal is a one-place change.
"""
import math

# A direction counts as along an axis when that component of the unit vector is at least this.
TOLERANCE = 0.9
AXES = ('x', 'y', 'z')


def up_axis(design):
    """("y" | "z" | None, reason or None) for an open design, read only from its named views:
    the Top view's view direction (target - eye) must be about -Z (or -Y), and the Front view's
    up vector about +Z (or +Y), and both must agree. Anything else (a view missing, a vector
    along neither axis, the two disagreeing, the API raising) gives None with a short reason.

    Read only: no camera is set, the viewport is never touched (no goHome, no fit)."""
    try:
        views = getattr(design, 'namedViews', None)
        if views is None:
            return None, 'the design has no named views'
        top = camera_of(views, 'topNamedView', 'Top')
        if isinstance(top, str):
            return None, top
        front = camera_of(views, 'frontNamedView', 'Front')
        if isinstance(front, str):
            return None, front
        looking = difference(top.target, top.eye)
        from_top = along(looking, -1)
        if from_top is None:
            return None, 'the Top view does not look down Y or Z'
        from_front = along(vector(front.upVector), 1)
        if from_front is None:
            return None, "the Front view's up is neither +Y nor +Z"
        if from_top != from_front:
            return None, 'the Top view says %s up, the Front view %s up' % (from_top.upper(), from_front.upper())
        return from_top, None
    except Exception as error:
        return None, 'named views unreadable: ' + (str(error) or type(error).__name__)


def camera_of(views, name, label):
    """The named view's camera, or a reason (a string) when there is none."""
    view = getattr(views, name, None)
    if view is None:
        return 'the design has no %s view' % label
    camera = getattr(view, 'camera', None)
    if camera is None:
        return 'the %s view has no camera' % label
    return camera


def vector(value):
    return (float(value.x), float(value.y), float(value.z))


def difference(target, eye):
    return tuple(t - e for t, e in zip(vector(target), vector(eye)))


def along(direction, sign):
    """'y' or 'z' when the unit vector of direction is within TOLERANCE of sign times that axis
    (sign -1: pointing down it); None otherwise (x, slanted, zero, the wrong sign)."""
    length = math.sqrt(sum(component * component for component in direction))
    if not length or math.isnan(length) or math.isinf(length):
        return None
    for axis, component in zip(AXES, direction):
        if axis != 'x' and component * sign / length >= TOLERANCE:
            return axis
    return None
