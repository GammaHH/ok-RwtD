"""Map a physical Android display to the saved 1080x2400 module canvas.

The game's upper controls move slightly when the viewport height changes,
while the lower toolbar is bottom anchored.  The flexible play area between
them absorbs most of the height change.
All saved module coordinates and COCO annotations remain in reference space.
"""
from dataclasses import dataclass
import re


REFERENCE_WIDTH = 1080
REFERENCE_HEIGHT = 2400
HEADER_ANCHOR = 150
TOP_ANCHOR = 600
BOTTOM_ANCHOR = 1600


def expected_size(value):
    """An explicit size is an assertion about the selected device, not a guess."""
    if not isinstance(value, str):
        raise ValueError('Display Size must be Auto or WIDTHxHEIGHT')
    if value.strip().lower() == 'auto':
        return None
    match = re.fullmatch(r'([1-9][0-9]{2,3})[xX×]([1-9][0-9]{2,3})', value.strip())
    if not match:
        raise ValueError('Display Size must be Auto or WIDTHxHEIGHT')
    return tuple(map(int, match.groups()))


@dataclass(frozen=True)
class ScreenGeometry:
    width: int
    height: int

    def __post_init__(self):
        if type(self.width) is not int or type(self.height) is not int:
            raise ValueError('Android frame dimensions must be integers')
        if not 400 <= self.width <= 4000 or self.height <= self.width:
            raise ValueError('Unsupported Android frame orientation or dimensions')
        # Do not silently automate on a layout radically different from the
        # annotated reference.  Such a layout requires a separate calibration.
        if not 2100 <= self.logical_height <= 2700:
            raise ValueError('Display aspect ratio needs calibration before input')

    @property
    def scale(self):
        return self.width / REFERENCE_WIDTH

    @property
    def logical_height(self):
        return round(self.height / self.scale)

    @property
    def anchors(self):
        # The 1080x2340 capture places the header about 10 px and the BUFF
        # controls about 18 px below their 1080x2400 reference positions.
        # Keep the lower toolbar bottom anchored.  These are logical pixels,
        # before applying the width scale for a different physical width.
        gap = REFERENCE_HEIGHT - self.logical_height
        physical = (0, HEADER_ANCHOR + gap / 6,
                    TOP_ANCHOR + gap / 3,
                    self.logical_height - (REFERENCE_HEIGHT - BOTTOM_ANCHOR),
                    self.logical_height)
        reference = (0, HEADER_ANCHOR, TOP_ANCHOR, BOTTOM_ANCHOR, REFERENCE_HEIGHT)
        return reference, physical

    def _logical_y(self, y):
        reference, physical = self.anchors
        for index in range(len(reference) - 1):
            if y <= reference[index + 1]:
                fraction = (y - reference[index]) / (reference[index + 1] - reference[index])
                return physical[index] + fraction * (physical[index + 1] - physical[index])
        raise ValueError('Y coordinate outside the 1080x2400 module canvas')

    def point(self, x, y):
        """Convert one reference-space touch point to physical pixels."""
        if (type(x) is not int or type(y) is not int or
                not 0 <= x < REFERENCE_WIDTH or not 0 <= y < REFERENCE_HEIGHT):
            raise ValueError('Touch point outside the 1080x2400 module canvas')
        return (min(self.width - 1, round(x * self.scale)),
                min(self.height - 1, round(self._logical_y(y) * self.scale)))

    def reference_point(self, x, y):
        """Convert an explicitly physical touch point to saved reference space.

        A Dashboard point is already in reference space and must not pass
        through this inverse conversion when its module is saved.
        """
        if (type(x) is not int or type(y) is not int or
                not 0 <= x < self.width or not 0 <= y < self.height):
            raise ValueError('Physical touch point outside the Android display')
        logical_y = y / self.scale
        reference, physical = self.anchors
        for index in range(len(physical) - 1):
            if logical_y <= physical[index + 1]:
                fraction = ((logical_y - physical[index]) /
                            (physical[index + 1] - physical[index]))
                return (min(REFERENCE_WIDTH - 1, round(x / self.scale)),
                        min(REFERENCE_HEIGHT - 1,
                            round(reference[index] + fraction *
                                  (reference[index + 1] - reference[index]))))
        raise ValueError('Physical touch point outside the mapped display')

    def physical_roi(self, roi):
        """Show where a saved reference-space ROI lies on the actual display."""
        import math

        if (not isinstance(roi, (list, tuple)) or len(roi) != 4 or
                any(type(value) is not int for value in roi)):
            raise ValueError('ROI must be [x,y,width,height] integers')
        x, y, width, height = roi
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > REFERENCE_WIDTH or y + height > REFERENCE_HEIGHT:
            raise ValueError('ROI outside the 1080x2400 module canvas')
        left = math.floor(x * self.scale)
        top = math.floor(self._logical_y(y) * self.scale)
        right = math.ceil((x + width) * self.scale)
        bottom = math.ceil(self._logical_y(y + height) * self.scale)
        return [left, top, right - left, bottom - top]

    def template_mapping(self, reference_bbox):
        """Size of a reference COCO template after this display's frame warp.

        The normalized frame is stretched by a different amount in each
        vertical band.  Weight bands crossed by a template rather than using
        one global height ratio.  Horizontal normalization restores 1080 px.
        """
        if (not isinstance(reference_bbox, (list, tuple)) or len(reference_bbox) != 4 or
                any(type(value) is not int for value in reference_bbox)):
            raise ValueError('Template bbox must be [x,y,width,height] integers')
        x, y, width, height = reference_bbox
        if (x < 0 or y < 0 or width <= 0 or height <= 0 or
                x + width > REFERENCE_WIDTH or y + height > REFERENCE_HEIGHT):
            raise ValueError('Template bbox outside the 1080x2400 canvas')
        reference, physical = self.anchors
        stretched_height = 0.0
        bands = []
        for index in range(len(reference) - 1):
            overlap = max(0, min(y + height, reference[index + 1]) - max(y, reference[index]))
            if overlap:
                factor = (reference[index + 1] - reference[index]) / (physical[index + 1] - physical[index])
                stretched_height += overlap * factor
                bands.append(index)
        mapped_height = max(1, round(stretched_height))
        return {'source_size': [width, height], 'mapped_size': [width, mapped_height],
                'scale_x': 1.0, 'scale_y': stretched_height / height,
                'bands': bands, 'physical_size': [self.width, self.height]}

    def map_template(self, template, reference_bbox):
        """Apply the frame warp's local size change to one COCO template."""
        import cv2
        import numpy as np

        mapping = self.template_mapping(reference_bbox)
        if (not isinstance(template, np.ndarray) or
                list(template.shape[1::-1]) != mapping['source_size']):
            raise ValueError('Template pixels do not match reference bbox')
        size = mapping['mapped_size']
        if size == mapping['source_size']:
            return template, mapping
        return cv2.resize(template, tuple(size), interpolation=cv2.INTER_CUBIC), mapping

    def normalize(self, frame):
        """Return a reference-space BGR frame for existing COCO/OCR/ROI code."""
        import cv2
        import numpy as np

        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or
                frame.shape != (self.height, self.width, 3)):
            raise ValueError('Physical frame does not match selected display size')
        if (self.width, self.height) == (REFERENCE_WIDTH, REFERENCE_HEIGHT):
            return frame
        scaled = cv2.resize(frame, (REFERENCE_WIDTH, self.logical_height), interpolation=cv2.INTER_LINEAR)
        reference, physical = self.anchors
        boundaries = [0] + [round(value) for value in physical[1:-1]] + [self.logical_height]
        segments = [cv2.resize(scaled[boundaries[index]:boundaries[index + 1]],
                               (REFERENCE_WIDTH, reference[index + 1] - reference[index]),
                               interpolation=cv2.INTER_LINEAR)
                    for index in range(len(reference) - 1)]
        return np.concatenate(segments, axis=0)

    def step(self, step):
        """Copy a validated module step, converting only physical touch fields."""
        result = dict(step)
        for x_key, y_key in (('x', 'y'), ('mid_x', 'mid_y'), ('end_x', 'end_y')):
            if x_key in result and y_key in result:
                result[x_key], result[y_key] = self.point(result[x_key], result[y_key])
        return result

    def calibrated_three_point_reference(self, points, module_id=None, step_id=None):
        """Lengthen uncalibrated 1080x2340 gestures using FOOD's measured ratio.

        Keep saved coordinates in reference space. FOOD and both ARMY modules
        are calibrated on the old phone, then adjusted only when sending input.
        """
        if (self.width, self.height) != (1080, 2340):
            return [list(point) for point in points]
        if len(points) != 3:
            raise ValueError('Three-point calibration requires start, middle and end')
        start_y = self.point(*points[0])[1]
        food_start = self.point(268, 2118)[1]
        food_before = self.point(278, 230)[1]
        food_after = self.point(278, 218)[1]
        factor = (food_start - food_after) / (food_start - food_before)
        adjusted = [list(points[0])]
        for x, y in points[1:]:
            physical_x, physical_y = self.point(x, y)
            extended_y = round(start_y + (physical_y - start_y) * factor)
            extended_y = min(self.height - 1, max(0, extended_y))
            adjusted.append(list(self.reference_point(physical_x, extended_y)))
        return adjusted
