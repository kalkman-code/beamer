"""The pairing QR, drawn with Core Image's CIQRCodeGenerator through PyObjC, which adds no dependency.

The generator gives one pixel per module; this reads them back into a grid and draws it as
square modules, dark on a light tile with the four-module quiet zone every reader wants, whatever
the window's appearance is.
"""

import AppKit
import Foundation
import Quartz

QUIET_MODULES = 4


def modules(text):
    """The QR's modules as rows of booleans (True is dark), without any border. ValueError when
    the text is too long for a code."""
    data = text.encode("utf-8")
    generator = Quartz.CIFilter.filterWithName_("CIQRCodeGenerator")
    generator.setValue_forKey_(Foundation.NSData.dataWithBytes_length_(data, len(data)), "inputMessage")
    generator.setValue_forKey_("M", "inputCorrectionLevel")
    output = generator.outputImage()
    if output is None or output.extent().size.width < 1:
        raise ValueError("the text does not fit in a QR code")
    size = int(output.extent().size.width)
    rep = AppKit.NSBitmapImageRep.alloc().initWithCIImage_(output)
    grid = [[rep.colorAtX_y_(x, y).redComponent() < 0.5 for x in range(size)] for y in range(size)]
    # The generator's own one-module border, which the quiet zone replaces.
    return [row[1:-1] for row in grid[1:-1]]


def image(text, side):
    """An NSImage showing `text` as a QR, square and no bigger than `side` points: a whole number
    of points to a module, so the edges are sharp."""
    grid = modules(text)
    cells = len(grid) + 2 * QUIET_MODULES
    unit = max(1, int(side // cells))
    side = unit * cells

    def draw(_rect):
        AppKit.NSColor.whiteColor().setFill()
        AppKit.NSRectFill(Foundation.NSMakeRect(0, 0, side, side))
        AppKit.NSColor.blackColor().setFill()
        for row, line in enumerate(grid):
            for column, dark in enumerate(line):
                if dark:
                    # NSImage's drawing space has its origin bottom left; the grid's row 0 is the top.
                    AppKit.NSRectFill(Foundation.NSMakeRect(
                        (column + QUIET_MODULES) * unit, side - (row + QUIET_MODULES + 1) * unit, unit, unit))
        return True

    return AppKit.NSImage.imageWithSize_flipped_drawingHandler_((side, side), False, draw)
