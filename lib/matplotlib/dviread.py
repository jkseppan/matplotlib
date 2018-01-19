"""
A module for reading dvi files output by TeX. Several limitations make
this not (currently) useful as a general-purpose dvi preprocessor, but
it is currently used by the pdf backend for processing usetex text.

Interface::

  with Dvi(filename, 72) as dvi:
      # iterate over pages:
      for page in dvi:
          w, h, d = page.width, page.height, page.descent
          for x,y,font,glyph,width in page.text:
              fontname = font.texname
              pointsize = font.size
              ...
          for x,y,height,width in page.boxes:
              ...

"""
from collections import namedtuple
import enum
from functools import lru_cache, partial, wraps
import logging
import os
import re
import struct
import sqlite3
import sys
import textwrap
import numpy as np
import zlib

from matplotlib import cbook, get_cachedir, rcParams
from matplotlib.compat import subprocess

_log = logging.getLogger(__name__)

# Dvi is a bytecode format documented in
# http://mirrors.ctan.org/systems/knuth/dist/texware/dvitype.web
# http://texdoc.net/texmf-dist/doc/generic/knuth/texware/dvitype.pdf
#
# The file consists of a preamble, some number of pages, a postamble,
# and a finale. Different opcodes are allowed in different contexts,
# so the Dvi object has a parser state:
#
#   pre:       expecting the preamble
#   outer:     between pages (followed by a page or the postamble,
#              also e.g. font definitions are allowed)
#   page:      processing a page
#   post_post: state after the postamble (our current implementation
#              just stops reading)
#   finale:    the finale (unimplemented in our current implementation)

_dvistate = enum.Enum('DviState', 'pre outer inpage post_post finale')

# The marks on a page consist of text and boxes. A page also has dimensions.
Page = namedtuple('Page', 'text boxes height width descent')
Text = namedtuple('Text', 'x y font glyph width')
Box = namedtuple('Box', 'x y height width')


# Opcode argument parsing
#
# Each of the following functions takes a Dvi object and delta,
# which is the difference between the opcode and the minimum opcode
# with the same meaning. Dvi opcodes often encode the number of
# argument bytes in this delta.

def _arg_raw(dvi, delta):
    """Return *delta* without reading anything more from the dvi file"""
    return delta


def _arg(bytes, signed, dvi, _):
    """Read *bytes* bytes, returning the bytes interpreted as a
    signed integer if *signed* is true, unsigned otherwise."""
    return dvi._arg(bytes, signed)


def _arg_slen(dvi, delta):
    """Signed, length *delta*

    Read *delta* bytes, returning None if *delta* is zero, and
    the bytes interpreted as a signed integer otherwise."""
    if delta == 0:
        return None
    return dvi._arg(delta, True)


def _arg_slen1(dvi, delta):
    """Signed, length *delta*+1

    Read *delta*+1 bytes, returning the bytes interpreted as signed."""
    return dvi._arg(delta+1, True)


def _arg_ulen1(dvi, delta):
    """Unsigned length *delta*+1

    Read *delta*+1 bytes, returning the bytes interpreted as unsigned."""
    return dvi._arg(delta+1, False)


def _arg_olen1(dvi, delta):
    """Optionally signed, length *delta*+1

    Read *delta*+1 bytes, returning the bytes interpreted as
    unsigned integer for 0<=*delta*<3 and signed if *delta*==3."""
    return dvi._arg(delta + 1, delta == 3)


_arg_mapping = dict(raw=_arg_raw,
                    u1=partial(_arg, 1, False),
                    u4=partial(_arg, 4, False),
                    s4=partial(_arg, 4, True),
                    slen=_arg_slen,
                    olen1=_arg_olen1,
                    slen1=_arg_slen1,
                    ulen1=_arg_ulen1)


def _dispatch(table, min, max=None, state=None, args=('raw',)):
    """Decorator for dispatch by opcode. Sets the values in *table*
    from *min* to *max* to this method, adds a check that the Dvi state
    matches *state* if not None, reads arguments from the file according
    to *args*.

    *table*
        the dispatch table to be filled in

    *min*
        minimum opcode for calling this function

    *max*
        maximum opcode for calling this function, None if only *min* is allowed

    *state*
        state of the Dvi object in which these opcodes are allowed

    *args*
        sequence of argument specifications:

        ``'raw'``: opcode minus minimum
        ``'u1'``: read one unsigned byte
        ``'u4'``: read four bytes, treat as an unsigned number
        ``'s4'``: read four bytes, treat as a signed number
        ``'slen'``: read (opcode - minimum) bytes, treat as signed
        ``'slen1'``: read (opcode - minimum + 1) bytes, treat as signed
        ``'ulen1'``: read (opcode - minimum + 1) bytes, treat as unsigned
        ``'olen1'``: read (opcode - minimum + 1) bytes, treat as unsigned
                     if under four bytes, signed if four bytes
    """
    def decorate(method):
        get_args = [_arg_mapping[x] for x in args]

        @wraps(method)
        def wrapper(self, byte):
            if state is not None and self.state != state:
                raise ValueError("state precondition failed")
            return method(self, *[f(self, byte-min) for f in get_args])
        if max is None:
            table[min] = wrapper
        else:
            for i in range(min, max+1):
                assert table[i] is None
                table[i] = wrapper
        return wrapper
    return decorate


class Dvi(object):
    """
    A reader for a dvi ("device-independent") file, as produced by TeX.
    The current implementation can only iterate through pages in order.

    This class can be used as a context manager to close the underlying
    file upon exit. Pages can be read via iteration. Here is an overly
    simple way to extract text without trying to detect whitespace::

    >>> with matplotlib.dviread.Dvi('input.dvi', 72) as dvi:
    >>>     for page in dvi:
    >>>         print(''.join(chr(t.glyph) for t in page.text))

    Parameters
    ----------

    filename : str
        dvi file to read
    dpi : number or None
        Dots per inch, can be floating-point; this affects the
        coordinates returned. Use None to get TeX's internal units
        which are likely only useful for debugging.
    cache : TeXSupportCache instance, optional
        Support file cache instance, defaults to the TeXSupportCache
        singleton.
    """
    # dispatch table
    _dtable = [None] * 256
    _dispatch = partial(_dispatch, _dtable)

    def __init__(self, filename, dpi, cache=None):
        """
        Read the data from the file named *filename* and convert
        TeX's internal units to units of *dpi* per inch.
        *dpi* only sets the units and does not limit the resolution.
        Use None to return TeX's internal units.
        """
        _log.debug('Dvi: %s', filename)
        if cache is None:
            cache = TeXSupportCache.get_cache()
        self.cache = cache
        self.file = open(filename, 'rb')
        self.dpi = dpi
        self.fonts = {}
        self.state = _dvistate.pre
        self.baseline = self._get_baseline(filename)
        self.fontnames = sorted(set(self._read_fonts()))
        # populate kpsewhich cache with font pathnames
        find_tex_files([x + suffix for x in self.fontnames
                        for suffix in ('.tfm', '.vf', '.pfb')],
                       cache)
        cache.optimize()

    def _get_baseline(self, filename):
        if rcParams['text.latex.preview']:
            base, ext = os.path.splitext(filename)
            baseline_filename = base + ".baseline"
            if os.path.exists(baseline_filename):
                with open(baseline_filename, 'rb') as fd:
                    line = fd.read().split()
                height, depth, width = line
                return float(depth)
        return None

    def __enter__(self):
        """
        Context manager enter method, does nothing.
        """
        return self

    def __exit__(self, etype, evalue, etrace):
        """
        Context manager exit method, closes the underlying file if it is open.
        """
        self.close()

    def __iter__(self):
        """
        Iterate through the pages of the file.

        Yields
        ------
        Page
            Details of all the text and box objects on the page.
            The Page tuple contains lists of Text and Box tuples and
            the page dimensions, and the Text and Box tuples contain
            coordinates transformed into a standard Cartesian
            coordinate system at the dpi value given when initializing.
            The coordinates are floating point numbers, but otherwise
            precision is not lost and coordinate values are not clipped to
            integers.
        """
        while True:
            have_page = self._read()
            if have_page:
                yield self._output()
            else:
                break

    def close(self):
        """
        Close the underlying file if it is open.
        """
        if not self.file.closed:
            self.file.close()

    def _output(self):
        """
        Output the text and boxes belonging to the most recent page.
        page = dvi._output()
        """
        minx, miny, maxx, maxy = np.inf, np.inf, -np.inf, -np.inf
        maxy_pure = -np.inf
        for elt in self.text + self.boxes:
            if isinstance(elt, Box):
                x, y, h, w = elt
                e = 0           # zero depth
            else:               # glyph
                x, y, font, g, w = elt
                h, e = font._height_depth_of(g)
            minx = min(minx, x)
            miny = min(miny, y - h)
            maxx = max(maxx, x + w)
            maxy = max(maxy, y + e)
            maxy_pure = max(maxy_pure, y)

        if self.dpi is None:
            # special case for ease of debugging: output raw dvi coordinates
            return Page(text=self.text, boxes=self.boxes,
                        width=maxx-minx, height=maxy_pure-miny,
                        descent=maxy-maxy_pure)

        # convert from TeX's "scaled points" to dpi units
        d = self.dpi / (72.27 * 2**16)
        if self.baseline is None:
            descent = (maxy - maxy_pure) * d
        else:
            descent = self.baseline

        text = [Text((x-minx)*d, (maxy-y)*d - descent, f, g, w*d)
                for (x, y, f, g, w) in self.text]
        boxes = [Box((x-minx)*d, (maxy-y)*d - descent, h*d, w*d)
                 for (x, y, h, w) in self.boxes]

        return Page(text=text, boxes=boxes, width=(maxx-minx)*d,
                    height=(maxy_pure-miny)*d, descent=descent)

    def _read_fonts(self):
        """Read the postamble of the file and return a list of fonts used."""

        file = self.file
        offset = -1
        while offset > -100:
            file.seek(offset, 2)
            byte = file.read(1)[0]
            if byte != 223:
                break
            offset -= 1
        if offset >= -4:
            raise ValueError(
                "malformed dvi file %s: too few 223 bytes" % file.name)
        if byte != 2:
            raise ValueError(
                ("malformed dvi file %s: post-postamble "
                 "identification byte not 2") % file.name)
        file.seek(offset - 4, 2)
        offset = struct.unpack('!I', file.read(4))[0]
        file.seek(offset, 0)
        try:
            byte = file.read(1)[0]
        except IndexError:
            raise ValueError(
                "malformed dvi file %s: postamble offset %d out of range"
                % (file.name, offset))
        if byte != 248:
            raise ValueError(
                "malformed dvi file %s: postamble not found at offset %d"
                % (file.name, offset))

        fonts = []
        file.seek(28, 1)
        while True:
            byte = file.read(1)[0]
            if 243 <= byte <= 246:
                _, _, _, _, a, length = (
                    _arg_olen1(self, byte-243),
                    _arg(4, False, self, None),
                    _arg(4, False, self, None),
                    _arg(4, False, self, None),
                    _arg(1, False, self, None),
                    _arg(1, False, self, None))
                fontname = file.read(a + length)[-length:].decode('ascii')
                fonts.append(fontname)
            elif byte == 249:
                break
            else:
                raise ValueError(
                    "malformed dvi file %s: opcode %d in postamble"
                    % (file.name, byte))
        file.seek(0, 0)
        return fonts

    def _read(self):
        """
        Read one page from the file. Return True if successful,
        False if there were no more pages.
        """
        while True:
            byte = self.file.read(1)[0]
            self._dtable[byte](self, byte)
            if byte == 140:                         # end of page
                return True
            if self.state is _dvistate.post_post:   # end of file
                self.close()
                return False

    def _arg(self, nbytes, signed=False):
        """
        Read and return an integer argument *nbytes* long.
        Signedness is determined by the *signed* keyword.
        """
        str = self.file.read(nbytes)
        value = str[0]
        if signed and value >= 0x80:
            value = value - 0x100
        for i in range(1, nbytes):
            value = 0x100*value + str[i]
        return value

    @_dispatch(min=0, max=127, state=_dvistate.inpage)
    def _set_char_immediate(self, char):
        self._put_char_real(char)
        self.h += self.fonts[self.f]._width_of(char)

    @_dispatch(min=128, max=131, state=_dvistate.inpage, args=('olen1',))
    def _set_char(self, char):
        self._put_char_real(char)
        self.h += self.fonts[self.f]._width_of(char)

    @_dispatch(132, state=_dvistate.inpage, args=('s4', 's4'))
    def _set_rule(self, a, b):
        self._put_rule_real(a, b)
        self.h += b

    @_dispatch(min=133, max=136, state=_dvistate.inpage, args=('olen1',))
    def _put_char(self, char):
        self._put_char_real(char)

    def _put_char_real(self, char):
        font = self.fonts[self.f]
        if font._vf is None:
            self.text.append(Text(self.h, self.v, font, char,
                                  font._width_of(char)))
        else:
            scale = font.scale
            for x, y, f, g, w in font._vf[char].text:
                newf = DviFont(scale=_mul2012(scale, f.scale),
                               tfm=f._tfm, texname=f.texname, vf=f._vf)
                self.text.append(Text(self.h + _mul2012(x, scale),
                                      self.v + _mul2012(y, scale),
                                      newf, g, newf._width_of(g)))
            self.boxes.extend([Box(self.h + _mul2012(x, scale),
                                   self.v + _mul2012(y, scale),
                                   _mul2012(a, scale), _mul2012(b, scale))
                               for x, y, a, b in font._vf[char].boxes])

    @_dispatch(137, state=_dvistate.inpage, args=('s4', 's4'))
    def _put_rule(self, a, b):
        self._put_rule_real(a, b)

    def _put_rule_real(self, a, b):
        if a > 0 and b > 0:
            self.boxes.append(Box(self.h, self.v, a, b))

    @_dispatch(138)
    def _nop(self, _):
        pass

    @_dispatch(139, state=_dvistate.outer, args=('s4',)*11)
    def _bop(self, c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, p):
        self.state = _dvistate.inpage
        self.h, self.v, self.w, self.x, self.y, self.z = 0, 0, 0, 0, 0, 0
        self.stack = []
        self.text = []          # list of Text objects
        self.boxes = []         # list of Box objects

    @_dispatch(140, state=_dvistate.inpage)
    def _eop(self, _):
        self.state = _dvistate.outer
        del self.h, self.v, self.w, self.x, self.y, self.z, self.stack

    @_dispatch(141, state=_dvistate.inpage)
    def _push(self, _):
        self.stack.append((self.h, self.v, self.w, self.x, self.y, self.z))

    @_dispatch(142, state=_dvistate.inpage)
    def _pop(self, _):
        self.h, self.v, self.w, self.x, self.y, self.z = self.stack.pop()

    @_dispatch(min=143, max=146, state=_dvistate.inpage, args=('slen1',))
    def _right(self, b):
        self.h += b

    @_dispatch(min=147, max=151, state=_dvistate.inpage, args=('slen',))
    def _right_w(self, new_w):
        if new_w is not None:
            self.w = new_w
        self.h += self.w

    @_dispatch(min=152, max=156, state=_dvistate.inpage, args=('slen',))
    def _right_x(self, new_x):
        if new_x is not None:
            self.x = new_x
        self.h += self.x

    @_dispatch(min=157, max=160, state=_dvistate.inpage, args=('slen1',))
    def _down(self, a):
        self.v += a

    @_dispatch(min=161, max=165, state=_dvistate.inpage, args=('slen',))
    def _down_y(self, new_y):
        if new_y is not None:
            self.y = new_y
        self.v += self.y

    @_dispatch(min=166, max=170, state=_dvistate.inpage, args=('slen',))
    def _down_z(self, new_z):
        if new_z is not None:
            self.z = new_z
        self.v += self.z

    @_dispatch(min=171, max=234, state=_dvistate.inpage)
    def _fnt_num_immediate(self, k):
        self.f = k

    @_dispatch(min=235, max=238, state=_dvistate.inpage, args=('olen1',))
    def _fnt_num(self, new_f):
        self.f = new_f

    @_dispatch(min=239, max=242, args=('ulen1',))
    def _xxx(self, datalen):
        special = self.file.read(datalen)
        _log.debug(
            'Dvi._xxx: encountered special: %s',
            ''.join([chr(ch) if 32 <= ch < 127 else '<%02x>' % ch
                     for ch in special]))

    @_dispatch(min=243, max=246, args=('olen1', 'u4', 'u4', 'u4', 'u1', 'u1'))
    def _fnt_def(self, k, c, s, d, a, l):
        self._fnt_def_real(k, c, s, d, a, l)

    def _fnt_def_real(self, k, c, s, d, a, l):
        n = self.file.read(a + l)
        fontname = n[-l:].decode('ascii')
        tfm = _tfmfile(fontname)
        if tfm is None:
            raise FileNotFoundError("missing font metrics file: %s" % fontname)
        if c != 0 and tfm.checksum != 0 and c != tfm.checksum:
            raise ValueError('tfm checksum mismatch: %s' % n)

        vf = _vffile(fontname)

        self.fonts[k] = DviFont(scale=s, tfm=tfm, texname=n, vf=vf)

    @_dispatch(247, state=_dvistate.pre, args=('u1', 'u4', 'u4', 'u4', 'u1'))
    def _pre(self, i, num, den, mag, k):
        comment = self.file.read(k)
        if i != 2:
            raise ValueError("Unknown dvi format %d" % i)
        if num != 25400000 or den != 7227 * 2**16:
            raise ValueError("nonstandard units in dvi file")
            # meaning: TeX always uses those exact values, so it
            # should be enough for us to support those
            # (There are 72.27 pt to an inch so 7227 pt =
            # 7227 * 2**16 sp to 100 in. The numerator is multiplied
            # by 10^5 to get units of 10**-7 meters.)
        if mag != 1000:
            raise ValueError("nonstandard magnification in dvi file")
            # meaning: LaTeX seems to frown on setting \mag, so
            # I think we can assume this is constant
        self.state = _dvistate.outer

    @_dispatch(248, state=_dvistate.outer)
    def _post(self, _):
        self.state = _dvistate.post_post
        # TODO: actually read the postamble and finale?
        # currently post_post just triggers closing the file

    @_dispatch(249)
    def _post_post(self, _):
        raise NotImplementedError

    @_dispatch(min=250, max=255)
    def _malformed(self, offset):
        raise ValueError("unknown command: byte %d", 250 + offset)


class DviFont(object):
    """
    Encapsulation of a font that a DVI file can refer to.

    This class holds a font's texname and size, supports comparison,
    and knows the widths of glyphs in the same units as the AFM file.
    There are also internal attributes (for use by dviread.py) that
    are *not* used for comparison.

    The size is in Adobe points (converted from TeX points).

    Parameters
    ----------

    scale : float
       Factor by which the font is scaled from its natural size,
       represented as an integer in 20.12 fixed-point format.
    tfm : Tfm, may be None if widths given
       TeX Font Metrics file for this font
    texname : bytes
       Name of the font as used internally by TeX and friends, as an
       ASCII bytestring. This is usually very different from any external
       font names, and :class:`dviread.PsfontsMap` can be used to find
       the external name of the font.
    vf : Vf or None
       A TeX "virtual font" file, or None if this font is not virtual.
    widths : list of integers, optional
       Widths for this font. Overrides the widths read from the tfm file.

    Attributes
    ----------

    texname : bytes
    size : float
       Size of the font in Adobe points, converted from the slightly
       smaller TeX points.
    scale : int
       Factor by which the font is scaled from its natural size,
       represented as an integer in 20.12 fixed-point format.
    widths : list
       Widths of glyphs in glyph-space units, typically 1/1000ths of
       the point size.

    """
    __slots__ = ('texname', 'size', 'widths', 'scale', '_vf', '_tfm')

    def __init__(self, scale, tfm, texname, vf, widths=None):
        if not isinstance(texname, bytes):
            raise ValueError("texname must be a bytestring, got %s"
                             % type(texname))
        self.scale, self._tfm, self.texname, self._vf, self.widths = \
            scale, tfm, texname, vf, widths
        self.size = scale * (72.0 / (72.27 * 2**16))

        if self.widths is None:
            try:
                nchars = max(tfm.width) + 1
            except ValueError:
                nchars = 0
            self.widths = [(1000*tfm.width.get(char, 0)) >> 20
                           for char in range(nchars)]

    def __repr__(self):
        return '<DviFont %s *%f>' % (self.texname, self.scale / 2**20)

    def __hash__(self):
        return 1001 * hash(self.texname) + hash(self.size)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
            self.texname == other.texname and self.size == other.size

    def __ne__(self, other):
        return not self.__eq__(other)

    def _width_of(self, char):
        """
        Width of char in dvi units. For internal use by dviread.py.
        """

        width = self._tfm.width.get(char, None)
        if width is not None:
            return _mul2012(width, self.scale)
        _log.debug('No width for char %d in font %s.', char, self.texname)
        return 0

    def _height_depth_of(self, char):
        """
        Height and depth of char in dvi units. For internal use by dviread.py.
        """

        result = []
        for metric, name in ((self._tfm.height, "height"),
                             (self._tfm.depth, "depth")):
            value = metric.get(char, None)
            if value is None:
                _log.debug('No %s for char %d in font %s',
                           name, char, self.texname)
                result.append(0)
            else:
                result.append(_mul2012(value, self.scale))
        return result


class Vf(Dvi):
    """
    A virtual font (\\*.vf file) containing subroutines for dvi files.

    Usage::

      vf = Vf(filename)
      glyph = vf[code]
      glyph.text, glyph.boxes, glyph.width

    Parameters
    ----------

    filename : string or bytestring
        vf file to read
    cache : TeXSupportCache instance, optional
        Support file cache instance, defaults to the TeXSupportCache
        singleton.

    Notes
    -----

    The virtual font format is a derivative of dvi:
    http://mirrors.ctan.org/info/knuth/virtual-fonts
    This class reuses some of the machinery of `Dvi`
    but replaces the `_read` loop and dispatch mechanism.
    """

    def __init__(self, filename, cache=None):
        Dvi.__init__(self, filename, dpi=0, cache=cache)
        try:
            self._first_font = None
            self._chars = {}
            self._read()
        finally:
            self.close()

    def __getitem__(self, code):
        return self._chars[code]

    def _read_fonts(self):
        """Read through the font-definition section of the vf file
        and return the list of font names."""
        fonts = []
        self.file.seek(0, 0)
        while True:
            byte = self.file.read(1)[0]
            if byte <= 242 or byte >= 248:
                break
            elif 243 <= byte <= 246:
                _ = self._arg(byte - 242)
                _, _, _, a, length = [self._arg(x) for x in (4, 4, 4, 1, 1)]
                fontname = self.file.read(a + length)[-length:].decode('ascii')
                fonts.append(fontname)
            elif byte == 247:
                _, k = self._arg(1), self._arg(1)
                _ = self.file.read(k)
                _, _ = self._arg(4), self._arg(4)
        self.file.seek(0, 0)
        return fonts

    def _read(self):
        """
        Read one page from the file. Return True if successful,
        False if there were no more pages.
        """
        packet_char, packet_ends = None, None
        packet_len, packet_width = None, None
        while True:
            byte = self.file.read(1)[0]
            # If we are in a packet, execute the dvi instructions
            if self.state is _dvistate.inpage:
                byte_at = self.file.tell()-1
                if byte_at == packet_ends:
                    self._finalize_packet(packet_char, packet_width)
                    packet_len, packet_char, packet_width = None, None, None
                    # fall through to out-of-packet code
                elif byte_at > packet_ends:
                    raise ValueError("Packet length mismatch in vf file")
                else:
                    if byte in (139, 140) or byte >= 243:
                        raise ValueError(
                            "Inappropriate opcode %d in vf file" % byte)
                    Dvi._dtable[byte](self, byte)
                    continue

            # We are outside a packet
            if byte < 242:          # a short packet (length given by byte)
                packet_len = byte
                packet_char, packet_width = self._arg(1), self._arg(3)
                packet_ends = self._init_packet(byte)
                self.state = _dvistate.inpage
            elif byte == 242:       # a long packet
                packet_len, packet_char, packet_width = \
                            [self._arg(x) for x in (4, 4, 4)]
                self._init_packet(packet_len)
            elif 243 <= byte <= 246:
                k = self._arg(byte - 242, byte == 246)
                c, s, d, a, length = [self._arg(x) for x in (4, 4, 4, 1, 1)]
                self._fnt_def_real(k, c, s, d, a, length)
                if self._first_font is None:
                    self._first_font = k
            elif byte == 247:       # preamble
                i, k = self._arg(1), self._arg(1)
                x = self.file.read(k)
                cs, ds = self._arg(4), self._arg(4)
                self._pre(i, x, cs, ds)
            elif byte == 248:       # postamble (just some number of 248s)
                break
            else:
                raise ValueError("unknown vf opcode %d" % byte)

    def _init_packet(self, pl):
        if self.state != _dvistate.outer:
            raise ValueError("Misplaced packet in vf file")
        self.h, self.v, self.w, self.x, self.y, self.z = 0, 0, 0, 0, 0, 0
        self.stack, self.text, self.boxes = [], [], []
        self.f = self._first_font
        return self.file.tell() + pl

    def _finalize_packet(self, packet_char, packet_width):
        self._chars[packet_char] = Page(
            text=self.text, boxes=self.boxes, width=packet_width,
            height=None, descent=None)
        self.state = _dvistate.outer

    def _pre(self, i, x, cs, ds):
        if self.state is not _dvistate.pre:
            raise ValueError("pre command in middle of vf file")
        if i != 202:
            raise ValueError("Unknown vf format %d" % i)
        if len(x):
            _log.debug('vf file comment: %s', x)
        self.state = _dvistate.outer
        # cs = checksum, ds = design size


def _fix2comp(num):
    """
    Convert from two's complement to negative.
    """
    assert 0 <= num < 2**32
    if num & 2**31:
        return num - 2**32
    else:
        return num


def _mul2012(num1, num2):
    """
    Multiply two numbers in 20.12 fixed point format.
    """
    # Separated into a function because >> has surprising precedence
    return (num1*num2) >> 20


class Tfm(object):
    """
    A TeX Font Metric file.

    This implementation covers only the bare minimum needed by the Dvi class.

    Parameters
    ----------
    filename : string or bytestring

    Attributes
    ----------
    checksum : int
       Used for verifying against the dvi file.
    design_size : int
       Design size of the font (unknown units)
    width, height, depth : dict
       Dimensions of each character, need to be scaled by the factor
       specified in the dvi file. These are dicts because indexing may
       not start from 0.
    """
    __slots__ = ('checksum', 'design_size', 'width', 'height', 'depth')

    def __init__(self, filename):
        _log.debug('opening tfm file %s', filename)
        with open(filename, 'rb') as file:
            header1 = file.read(24)
            lh, bc, ec, nw, nh, nd = \
                struct.unpack('!6H', header1[2:14])
            _log.debug('lh=%d, bc=%d, ec=%d, nw=%d, nh=%d, nd=%d',
                       lh, bc, ec, nw, nh, nd)
            header2 = file.read(4*lh)
            self.checksum, self.design_size = \
                struct.unpack('!2I', header2[:8])
            # there is also encoding information etc.
            char_info = file.read(4*(ec-bc+1))
            widths = file.read(4*nw)
            heights = file.read(4*nh)
            depths = file.read(4*nd)

        self.width, self.height, self.depth = {}, {}, {}
        widths, heights, depths = \
            [struct.unpack('!%dI' % (len(x)/4), x)
             for x in (widths, heights, depths)]
        for idx, char in enumerate(range(bc, ec+1)):
            byte0 = char_info[4*idx]
            byte1 = char_info[4*idx+1]
            self.width[char] = _fix2comp(widths[byte0])
            self.height[char] = _fix2comp(heights[byte1 >> 4])
            self.depth[char] = _fix2comp(depths[byte1 & 0xf])


PsFont = namedtuple('Font', 'texname psname effects encoding filename')


class PsfontsMap(object):
    """
    A psfonts.map formatted file, mapping TeX fonts to PS fonts.

    Usage::

     >>> map = PsfontsMap(find_tex_file('pdftex.map'))
     >>> entry = map[b'ptmbo8r']
     >>> entry.texname
     b'ptmbo8r'
     >>> entry.psname
     b'Times-Bold'
     >>> entry.encoding
     '/usr/local/texlive/2008/texmf-dist/fonts/enc/dvips/base/8r.enc'
     >>> entry.effects
     {'slant': 0.16700000000000001}
     >>> entry.filename

    Parameters
    ----------

    filename : string or bytestring

    Notes
    -----

    For historical reasons, TeX knows many Type-1 fonts by different
    names than the outside world. (For one thing, the names have to
    fit in eight characters.) Also, TeX's native fonts are not Type-1
    but Metafont, which is nontrivial to convert to PostScript except
    as a bitmap. While high-quality conversions to Type-1 format exist
    and are shipped with modern TeX distributions, we need to know
    which Type-1 fonts are the counterparts of which native fonts. For
    these reasons a mapping is needed from internal font names to font
    file names.

    A texmf tree typically includes mapping files called e.g.
    :file:`psfonts.map`, :file:`pdftex.map`, or :file:`dvipdfm.map`.
    The file :file:`psfonts.map` is used by :program:`dvips`,
    :file:`pdftex.map` by :program:`pdfTeX`, and :file:`dvipdfm.map`
    by :program:`dvipdfm`. :file:`psfonts.map` might avoid embedding
    the 35 PostScript fonts (i.e., have no filename for them, as in
    the Times-Bold example above), while the pdf-related files perhaps
    only avoid the "Base 14" pdf fonts. But the user may have
    configured these files differently.
    """
    __slots__ = ('_font', '_filename')

    def __init__(self, filename):
        self._font = {}
        self._filename = filename
        if isinstance(filename, bytes):
            encoding = sys.getfilesystemencoding() or 'utf-8'
            self._filename = filename.decode(encoding, errors='replace')
        with open(filename, 'rb') as file:
            self._parse(file)

    def __getitem__(self, texname):
        assert isinstance(texname, bytes)
        try:
            result = self._font[texname]
        except KeyError:
            fmt = ('A PostScript file for the font whose TeX name is "{0}" '
                   'could not be found in the file "{1}". The dviread module '
                   'can only handle fonts that have an associated PostScript '
                   'font file. '
                   'This problem can often be solved by installing '
                   'a suitable PostScript font package in your (TeX) '
                   'package manager.')
            msg = fmt.format(texname.decode('ascii'), self._filename)
            msg = textwrap.fill(msg, break_on_hyphens=False,
                                break_long_words=False)
            _log.info(msg)
            raise
        fn, enc = result.filename, result.encoding
        if fn is not None and not fn.startswith(b'/'):
            fn = find_tex_file(fn)
        if enc is not None and not enc.startswith(b'/'):
            enc = find_tex_file(result.encoding)
        return result._replace(filename=fn, encoding=enc)

    def _parse(self, file):
        """
        Parse the font mapping file.

        The format is, AFAIK: texname fontname [effects and filenames]
        Effects are PostScript snippets like ".177 SlantFont",
        filenames begin with one or two less-than signs. A filename
        ending in enc is an encoding file, other filenames are font
        files. This can be overridden with a left bracket: <[foobar
        indicates an encoding file named foobar.

        There is some difference between <foo.pfb and <<bar.pfb in
        subsetting, but I have no example of << in my TeX installation.
        """
        # If the map file specifies multiple encodings for a font, we
        # follow pdfTeX in choosing the last one specified. Such
        # entries are probably mistakes but they have occurred.
        # http://tex.stackexchange.com/questions/10826/
        # http://article.gmane.org/gmane.comp.tex.pdftex/4914

        empty_re = re.compile(br'%|\s*$')
        word_re = re.compile(
            br'''(?x) (?:
                 "<\[ (?P<enc1>  [^"]+    )" | # quoted encoding marked by [
                 "<   (?P<enc2>  [^"]+.enc)" | # quoted encoding, ends in .enc
                 "<<? (?P<file1> [^"]+    )" | # quoted font file name
                 "    (?P<eff1>  [^"]+    )" | # quoted effects or font name
                 <\[  (?P<enc3>  \S+      )  | # encoding marked by [
                 <    (?P<enc4>  \S+  .enc)  | # encoding, ends in .enc
                 <<?  (?P<file2> \S+      )  | # font file name
                      (?P<eff2>  \S+      )    # effects or font name
            )''')
        effects_re = re.compile(
            br'''(?x) (?P<slant> -?[0-9]*(?:\.[0-9]+)) \s* SlantFont
                    | (?P<extend>-?[0-9]*(?:\.[0-9]+)) \s* ExtendFont''')

        lines = (line.strip()
                 for line in file
                 if not empty_re.match(line))
        for line in lines:
            effects, encoding, filename = b'', None, None
            words = word_re.finditer(line)

            # The named groups are mutually exclusive and are
            # referenced below at an estimated order of probability of
            # occurrence based on looking at my copy of pdftex.map.
            # The font names are probably unquoted:
            w = next(words)
            texname = w.group('eff2') or w.group('eff1')
            w = next(words)
            psname = w.group('eff2') or w.group('eff1')

            for w in words:
                # Any effects are almost always quoted:
                eff = w.group('eff1') or w.group('eff2')
                if eff:
                    effects = eff
                    continue
                # Encoding files usually have the .enc suffix
                # and almost never need quoting:
                enc = (w.group('enc4') or w.group('enc3') or
                       w.group('enc2') or w.group('enc1'))
                if enc:
                    if encoding is not None:
                        _log.debug('Multiple encodings for %s = %s',
                                   texname, psname)
                    encoding = enc
                    continue
                # File names are probably unquoted:
                filename = w.group('file2') or w.group('file1')

            effects_dict = {}
            for match in effects_re.finditer(effects):
                slant = match.group('slant')
                if slant:
                    effects_dict['slant'] = float(slant)
                else:
                    effects_dict['extend'] = float(match.group('extend'))

            self._font[texname] = PsFont(
                texname=texname, psname=psname, effects=effects_dict,
                encoding=encoding, filename=filename)


class Encoding(object):
    """
    Parses a \\*.enc file referenced from a psfonts.map style file.
    The format this class understands is a very limited subset of
    PostScript.

    Usage (subject to change)::

      for name in Encoding(filename):
          whatever(name)

    Parameters
    ----------
    filename : string or bytestring

    Attributes
    ----------
    encoding : list
        List of character names
    """
    __slots__ = ('encoding',)

    def __init__(self, filename):
        with open(filename, 'rb') as file:
            _log.debug('Parsing TeX encoding %s', filename)
            self.encoding = self._parse(file)
            _log.debug('Result: %s', self.encoding)

    def __iter__(self):
        for name in self.encoding:
            yield name

    def _parse(self, file):
        result = []

        lines = (line.split(b'%', 1)[0].strip() for line in file)
        data = b''.join(lines)
        beginning = data.find(b'[')
        if beginning < 0:
            raise ValueError("Cannot locate beginning of encoding in {}"
                             .format(file))
        data = data[beginning:]
        end = data.find(b']')
        if end < 0:
            raise ValueError("Cannot locate end of encoding in {}"
                             .format(file))
        data = data[:end]

        return re.findall(br'/([^][{}<>\s]+)', data)


class TeXSupportCacheError(Exception):
    pass


class TeXSupportCache:
    """A persistent cache of data related to support files related to dvi
    files produced by TeX. Currently holds results from :program:`kpsewhich`
    and the contents of parsed dvi files, in future versions could include
    pre-parsed font data etc.

    Usage::

      # create or get the singleton instance
      cache = TeXSupportCache.get_cache()

      # insert and query some pathnames
      with cache.connection as transaction:
          cache.update_pathnames(
              {"pdftex.map": "/usr/local/pdftex.map",
               "cmsy10.pfb": "/usr/local/fonts/cmsy10.pfb"},
               transaction)
      pathnames = cache.get_pathnames(["pdftex.map", "cmr10.pfb"])
      # now pathnames = {"pdftex.map": "/usr/local/pdftex.map"}

      # optional after inserting new data, may improve query performance:
      cache.optimize()

      # insert and query some dvi file contents
      with cache.connection as transaction:
          id = cache.dvi_new_file("/path/to/foobar.dvi", transaction)
          font_ids = cache.dvi_font_sync_ids(['font1', 'font2'], transaction)
          cache.dvi_font_sync_metrics(DviFont1, transaction)
          cache.dvi_font_sync_metrics(DviFont2, transaction)
          for i, box in enumerate(boxes):
               cache.dvi_add_box(box, id, 0, i, transaction)
          for i, text in enumerate(texts):
               cache.dvi_add_text(text, id, 0, i, font_ids['font1'],
                                  transaction)
      fonts = cache.dvi_fonts(id)
      assert cache.dvi_page_exists(id, 0)
      bbox = cache.dvi_page_boundingbox(id, 0)
      for box in dvi_page_boxes(id, 0):
          handle_box(box)
      for text in dvi_page_texts(id, 0):
          handle_text(text)

    Parameters
    ----------

    filename : str, optional
        File in which to store the cache. Defaults to `texsupport.N.db` in
        the standard cache directory where N is the current schema version.

    Attributes
    ----------

    connection
        This database connection object has a context manager to set up
        a transaction. Transactions are passed into methods that write to
        the database.
    """

    __slots__ = ('connection')
    schema_version = 2  # should match PRAGMA user_version in _create
    instance = None

    @classmethod
    def get_cache(cls):
        "Return the singleton instance of the cache, at the default location"
        if cls.instance is None:
            cls.instance = cls()
        return cls.instance

    def __init__(self, filename=None):
        if filename is None:
            filename = os.path.join(get_cachedir(), 'texsupport.%d.db'
                                    % self.schema_version)

        self.connection = sqlite3.connect(
                filename, isolation_level="DEFERRED")
        if _log.isEnabledFor(logging.DEBUG):
            def debug_sql(sql):
                _log.debug(' '.join(sql.splitlines()).strip())
            self.connection.set_trace_callback(debug_sql)
        self.connection.row_factory = sqlite3.Row
        with self.connection as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA foreign_keys=ON;
            """)
            version, = conn.execute("PRAGMA user_version;").fetchone()

        if version == 0:
            self._create()
        elif version != self.schema_version:
            raise TeXSupportCacheError(
                "support database %s has version %d, expected %d"
                % (filename, version, self.schema_version))

    def _create(self):
        """Create the database."""
        with self.connection as conn:
            # kpsewhich results
            conn.executescript(
                """
                PRAGMA page_size=4096;
                CREATE TABLE file_path(
                    filename TEXT PRIMARY KEY NOT NULL,
                    pathname TEXT
                ) WITHOUT ROWID;
                """)
            # dvi files
            conn.executescript(
                """
                CREATE TABLE dvi_file(
                    id INTEGER PRIMARY KEY,
                    name UNIQUE NOT NULL,
                    mtime INTEGER,
                    size INTEGER
                );
                CREATE TABLE dvi_font(
                    id INTEGER PRIMARY KEY,
                    texname UNIQUE NOT NULL
                );
                CREATE TABLE dvi_font_metrics(
                    id INTEGER NOT NULL
                        REFERENCES dvi_font(id) ON DELETE CASCADE,
                    scale INTEGER NOT NULL,
                    widths BLOB NOT NULL,
                    PRIMARY KEY (id, scale)
                );
                CREATE TABLE dvi(
                    fileid INTEGER NOT NULL
                        REFERENCES dvi_file(id) ON DELETE CASCADE,
                    pageno INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    width INTEGER NOT NULL,
                    depth INTEGER NOT NULL,
                    fontid INTEGER,
                    fontscale INTEGER,
                    glyph INTEGER,
                    PRIMARY KEY (fileid, pageno, seq)
                ) WITHOUT ROWID;
                CREATE TABLE dvi_baseline(
                    fileid INTEGER NOT NULL
                        REFERENCES dvi_file(id) ON DELETE CASCADE,
                    pageno INTEGER NOT NULL,
                    baseline REAL NOT NULL,
                    PRIMARY KEY (fileid, pageno)
                ) WITHOUT ROWID;
                PRAGMA user_version=2;
                """)

    def optimize(self):
        """Optional optimization phase after updating data.
        Executes sqlite's `PRAGMA optimize` statement, which can call
        `ANALYZE` or other functions that can improve future query performance
        by spending some time up-front."""
        with self.connection as conn:
            conn.execute("PRAGMA optimize;")

    def get_pathnames(self, filenames):
        """Query the cache for pathnames related to `filenames`.

        Parameters
        ----------
        filenames : iterable of str

        Returns
        -------
        mapping from str to (str or None)
            For those filenames that exist in the cache, the mapping
            includes either the related pathname or None to indicate that
            the named file does not exist.
        """
        rows = self.connection.execute(
            "SELECT filename, pathname FROM file_path WHERE filename IN "
            "(%s)"
            % ','.join('?' for _ in filenames),
            filenames).fetchall()
        return {filename: pathname for (filename, pathname) in rows}

    def update_pathnames(self, mapping, transaction):
        """Update the cache with the given filename-to-pathname mapping

        Parameters
        ----------
        mapping : mapping from str to (str or None)
            Mapping from filenames to the corresponding full pathnames
            or None to indicate that the named file does not exist.
        transaction : obtained via the context manager of self.connection
        """
        transaction.executemany(
            "INSERT OR REPLACE INTO file_path (filename, pathname) "
            "VALUES (?, ?)",
            mapping.items())

    # Dvi files

    def dvi_new_file(self, name, transaction):
        """Record a dvi file in the cache.

        Parameters
        ----------
        name : str
            Name of the file to add.
        transaction : obtained via the context manager of self.connection
        """

        stat = os.stat(name)
        transaction.execute("DELETE FROM dvi_file WHERE name=?", (name,))
        transaction.execute(
            "INSERT INTO dvi_file (name, mtime, size) VALUES (?, ?, ?)",
            (name, int(stat.st_mtime), int(stat.st_size)))
        return transaction.execute("SELECT last_insert_rowid()").fetchone()[0]

    def dvi_id(self, name):
        """Query the database identifier of a given dvi file.

        Parameters
        ----------
        name : str
            Name of the file to query.

        Returns
        -------
        int or None
        """

        rows = self.connection.execute(
            "SELECT id, mtime, size FROM dvi_file WHERE name=? LIMIT 1",
            (name,)).fetchall()
        if rows:
            id, mtime, size = rows[0]
            stat = os.stat(name)
            if mtime == int(stat.st_mtime) and size == stat.st_size:
                return id

    def dvi_font_sync_ids(self, fontnames, transaction):
        """Record dvi fonts in the cache and return their database
        identifiers.

        Parameters
        ----------
        fontnames : list of str
            TeX names of fonts
        transaction : obtained via the context manager of self.connection

        Returns
        -------
        mapping from texname to int
        """

        transaction.executemany(
            "INSERT OR IGNORE INTO dvi_font (texname) VALUES (?)",
            ((name,) for name in fontnames))
        fontid = {}
        for name in fontnames:
            fontid[name], = transaction.execute(
                "SELECT id FROM dvi_font WHERE texname=?",
                (name,)).fetchone()
        return fontid

    def dvi_font_sync_metrics(self, dvifont, transaction):
        """Record dvi font metrics in the cache.

        Parameters
        ----------
        dvifont : DviFont
        transaction : obtained via the context manager of self.connection
        """

        exists = bool(transaction.execute("""
            SELECT 1 FROM dvi_font_metrics m, dvi_font f
            WHERE m.id=f.id AND f.texname=:texname
            AND m.scale=:scale LIMIT 1
        """, {
            "texname": dvifont.texname.decode('ascii'),
            "scale": dvifont.scale
        }).fetchall())

        if not exists:
            # Widths are given in 32-bit words in tfm, although the normal
            # range is around 1000 units. This and the repetition of values
            # make the width data very compressible.
            widths = struct.pack('<{}I'.format(len(dvifont.widths)),
                                 *dvifont.widths)
            widths = zlib.compress(widths, 9)
            transaction.execute("""
                INSERT INTO dvi_font_metrics (id, scale, widths)
                SELECT id, :scale, :widths FROM dvi_font WHERE texname=:texname
            """, {
                "texname": dvifont.texname.decode('ascii'),
                "scale": dvifont.scale,
                "widths": widths
            })

    def dvi_fonts(self, fileid):
        """Query the dvi fonts of a given dvi file.

        Parameters
        ----------
        fileid : int
            File identifier as returned by dvi_id

        Returns
        -------
        mapping from (str, float) to DviFont
            Maps from (TeX name, scale) to DviFont objects.
        """

        rows = self.connection.execute("""
            SELECT texname, fontscale, widths FROM
            (SELECT DISTINCT fontid, fontscale FROM dvi WHERE fileid=?) d
            JOIN dvi_font f ON (d.fontid=f.id)
            JOIN dvi_font_metrics m ON (d.fontid=m.id AND d.fontscale=m.scale)
        """, (fileid,)).fetchall()

        def decode(widths):
            data = zlib.decompress(widths)
            n = len(data) // 4
            return struct.unpack('<{}I'.format(n), data)

        return {(row['texname'], row['fontscale']):
                DviFont(texname=row['texname'].encode('ascii'),
                        scale=row['fontscale'],
                        widths=decode(row['widths']),
                        tfm=None, vf=None)
                for row in rows}

    def dvi_add_box(self, box, fileid, pageno, seq, transaction):
        """Record a box object of a dvi file.

        Parameters
        ----------
        box : Box
        fileid : int
            As returned by dvi_id
        pageno : int
            Page number
        seq : int
            Used to order the boxes
        transaction : obtained via the context manager of self.connection
        """

        transaction.execute("""
            INSERT INTO dvi (
                fileid, pageno, seq, x, y, height, width, depth
            ) VALUES (:fileid, :pageno, :seq, :x, :y, :height, :width, 0)
        """, {
            "fileid": fileid, "pageno": pageno, "seq": seq,
            "x": box.x, "y": box.y, "height": box.height, "width": box.width
        })

    def dvi_add_text(self, text, fileid, pageno, seq, fontid, transaction):
        """Record a box object of a dvi file.

        Parameters
        ----------
        box : Text
        fileid : int
            As returned by dvi_id
        pageno : int
            Page number
        seq : int
            Used to order the boxes
        fontid : int
            As returned by dvi_font_sync_ids
        transaction : obtained via the context manager of self.connection
        """

        height, depth = text.font._height_depth_of(text.glyph)
        transaction.execute("""
            INSERT INTO dvi (
                fileid, pageno, seq,
                x, y, height, width, depth, fontid, fontscale, glyph
            ) VALUES (
                :fileid, :pageno, :seq,
                :x, :y, :height, :width, :depth, :fontid, :fontscale, :glyph
            )
        """, {
            "fileid": fileid, "pageno": pageno, "seq": seq,
            "x": text.x, "y": text.y, "width": text.width,
            "height": height, "depth": depth,
            "fontid": fontid, "fontscale": text.font.scale, "glyph": text.glyph
        })

    def dvi_page_exists(self, fileid, pageno):
        """Query if a page exists in the dvi file.

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno : int
            Page number

        Returns
        -------
        boolean
        """
        return bool(self.connection.execute(
            "SELECT 1 FROM dvi WHERE fileid=? AND pageno=? LIMIT 1",
            (fileid, pageno)).fetchall())

    def dvi_page_boundingbox(self, fileid, pageno):
        """Query the bounding box of a page

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno
            Page number

        Returns
        -------
        A namedtuple-like object with fields min_x, min_y, max_x,
        max_y and max_y_pure (like max_y but ignores depth).
        """

        return self.connection.execute("""
                SELECT min(x)          min_x,
                       min(y - height) min_y,
                       max(x + width)  max_x,
                       max(y + depth)  max_y,
                       max(y)          max_y_pure
                FROM dvi WHERE fileid=? AND pageno=?
                """, (fileid, pageno)).fetchone()

    def dvi_page_boxes(self, fileid, pageno):
        """Query the boxes of a page

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno
            Page number

        Returns
        -------
        An iterator of (x, y, height, width) tuples of boxes
        """

        return self.connection.execute("""
            SELECT x, y, height, width FROM dvi
            WHERE fileid=? AND pageno=? AND fontid IS NULL ORDER BY seq
        """, (fileid, pageno)).fetchall()

    def dvi_page_text(self, fileid, pageno):
        """Query the text of a page

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno
            Page number

        Returns
        -------
        An iterator of (x, y, height, width, depth, texname, fontscale)
        tuples of text
        """

        return self.connection.execute("""
            SELECT x, y, height, width, depth, f.texname, fontscale, glyph
            FROM dvi JOIN dvi_font f ON (dvi.fontid=f.id)
            WHERE fileid=? AND pageno=? AND fontid IS NOT NULL ORDER BY seq
        """, (fileid, pageno)).fetchall()

    def dvi_add_baseline(self, fileid, pageno, baseline, transaction):
        """Record the baseline of a dvi page

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno : int
            Page number
        baseline : float
        transaction : obtained via the context manager of self.connection
        """

        transaction.execute("""
            INSERT INTO dvi_baseline (fileid, pageno, baseline)
            VALUES (:fileid, :pageno, :baseline)
        """, {"fileid": fileid, "pageno": pageno, "baseline": baseline})

    def dvi_get_baseline(self, fileid, pageno):
        """Query the baseline of a dvi page

        Parameters
        ----------
        fileid : int
            As returned by dvi_id
        pageno : int
            Page number

        Returns
        -------
        float
        """

        rows = self.connection.execute(
            "SELECT baseline FROM dvi_baseline WHERE fileid=? AND pageno=?",
            (fileid, pageno)).fetchall()
        if rows:
            return rows[0][0]


def find_tex_files(filenames, cache=None):
    """Find multiple files in the texmf tree. This can be more efficient
    than `find_tex_file` because it makes only one call to `kpsewhich`.

    Calls :program:`kpsewhich` which is an interface to the kpathsea
    library [1]_. Most existing TeX distributions on Unix-like systems use
    kpathsea. It is also available as part of MikTeX, a popular
    distribution on Windows.

    The results are cached into the TeX support database. In case of
    mistaken results, deleting the database resets the cache.

    Parameters
    ----------
    filename : string or bytestring
    cache : TeXSupportCache, optional
        Cache instance to use, defaults to the singleton instance of the class.

    References
    ----------

    .. [1] `Kpathsea documentation <http://www.tug.org/kpathsea/>`_
        The library that :program:`kpsewhich` is part of.

    """

    # we expect these to always be ascii encoded, but use utf-8
    # out of caution
    filenames = [f.decode('utf-8', errors='replace')
                 if isinstance(f, bytes) else f
                 for f in filenames]
    if cache is None:
        cache = TeXSupportCache.get_cache()
    result = cache.get_pathnames(filenames)

    filenames = [f for f in filenames if f not in result]
    if not filenames:
        return result

    cmd = ['kpsewhich'] + list(filenames)
    _log.debug('find_tex_files: %s', cmd)
    pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    output = pipe.communicate()[0].decode('ascii').splitlines()
    _log.debug('find_tex_files result: %s', output)
    mapping = _match(filenames, output)
    with cache.connection as transaction:
        cache.update_pathnames(mapping, transaction)
    result.update(mapping)

    return result


def _match(filenames, pathnames):
    """
    Match filenames to pathnames in lists that are in matching order,
    except that some filenames may lack pathnames.
    """
    result = {f: None for f in filenames}
    filenames, pathnames = iter(filenames), iter(pathnames)
    try:
        filename, pathname = next(filenames), next(pathnames)
        while True:
            if pathname.endswith(os.path.sep + filename):
                result[filename] = pathname
                pathname = next(pathnames)
            filename = next(filenames)
    except StopIteration:
        return result


def find_tex_file(filename, format=None, cache=None):
    """
    Find a file in the texmf tree.

    Calls :program:`kpsewhich` which is an interface to the kpathsea
    library [1]_. Most existing TeX distributions on Unix-like systems use
    kpathsea. It is also available as part of MikTeX, a popular
    distribution on Windows.

    The results are cached into a database whose location defaults to
    :file:`~/.matplotlib/texsupport.db`. In case of mistaken results,
    deleting this file resets the cache.

    Parameters
    ----------
    filename : string or bytestring
    format : string or bytestring, DEPRECATED
        Used as the value of the `--format` option to :program:`kpsewhich`.
        Could be e.g. 'tfm' or 'vf' to limit the search to that type of files.
        Deprecated to allow batching multiple filenames into one kpsewhich
        call, since any format option would apply to all filenames at once.
    cache : TeXSupportCache, optional
        Cache instance to use, defaults to the singleton instance of the class.

    References
    ----------

    .. [1] `Kpathsea documentation <http://www.tug.org/kpathsea/>`_
        The library that :program:`kpsewhich` is part of.
    """

    if format is not None:
        cbook.warn_deprecated(
            "3.0",
            "The format option to find_tex_file is deprecated "
            "to allow batching multiple filenames into one call. "
            "Omitting the option should not change the result, as "
            "kpsewhich uses the filename extension to choose the path.")
        # we expect these to always be ascii encoded, but use utf-8
        # out of caution
        if isinstance(filename, bytes):
            filename = filename.decode('utf-8', errors='replace')
        if isinstance(format, bytes):
            format = format.decode('utf-8', errors='replace')

        cmd = ['kpsewhich']
        if format is not None:
            cmd += ['--format=' + format]
        cmd += [filename]
        _log.debug('find_tex_file(%s): %s', filename, cmd)
        pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        result = pipe.communicate()[0].rstrip()
        _log.debug('find_tex_file result: %s', result)
        return result.decode('ascii')

    return list(find_tex_files([filename], cache).values())[0]


# With multiple text objects per figure (e.g., tick labels) we may end
# up reading the same tfm and vf files many times, so we implement a
# simple cache. TODO: is this worth making persistent?

@lru_cache()
def _fontfile(cls, suffix, texname):
    filename = find_tex_file(texname + suffix)
    return cls(filename) if filename else None


_tfmfile = partial(_fontfile, Tfm, ".tfm")
_vffile = partial(_fontfile, Vf, ".vf")


if __name__ == '__main__':
    import sys
    fname = sys.argv[1]
    try:
        dpi = float(sys.argv[2])
    except IndexError:
        dpi = None
    with Dvi(fname, dpi) as dvi:
        fontmap = PsfontsMap(find_tex_file('pdftex.map'))
        for page in dvi:
            print('=== new page ===')
            fPrev = None
            for x, y, f, c, w in page.text:
                if f != fPrev:
                    print('font', f.texname, 'scaled', f.scale/pow(2.0, 20))
                    fPrev = f
                print(x, y, c, 32 <= c < 128 and chr(c) or '.', w)
            for x, y, w, h in page.boxes:
                print(x, y, 'BOX', w, h)
